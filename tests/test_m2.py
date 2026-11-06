"""M2 contract, recovery and review invariants. No paid model requests."""

from copy import deepcopy
import json
import os
import subprocess
import sys
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
import psycopg
import pytest

from enterpriseiq.agent import build_agent, execute_run, initial_state, postgres_checkpointer, run_lock
from enterpriseiq.cases import Cases
from enterpriseiq.model import Completion, ModelError
from enterpriseiq.reliability import ReportVerifier, repair_feedback
from enterpriseiq.tasks import investigation_gaps, task_contract
from test_agent import AdaptiveModel, EvidenceTools, FixtureVerifier
from test_reliability import RecordedRun


@pytest.mark.parametrize("question", [
    "列出全部开放 P1 工单、产品模块、客户名和年度经常性收入",
    "逐个列出未关闭 P1 tickets 的 components 和 customers 与 ARR",
])
def test_synonyms_preserve_required_ticket_collection(question):
    contract = task_contract(question)
    assert contract["required_tables"] == ["ticket_contexts"]
    assert contract["priorities"] == ["p1"]


def test_contract_tampering_is_blocked(populated):
    run = RecordedRun(populated)
    ref = run.record("account", "ACC-013")
    run.state["task_contract"]["required_tables"] = []
    run.state["task_contract"]["snapshot_id"] = "another-snapshot"
    checked = run.verify([{"kind": "field", "evidence_id": ref, "field": "/account_name", "value": "Vantara"}])
    assert "task_contract_mismatch" in {g["code"] for g in checked["report"]["gaps"]}


def test_clarification_continues_same_checkpoint_and_excludes_wait():
    model, tools = AdaptiveModel(), EvidenceTools()
    graph = build_agent(model, tools, InMemorySaver(), verifier=FixtureVerifier())
    state = initial_state("按区域统计 ARR 前三", model.name)
    result = execute_run(graph, state=state)
    assert result["status"] == "awaiting_input" and result["model_calls"] == 0
    config = {"configurable": {"thread_id": state["run_id"]}}
    graph.update_state(config, {"deadline": 0})  # Arbitrarily long human wait.
    assert execute_run(graph, run_id=state["run_id"])["status"] == "awaiting_input"
    resumed = execute_run(graph, run_id=state["run_id"], clarification="产品组件，不是地理区域")
    assert resumed["run_id"] == state["run_id"]
    assert resumed["task_contract"]["scope"] == "direct_component"
    assert resumed["model_calls"] == 3 and resumed["tool_calls"] == 2
    assert resumed["remaining_seconds"] < result["remaining_seconds"]
    with pytest.raises(ValueError, match="does not accept"):
        execute_run(graph, run_id=state["run_id"], clarification="another")


@pytest.mark.parametrize("code,expected_calls", [("model_http_503", 3), ("model_timeout", 3), ("model_http_401", 1)])
def test_model_retry_is_bounded_and_attempts_persist(code, expected_calls):
    class Broken:
        def complete(self, *args, **kwargs):
            raise ModelError(code)

    result = execute_run(build_agent(Broken(), EvidenceTools(), InMemorySaver()), state=initial_state("Find account", "fake"))
    assert result["model_calls"] == expected_calls
    assert len(result["trace"]) == expected_calls
    assert result["usage_incomplete"] and result["status"] == "failed"
    assert all(e["usage_unknown"] for e in result["trace"].values())


