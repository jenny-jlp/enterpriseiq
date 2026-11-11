"""Workbench boundary tests: real PostgreSQL, no paid model requests."""

from contextlib import contextmanager
import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from enterpriseiq.web import create_app
from enterpriseiq.web_auth import Identity
from enterpriseiq.jobs import Jobs


@pytest.fixture
def workbench(conn):
    Jobs(conn).initialize()
    users = {
        "investigator-token": Identity("alice", "demo", ("investigator", "reviewer")),
        "reviewer-token": Identity("bob", "demo", ("reviewer",)),
        "viewer-token": Identity("carol", "demo", ("viewer",)),
        "other-token": Identity("mallory", "other", ("investigator", "reviewer")),
    }

    @contextmanager
    def connect():
        yield conn

    app = create_app(connect=connect, users={hashlib.sha256(k.encode()).hexdigest(): v for k, v in users.items()})
    with TestClient(app) as client:
        yield client, conn


def login(client, token="investigator-token"):
    assert client.post("/api/session", json={"token": token}).status_code == 200


def submit(client, **changes):
    body = {"request_id": str(uuid4()), "question": "按区域统计 ARR 前三", **changes}
    response = client.post("/api/jobs", json=body)
    return body, response


def test_submit_is_durable_and_idempotent_after_client_disconnect(workbench):
    client, conn = workbench
    login(client)
    body, response = submit(client)
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    client.cookies.clear()
    login(client)
    repeated = client.post("/api/jobs", json=body)
    assert repeated.status_code == 202 and repeated.json()["job_id"] == job["job_id"]
    assert client.get("/api/jobs/" + job["job_id"]).json()["status"] == "queued"
    assert conn.execute("SELECT count(*) FROM investigation_web.jobs WHERE job_id=%s", (job["job_id"],)).fetchone()[0] == 1
    assert client.post("/api/jobs", json={**body, "question": "another question"}).status_code == 409
    assert client.get("/api/cases/" + job["case_id"]).status_code == 200


def test_backend_roles_and_resource_scope_cannot_be_bypassed(workbench):
    client, _ = workbench
    assert client.get("/api/cases").status_code == 401
    login(client)
    _, response = submit(client)
    job = response.json()
    login(client, "viewer-token")
    assert client.get("/api/cases/" + job["case_id"]).status_code == 200
    assert submit(client)[1].status_code == 403
    assert client.post(f"/api/runs/{job['run_id']}/resume", json={"request_id": str(uuid4())}).status_code == 403
    login(client, "reviewer-token")
    assert submit(client)[1].status_code == 403
    login(client, "other-token")
    assert client.get("/api/jobs/" + job["job_id"]).status_code == 404
    assert client.get("/api/cases/" + job["case_id"]).status_code == 404
    assert client.get("/api/cases").json() == []
    assert client.post(f"/api/runs/{job['run_id']}/resume", json={"request_id": str(uuid4())}).status_code == 404


def test_cross_origin_and_forged_identity_are_rejected(workbench):
    client, _ = workbench
    assert client.post("/api/session", json={"token": "wrong"}).status_code == 401
    login(client)
    assert submit(client, actor="bob")[1].status_code == 422
    assert client.post("/api/jobs", headers={"Origin": "https://evil.example"}, json={"request_id": str(uuid4()), "question": "test"}).status_code == 403
    assert client.get("/api/jobs/not-a-uuid").status_code == 422


def test_own_report_review_is_denied_even_with_reviewer_role(workbench):
    client, _ = workbench
    login(client)
    _, response = submit(client)
    job = response.json()
    review = {"request_id": str(uuid4()), "report_id": str(uuid4()), "action": "accept", "reason": "reviewed"}
    assert client.post(f"/api/cases/{job['case_id']}/review", json=review).status_code == 403


def test_evidence_endpoint_requires_both_run_and_evidence_membership(workbench, bundle):
    from enterpriseiq.store import import_bundle
    from enterpriseiq.cases import Cases
    from test_reliability import RecordedRun
    client, conn = workbench
    import_bundle(conn, bundle)
    login(client)
    _, response = submit(client, question="调查 Vantara")
    job = response.json()
    recorded = RecordedRun(conn)
    recorded.state["run_id"] = job["run_id"]
    ref_id = recorded.record("account", "ACC-013")
    checked = recorded.verify([{"kind": "field", "evidence_id": ref_id, "field": "/account_name", "value": "Vantara"}])
    Cases(conn).reconcile({**recorded.state, **checked})
    path = f"/api/runs/{job['run_id']}/evidence/{ref_id}"
    source = client.get(path)
    assert source.status_code == 200
    assert source.json()["content"]["account_name"] == "Vantara"
    assert source.json()["reference"]["source_id"] == "ACC-013"
    assert client.get(path + "unknown").status_code == 404
    login(client, "other-token")
    assert client.get(path).status_code == 404


def test_completed_review_uses_server_actor_and_keeps_history(workbench, bundle):
    from enterpriseiq.store import import_bundle
    from enterpriseiq.cases import Cases
    from test_reliability import RecordedRun
    import json
    client, conn = workbench
    import_bundle(conn, bundle)
    login(client)
    _, response = submit(client, question="调查 Vantara")
    job = response.json()
    recorded = RecordedRun(conn)
    recorded.state["run_id"] = job["run_id"]
    ref = recorded.record("account", "ACC-013")
    checked = recorded.verify([{"kind": "field", "evidence_id": ref, "field": "/account_name", "value": "Vantara"}])
    Cases(conn).reconcile({**recorded.state, "draft_answer": recorded.state["answer"], **checked})
    conn.execute("UPDATE investigation_web.jobs SET status='completed' WHERE job_id=%s", (job["job_id"],))
    case = client.get('/api/cases/' + job['case_id']).json()
    report = case['reports'][-1]
    own_review = {"request_id": str(uuid4()), "report_id": report['report_id'], "action": "accept", "reason": "禁止自审"}
    assert client.post(f"/api/cases/{job['case_id']}/review", json=own_review).status_code == 403
    login(client, "reviewer-token")
    path = f"/api/cases/{job['case_id']}/review"
    draft = json.loads(report['payload']['draft'])
    draft['open_questions'] = ['人工待确认：下一步安排？']
    modified = client.post(path, json={"request_id": str(uuid4()), "report_id": report['report_id'], "action": "modify", "reason": "补充问题", "draft": draft})
    assert modified.status_code == 200 and modified.json()['report_validation'] == 'passed'
    accepted = {"request_id": str(uuid4()), "report_id": modified.json()['new_report']['report_id'], "action": "accept", "reason": "已核对证据"}
    assert client.post(path, json=accepted).status_code == 200
    assert client.post(path, json=accepted).status_code == 200
    shown = client.get('/api/cases/' + job['case_id']).json()
    assert len(shown['reports']) == 2 and len(shown['reviews']) == 2
    assert shown['business_status'] == 'accept'
    assert {r['actor'] for r in shown['reviews']} == {'bob'}
    assert client.post(path, json={**accepted, 'request_id': str(uuid4()), 'report_id': report['report_id']}).status_code == 409
