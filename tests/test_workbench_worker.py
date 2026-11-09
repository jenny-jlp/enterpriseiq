"""Real worker/API/checkpoint integration in disposable PostgreSQL databases, no network model."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import threading
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from enterpriseiq.jobs import Jobs, WORKER_LOCK
from enterpriseiq.model import Completion
from enterpriseiq.store import import_bundle
from enterpriseiq.web import create_app
from enterpriseiq.web_auth import Identity
from enterpriseiq.worker import run_worker


@pytest.fixture
def database(bundle):
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL")
    name = "enterpriseiq_m3_test_" + uuid4().hex
    params = conninfo_to_dict(dsn)
    params.update(dbname=name, connect_timeout="5")
    isolated = make_conninfo(**params)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(isolated) as conn:
            import_bundle(conn, bundle)
            Jobs(conn).initialize()
        yield isolated
    finally:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as admin:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def app_for(dsn):
    @contextmanager
    def connect():
        with psycopg.connect(dsn, autocommit=True) as conn:
            yield conn
    return create_app(connect=connect, users={hashlib.sha256(b"local-test-token").hexdigest(): Identity("alice", "demo", ("investigator",))})


def sign_in(client):
    assert client.post("/api/session", json={"token": "local-test-token"}).status_code == 200


class SourceModel:
    """Only replaces the paid external dependency; tools/verifier/checkpoint stay real."""
    def complete(self, messages, tools, timeout):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            scope_is_arr = '"scope": "direct_component"' in messages[-1]["content"]
            name = "calculate_arr_exposure" if scope_is_arr else "get_record"
            args = {} if scope_is_arr else {"entity_type": "account", "source_id": "ACC-013"}
            message = {"role": "assistant", "content": "Read original source", "tool_calls": [{"id": "source-1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
        else:
            result = results[-1]
            if "groups" in result:
                answer = {"scope": "direct_component", "claims": [{"kind": "table", "view": "arr_exposure", "source_call_ids": [result['call_id']], "top_n": 3}], "open_questions": []}
            else:
                answer = {"scope": "investigation", "claims": [{"kind": "field", "evidence_id": result["item"]["evidence"]["evidence_id"], "field": "/account_name", "value": "Vantara"}], "open_questions": []}
            message = {"role": "assistant", "content": json.dumps(answer)}
        return Completion(message, {"prompt_tokens": 5, "completion_tokens": 2}, "offline-fixture", "offline-response", "tool_calls" if "tool_calls" in message else "stop")


def test_worker_completes_after_browser_disconnect_and_only_one_worker_executes(database):
    entered, release = threading.Event(), threading.Event()

    class Blocking(SourceModel):
        def complete(self, *args, **kwargs):
            entered.set()
            assert release.wait(15), "test did not release model fixture"
            return super().complete(*args, **kwargs)

    app = app_for(database)
    with TestClient(app) as first:
        sign_in(first)
        job = first.post('/api/jobs', json={"request_id": str(uuid4()), "question": "查看 ACC-013 客户名称"}).json()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_worker, database, once=True, model=Blocking())
        try:
            assert entered.wait(10)
            with TestClient(app) as refreshed:
                sign_in(refreshed)
                assert refreshed.get('/api/jobs/' + job['job_id']).json()['status'] == 'running'
                progress = refreshed.get('/api/cases/' + job['case_id']).json()['progress']
                assert progress['tool_calls'] == 0
                with pytest.raises(ValueError, match="already running"):
                    run_worker(database, once=True, model=SourceModel())
        finally:
            release.set()
        assert future.result(timeout=15)
    with TestClient(app) as final:
        sign_in(final)
        case = final.get('/api/cases/' + job['case_id']).json()
        assert final.get('/api/jobs/' + job['job_id']).json()['status'] == 'completed'
        assert case['reports'][-1]['payload']['report_validation'] == 'passed'
        assert 'Vantara' in case['reports'][-1]['payload']['report']['formal_report']
        assert case['progress']['model_calls'] == 2 and case['progress']['tool_calls'] == 1
        ref = next(iter(case['runs'][-1]['evidence']))
        assert final.get(f"/api/runs/{job['run_id']}/evidence/{ref}").json()['content']['account_name'] == 'Vantara'


def test_clarification_uses_same_run_and_historical_wait_does_not_block_followup(database):
    app = app_for(database)
    with TestClient(app) as client:
        sign_in(client)
        job = client.post('/api/jobs', json={"request_id": str(uuid4()), "question": "按区域统计 ARR 前三"}).json()
        run_worker(database, once=True)
        assert client.get('/api/jobs/' + job['job_id']).json()['status'] == 'awaiting_input'
        resumed = client.post(f"/api/runs/{job['run_id']}/resume", json={"request_id": str(uuid4()), "clarification": "产品组件，不是地理区域"})
        assert resumed.status_code == 202 and resumed.json()['run_id'] == job['run_id']
        assert client.post(f"/api/runs/{job['run_id']}/resume", json={"request_id": str(uuid4()), "clarification": "产品组件"}).status_code == 409
        run_worker(database, once=True, model=SourceModel())
        case = client.get('/api/cases/' + job['case_id']).json()
        assert case['jobs'][-1]['status'] == 'completed'
        assert len(case['runs']) == 1
        assert case['reports'][-1]['payload']['report_validation'] == 'passed'
        followup = client.post('/api/jobs', json={"request_id": str(uuid4()), "question": "查看客户名称", "case_id": job['case_id']})
        assert followup.status_code == 202


def test_disabled_model_is_explicit_failure_and_never_calls_provider(database):
    app = app_for(database)
    with TestClient(app) as client:
        sign_in(client)
        job = client.post('/api/jobs', json={"request_id": str(uuid4()), "question": "查看客户名称"}).json()
        run_worker(database, once=True)
        result = client.get('/api/jobs/' + job['job_id']).json()
        assert result['status'] == 'failed' and result['error_code'] == 'model_calls_disabled'
        case = client.get('/api/cases/' + job['case_id']).json()
        assert case['reports'][-1]['payload']['report']['formal_report'] is None


def test_abandoned_job_is_interrupted_and_same_run_can_restart_before_first_checkpoint(database):
    app = app_for(database)
    with TestClient(app) as client:
        sign_in(client)
        body = {"request_id": str(uuid4()), "question": "查看 ACC-013 客户名称"}
        job = client.post('/api/jobs', json=body).json()
        with psycopg.connect(database) as conn:
            conn.execute("UPDATE investigation_web.jobs SET status='running' WHERE job_id=%s", (job['job_id'],))
        assert client.get('/api/jobs/' + job['job_id']).json()['status'] == 'interrupted'
        resumed = client.post(f"/api/runs/{job['run_id']}/resume", json={"request_id": str(uuid4())})
        assert resumed.status_code == 202
        run_worker(database, once=True, model=SourceModel())
        result = client.get('/api/jobs/' + resumed.json()['job_id']).json()
        assert result['status'] == 'completed'
        assert result['run_id'] == job['run_id']


def test_budget_exhaustion_blocks_before_provider_construction(database, monkeypatch):
    from enterpriseiq.budget import BudgetLedger, BudgetedModel
    from enterpriseiq.model import ModelError
    with psycopg.connect(database) as conn:
        ledger = BudgetLedger(conn)
        ledger.initialize()
        ledger.reserve(str(uuid4()), 1_199_999, 1)

    def forbidden_provider():
        pytest.fail('Provider must not be constructed after the budget is exhausted')

    monkeypatch.setattr('enterpriseiq.budget.DeepSeekModel', forbidden_provider)
    with pytest.raises(ModelError, match='budget_exhausted'):
        BudgetedModel(database, str(uuid4())).complete([], [], 1)