def test_recovered_model_failure_does_not_poison_valid_report():
    class Recovering(AdaptiveModel):
        attempts = 0

        def complete(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise ModelError("model_http_429")
            return super().complete(*args, **kwargs)

    result = execute_run(build_agent(Recovering(), EvidenceTools(), InMemorySaver(), verifier=FixtureVerifier()),
                         state=initial_state("Find account", "fake"))
    assert result["model_calls"] == 4 and result["tool_calls"] == 2
    assert result["status"] == "completed" and not result["errors"]
    assert result["usage_incomplete"] and result["prompt_tokens"] == 15


def test_successful_tools_do_not_mark_investigation_verified():
    graph = build_agent(AdaptiveModel(), EvidenceTools(), InMemorySaver(), pause_after_tool=True)
    run = execute_run(graph, state=initial_state("Find account", "fake"))
    ledger = investigation_gaps(run)
    assert ledger[0]["status"] == "pending_validation"
    assert not any(g["status"] == "verified" for g in ledger)


def test_postgres_run_lock_rejects_other_connection():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL")
    state = initial_state("Find account", "fake")
    with postgres_checkpointer(dsn) as first, postgres_checkpointer(dsn) as second:
        g1 = build_agent(AdaptiveModel(), EvidenceTools(), first)
        g2 = build_agent(AdaptiveModel(), EvidenceTools(), second)
        with run_lock(g1, state["run_id"]):
            with pytest.raises(ValueError, match="already executing"):
                execute_run(g2, state=state)
            # A new process has no shared Python lock: this exercises the DB lock.
            process = subprocess.run([sys.executable, "-c", """
import os, sys
from enterpriseiq.agent import build_agent, execute_run, initial_state, postgres_checkpointer
with postgres_checkpointer(os.environ['TEST_DATABASE_URL']) as saver:
    graph = build_agent(None, object(), saver)
    state = initial_state('Find account', 'fake')
    state['run_id'] = sys.argv[1]
    try:
        execute_run(graph, state=state)
    except ValueError as exc:
        assert str(exc) == 'Run is already executing'
        print('database_lock_rejected')
    else:
        raise AssertionError('Concurrent run was permitted')
""", state["run_id"]], capture_output=True, text=True, timeout=30)
            assert process.returncode == 0 and "database_lock_rejected" in process.stdout
        result = execute_run(g2, state=state)
        assert result["tool_calls"] == 2
        second.delete_thread(state["run_id"])


def case_fixture(conn):
    cases = Cases(conn)
    cases.initialize()
    run = RecordedRun(conn, "查看 ACC-013 客户名称")
    ref = run.record("account", "ACC-013")
    checked = run.verify([{"kind": "field", "evidence_id": ref, "field": "/account_name", "value": "Vantara"}])
    assert checked["report_validation"] == "passed"
    run.state.update(checked)
    run.state["draft_answer"] = run.state["answer"]
    case_id = cases.register(run.state)
    cases.reconcile(run.state)
    return cases, case_id, run.state


def test_case_projection_review_idempotency_and_immutable_history(populated):
    cases, case_id, state = case_fixture(populated)
    cases.reconcile(state)
    show = cases.show(case_id)
    assert len(show["reports"]) == 1 and show["business_status"] == "awaiting_review"
    report_id = show["reports"][0]["report_id"]
    request_id = str(uuid4())
    args = (case_id, report_id, "accept", "reviewer", "原文核对通过", request_id)
    result = cases.review(*args, opinion="请业务负责人跟进")
    assert cases.review(*args, opinion="请业务负责人跟进") == result
    assert cases.show(case_id)["business_status"] == "accept"
    assert len(cases.show(case_id)["reviews"]) == 1
    with pytest.raises(ValueError, match="different content"):
        cases.review(*args, opinion="changed")
    with populated.transaction(force_rollback=True):
        with pytest.raises(psycopg.Error, match="append-only"):
            populated.execute("UPDATE investigation_cases.reports SET validation='blocked' WHERE report_id=%s", (report_id,))


def test_fact_modification_revalidates_and_old_version_is_rejected(populated):
    cases, case_id, state = case_fixture(populated)
    report_id = cases.show(case_id)["reports"][0]["report_id"]
    draft = json.loads(state["draft_answer"])
    draft["claims"][0]["value"] = "Fabricated account"
    result = cases.review(case_id, report_id, "modify", "reviewer", "修订事实", uuid4(), draft=draft)
    assert result["report_validation"] == "blocked"
    show = cases.show(case_id)
    assert len(show["reports"]) == 2 and show["business_status"] == "blocked"
    with pytest.raises(ValueError, match="Stale"):
        cases.review(case_id, report_id, "accept", "reviewer", "旧报告", uuid4())
    with pytest.raises(ValueError, match="Blocked"):
        cases.review(case_id, show["reports"][-1]["report_id"], "accept", "reviewer", "接受", uuid4())
    # Reconciliation must not overwrite or duplicate either version.
    cases.reconcile(state)
    assert cases.show(case_id)["reports"] == show["reports"]


def test_followup_retains_history_and_invalidates_previous_review_target(populated):
    cases, case_id, state = case_fixture(populated)
    report_id = cases.show(case_id)["reports"][0]["report_id"]
    followup = initial_state("补充调查 ACC-013", "fake")
    assert cases.register(followup, case_id) == case_id
    assert cases.show(case_id)["business_status"] == "investigating"
    with pytest.raises(ValueError, match="newer investigation"):
        cases.review(case_id, report_id, "accept", "reviewer", "接受旧调查", uuid4())
    with pytest.raises(ValueError, match="Case not found"):
        cases.register(initial_state("bad case", "fake"), uuid4())


def test_valid_fact_edit_requires_new_review_and_cannot_erase_history(populated):
    cases, case_id, state = case_fixture(populated)
    old = cases.show(case_id)["reports"][0]
    draft = json.loads(state["draft_answer"])
    draft["open_questions"] = ["请客户负责人核实后续行动是否完成"]
    result = cases.review(case_id, old["report_id"], "modify", "reviewer", "保留未知事项", uuid4(), draft=draft)
    assert result["report_validation"] == "passed"
    assert cases.show(case_id)["business_status"] == "awaiting_review"
    cases.review(case_id, result["new_report"]["report_id"], "accept", "reviewer", "核对后接受", uuid4())
    show = cases.show(case_id)
    assert len(show["reviews"]) == 2 and show["reports"][0] == old
    assert show["reports"][-1]["payload"]["report"]["open_questions_unverified"] == draft["open_questions"]


def test_retry_respects_model_call_budget():
    class Broken:
        def complete(self, *args, **kwargs):
            raise ModelError("model_http_503")
    state = initial_state("Find account", "fake")
    state["max_model_calls"] = 2
    result = execute_run(build_agent(Broken(), EvidenceTools(), InMemorySaver()), state=state)
    assert result["model_calls"] == 2 and len(result["trace"]) == 2


def test_report_contract_metadata_gets_actionable_schema_feedback(populated):
    run = RecordedRun(populated, "查看 ACC-013 客户名称")
    ref = run.record("account", "ACC-013")
    valid = {"scope": "investigation", "claims": [{"kind": "field", "evidence_id": ref,
             "field": "/account_name", "value": "Vantara"}], "open_questions": []}
    # Reproduces a real M2 failure: the model copied input metadata to the report.
    run.state["answer"] = json.dumps({**valid, "version": 1, "snapshot_id": run.state["snapshot_id"], "question": run.state["question"]})
    checked = ReportVerifier(populated).verify(run.state)
    assert checked["report_validation"] == "blocked"
    gap = next(g for g in checked["report"]["gaps"] if g["code"] == "unstructured_or_invalid_report")
    assert {tuple(e["location"]) for e in gap["schema_errors"]} == {("version",), ("snapshot_id",), ("question",)}
    assert all(e["type"] == "extra_forbidden" for e in gap["schema_errors"])
    run.state["answer"] = json.dumps(valid)
    assert ReportVerifier(populated).verify(run.state)["report_validation"] == "passed"


def test_generic_eta_cannot_hide_behind_a_valid_issue_eta(populated):
    run = RecordedRun(populated, "调查 ISS-017 ETA")
    issue = run.call("get_record", {"entity_type": "issue", "source_id": "ISS-017"})
    document = run.call("read_document", {"entity_type": "transcript", "source_id": "TRN-001"})
    valid = {"kind": "eta", "subject_id": "ISS-017", "evidence_id": issue["item"]["evidence"]["evidence_id"],
             "field": "/description", "quote": issue["item"]["data"]["description"], "value": "uncommitted"}
    invalid = {**valid, "evidence_id": document["evidence"]["evidence_id"], "field": None,
               "quote": "I don't have an ETA I can commit to today, but I need to get you a concrete update from engineering."}
    checked = run.verify([valid, invalid])
    assert checked["report_validation"] == "blocked"
    rejected = next(c for c in checked["report"]["rejected_claims"] if c["code"] == "eta_subject_not_supported")
    assert "subject_id" in rejected["explanation"]
    assert run.verify([valid])["report_validation"] == "passed"


def test_misattributed_quote_feedback_identifies_source_and_exact_assertion(populated):
    run = RecordedRun(populated, "查看支持工单原文")
    ref = run.record("ticket", "TKT-120")
    wrong = {"kind": "quote", "evidence_id": ref, "field": "/description",
             "quote": "we want to restructure our plan from per-seat to a volume-tier arrangement as part of that expansion"}
    checked = run.verify([wrong])
    assert checked["report_validation"] == "blocked"
    feedback = repair_feedback(run.state, checked["report"])
    assert feedback["rejected_assertions"][0]["rejected_claim"] == wrong
    assert feedback["rejected_assertions"][0]["referenced_source_id"] == "TKT-120"
    # Feedback must not fetch a replacement source, quote or benchmark answer.
    assert "TRN-001" not in json.dumps(feedback)


def test_advancement_investigation_cannot_omit_explicit_source_actions(populated, bundle):
    run = RecordedRun(populated, "调查推进条件并读取通话原文")
    result = run.call("read_document", {"entity_type": "transcript", "source_id": "TRN-001"})
    text = bundle.files["transcripts/trn-001-quarterly-business-review.txt"].decode("utf-8")
    lines = text.splitlines()
    first = next(line for line in lines if line.startswith("1. Provide written ETA"))
    claim = {"kind": "quote", "evidence_id": result["evidence"]["evidence_id"], "field": None, "quote": first}
    checked = run.verify([claim])
    assert checked["report_validation"] == "blocked"
    assert any(g["code"] == "source_actions_not_covered" for g in checked["report"]["gaps"])
    section = "\n".join(line for line in lines if line.startswith(("1. ", "2. ", "3. ", "4. ")))
    assert run.verify([{**claim, "quote": section}])["report_validation"] == "passed"
    # Merely requesting a quote does not require every source action.
    other = RecordedRun(populated, "读取通话并引用一句原文")
    document = other.call("read_document", {"entity_type": "transcript", "source_id": "TRN-001"})
    assert other.verify([{**claim, "evidence_id": document["evidence"]["evidence_id"]}])["report_validation"] == "passed"

    # The heading can be on an earlier page than subsequent action lines.
    paged = RecordedRun(populated, "调查推进条件并读取通话原文")
    cut = lines.index(first)+1
    page1 = paged.call("read_document", {"entity_type": "transcript", "source_id": "TRN-001", "line_count": cut})
    page2 = paged.call("read_document", {"entity_type": "transcript", "source_id": "TRN-001", "start_line": cut+1})
    first_claim = {**claim, "evidence_id": page1["evidence"]["evidence_id"]}
    assert paged.verify([first_claim])["report_validation"] == "blocked"
    quotes = [{**claim, "quote": line, "evidence_id": (page1 if i < cut else page2)["evidence"]["evidence_id"]}
              for i, line in enumerate(lines) if line.startswith(("1. ", "2. ", "3. ", "4. "))]
    assert paged.verify(quotes)["report_validation"] == "passed"


def test_provider_final_constraint_retains_current_correction():
    class Recording(AdaptiveModel):
        saw_correction = False

        def complete(self, messages, tools, timeout):
            assert messages[0]["role"] == "system"
            assert messages[-1]["role"] == "system"
            assert sum(m["role"] == "system" for m in messages) == 2
            self.saw_correction |= "CURRENT CORRECTION REQUIRED" in messages[-1]["content"]
            return super().complete(messages, tools, timeout)

    model = Recording()
    state = initial_state("Find account", model.name)
    state["messages"].append({"role": "system", "content": "Independent report validation rejected this draft. Correct the unsupported claim."})
    run = execute_run(build_agent(model, EvidenceTools(), InMemorySaver(), verifier=FixtureVerifier()), state=state)
    assert model.saw_correction and run["tool_calls"] == 2 and run["status"] == "completed"


def test_question_limit_feedback_exposes_limit_without_silently_dropping_items(populated):
    run = RecordedRun(populated)
    ref = run.record("account", "ACC-013")
    draft = {"scope": "investigation", "claims": [{"kind": "field", "evidence_id": ref,
             "field": "/account_name", "value": "Vantara"}], "open_questions": [f"Pending question {i}" for i in range(11)]}
    run.state["answer"] = json.dumps(draft)
    checked = ReportVerifier(populated).verify(run.state)
    error = next(g for g in checked["report"]["gaps"] if g["code"] == "unstructured_or_invalid_report")["schema_errors"][0]
    assert error["location"] == ["open_questions"] and error["max_length"] == 10
    assert len(json.loads(run.state["answer"])["open_questions"]) == 11
    assert checked["report_validation"] == "blocked"
