import json
import os
from concurrent.futures import ThreadPoolExecutor

from langgraph.checkpoint.memory import InMemorySaver
import pytest
import psycopg
from psycopg.conninfo import conninfo_to_dict

from enterpriseiq.agent import build_agent, execute_run, initial_state, postgres_checkpointer
from enterpriseiq.model import Completion, ModelError


def call(name, arguments, number):
    return {"id": f"call-{number}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class EvidenceTools:
    def __init__(self):
        self.calls = []

    def execute(self, name, arguments):
        self.calls.append(name)
        return {"status": "ok", "complete": True, "item": {"data": {"account_id": "ACC-013"},
                "evidence": {"evidence_id": "ev-account", "source_id": "ACC-013"}}}


class AdaptiveModel:
    name = "test-model"

    def complete(self, messages, tools, timeout):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            message = {"role": "assistant", "content": "Locate the account", "tool_calls": [call("search_accounts", {"name": "Vantara"}, 1)]}
        elif len(results) == 1:
            # Next arguments depend on the actual previous tool response.
            account_id = results[0]["item"]["data"]["account_id"]
            message = {"role": "assistant", "content": "Read the matched account", "tool_calls": [call("get_record", {"entity_type": "account", "source_id": account_id}, 2)]}
        else:
            message = {"role": "assistant", "content": "Account found [ev-account]."}
        return Completion(message, {"prompt_tokens": 5, "completion_tokens": 2}, self.name, "test-response", "tool_calls" if "tool_calls" in message else "stop")


def test_agent_tool_budget_routes_to_partial_without_extra_call():
    tools = EvidenceTools()
    graph = build_agent(AdaptiveModel(), tools, InMemorySaver())
    result = execute_run(graph, state=initial_state("Find account", "test-model", max_tool_calls=1))
    assert tools.calls == ["search_accounts"]
    assert result["status"] == "partial" and result["stop_reason"] == "tool_call_budget"
    assert result["tool_calls"] == 1 and result["model_calls"] == 2
    assert result["prompt_tokens"] == 10 and result["completion_tokens"] == 4
    assert list(result["evidence"]) == ["ev-account"]


def test_model_error_ends_with_sanitized_failed_state():
    class UnavailableModel:
        def complete(self, *args, **kwargs):
            raise ModelError("model_http_401")

    tools = EvidenceTools()
    graph = build_agent(UnavailableModel(), tools, InMemorySaver())
    result = execute_run(graph, state=initial_state("Find account", "test-model"))
    assert result["status"] == "failed" and result["stop_reason"] == "model_http_401"
    assert not tools.calls and result["model_calls"] == 1


def test_expired_budget_does_not_contact_model_or_tools():
    state = initial_state("Find account", "test-model")
    state["deadline"] = 0
    tools = EvidenceTools()
    result = execute_run(build_agent(AdaptiveModel(), tools, InMemorySaver()), state=state)
    assert result["status"] == "failed" and result["stop_reason"] == "time_budget"
    assert not tools.calls and result["model_calls"] == 0


def test_postgres_checkpoint_survives_connection_and_graph_recreation():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL checkpoint recovery verification")
    state = initial_state("Find account", "test-model")
    first_tools = EvidenceTools()
    with postgres_checkpointer(dsn) as saver:
        graph = build_agent(AdaptiveModel(), first_tools, saver, pause_after_tool=True)
        paused = execute_run(graph, state=state)
        assert paused["status"] == "paused" and paused["next_nodes"] == ["agent"]
        assert paused["tool_calls"] == 1 and paused["model_calls"] == 1
    # Both connection and graph are gone. Rebuild them and load only the persisted run ID.
    second_tools = EvidenceTools()
    with postgres_checkpointer(dsn) as saver:
        try:
            graph = build_agent(AdaptiveModel(), second_tools, saver)
            restored = execute_run(graph, run_id=state["run_id"])
            assert restored["status"] == "completed"
            assert first_tools.calls == ["search_accounts"] and second_tools.calls == ["get_record"]
            assert restored["tool_calls"] == 2 and restored["model_calls"] == 3
            assert restored["prompt_tokens"] == 15 and restored["completion_tokens"] == 6
            assert len(restored["evidence"]) == 1 and len(restored["trace"]) == 5
            again = execute_run(graph, run_id=state["run_id"])
            assert again["trace"] == restored["trace"] and second_tools.calls == ["get_record"]
            with pytest.raises(ValueError, match="already exists"):
                execute_run(graph, state=state)
        finally:
            saver.delete_thread(state["run_id"])


def test_two_starters_can_initialize_a_fresh_checkpoint_schema():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn or conninfo_to_dict(dsn).get("dbname") != "enterpriseiq_test":
        pytest.skip("Fresh-schema test requires the dedicated enterpriseiq_test database")
    with psycopg.connect(dsn) as conn:
        conn.execute("DROP SCHEMA IF EXISTS investigation_checkpoint CASCADE")

    def initialize(_):
        with postgres_checkpointer(dsn):
            return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(initialize, range(2))) == [True, True]
