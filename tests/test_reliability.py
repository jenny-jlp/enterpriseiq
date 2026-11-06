"""Report correctness regressions; automatic tests make no paid model calls."""

import json
from copy import deepcopy

from langgraph.checkpoint.memory import InMemorySaver
from psycopg.types.json import Jsonb

from enterpriseiq.agent import build_agent, collect_evidence, execute_run, initial_state
from enterpriseiq.dataset import SNAPSHOT_ID, digest
from enterpriseiq.reliability import ReportVerifier, scoped_eta_values, coverage_gaps
from enterpriseiq.tools import BusinessTools


class RecordedRun:
    def __init__(self, conn, question="调查 Vantara"):
        self.conn = conn
        self.tools = BusinessTools(conn)
        self.state = initial_state(question, "test-model")
        self.state["stop_reason"] = "answered"

    def call(self, name, arguments):
        call_id = f"test-call-{self.state['tool_calls']+1}"
        result = self.tools.execute(name, arguments)
        result["call_id"] = call_id
        self.state["messages"].extend([
            {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]},
            {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)},
        ])
        self.state["tool_calls"] += 1
        self.state["evidence"].update(collect_evidence(result))
        return result

    def record(self, kind, source_id):
        return self.call("get_record", {"entity_type": kind, "source_id": source_id})["item"]["evidence"]["evidence_id"]

    def verify(self, claims, scope="investigation"):
        self.state["answer"] = json.dumps({"scope": scope, "claims": claims, "open_questions": []})
        return ReportVerifier(self.conn).verify(self.state)


def codes(result):
    return {gap["code"] for gap in result["report"]["gaps"]}


def test_eta_cannot_borrow_another_issue_predicate():
    ref = {"entity_type": "document", "source_id": "DOC"}
    text = "ISS-017 is stable. ISS-032 No ETA committed."
    assert scoped_eta_values(ref, text, "ISS-017") == set()
    assert scoped_eta_values(ref, text, "ISS-032") == {"uncommitted"}


def test_duplicate_tool_call_ids_cannot_hide_an_earlier_incomplete_query():
    from test_agent import call
    messages = []
    for index in range(2):
        messages.extend([
            {"role": "assistant", "tool_calls": [call("list_open_tickets", {}, 1)]},
            {"role": "tool", "tool_call_id": "call-1", "content": json.dumps({"status": "ok", "total_matches": 0, "items": [], "returned": 0, "offset": 0, "complete": True})},
        ])
    gaps = coverage_gaps({"messages": messages})
    assert any(gap.get("error") == "duplicate_call_id" for gap in gaps)


def test_claims_prove_original_values_quotes_and_relation_semantics(populated):
    run = RecordedRun(populated)
    account = run.record("account", "ACC-013")
    issue = run.record("issue", "ISS-017")
    ticket = run.record("ticket", "TKT-119")
    opportunity = run.record("opportunity", "OPP-006")
    claims = [
        {"kind": "field", "evidence_id": account, "field": "/arr", "value": 432000},
        {"kind": "quote", "evidence_id": issue, "field": "/description", "quote": "No ETA committed"},
        {"kind": "eta", "evidence_id": issue, "subject_id": "ISS-017", "field": "/description", "quote": "No ETA committed", "value": "uncommitted"},
        {"kind": "candidate", "ticket_evidence_id": ticket, "issue_evidence_id": issue, "part_id": "PART-010"},
        {"kind": "reference", "evidence_id": opportunity, "field": "/description", "target_evidence_id": issue},
    ]
    valid = run.verify(claims)
    assert valid["status"] == "completed" and valid["report_validation"] == "passed"
    assert "不证明根因" in valid["report"]["formal_report"]
    wrong = deepcopy(claims)
    wrong[0]["value"] = 432001
    wrong[1]["quote"] = "ETA committed for tomorrow"
    wrong[2]["value"] = "2026-08-01"
    wrong[3]["part_id"] = "PART-038"
    rejected = run.verify(wrong)
    reasons = {item["code"] for item in rejected["report"]["rejected_claims"]}
    assert {"field_value_not_supported", "quote_not_in_original_source", "eta_value_not_supported", "unsupported_component_relation"} <= reasons
    assert rejected["status"] == "partial" and rejected["report"]["formal_report"] is None


def test_arr_is_recomputed_and_top_components_and_scope_are_checked(populated):
    run = RecordedRun(populated, "开放 p0/p1 工单按直接产品组件的 ARR 前三和全局去重 ARR")
    calculation = run.call("calculate_arr_exposure", {})
    groups = sorted(calculation["groups"], key=lambda g: -int(g["arr"]))[:3]
    claims = [{"kind": "arr", "calculation_call_id": calculation["call_id"], "scope": "component", "part_id": g["part_id"],
               "amount": g["arr"], "account_ids": g["account_ids"], "currency": calculation["currency"]} for g in groups]
    claims.append({"kind": "arr", "calculation_call_id": calculation["call_id"], "scope": "global", "amount": calculation["global"]["arr"],
                   "account_ids": calculation["global"]["account_ids"], "currency": calculation["currency"]})
    assert run.verify(claims, "direct_component")["report_validation"] == "passed"
    wrong = deepcopy(claims)
    wrong[0]["amount"] = "732001"
    result = run.verify(wrong, "direct_component")
    assert result["report"]["rejected_claims"][0]["code"] == "wrong_amount"
    assert result["report"]["formal_report"] is None
    duplicate = deepcopy(claims)
    duplicate[0]["account_ids"].append("ACC-013")
    assert run.verify(duplicate, "direct_component")["report"]["rejected_claims"][0]["code"] == "wrong_account_set"
    assert "wrong_top_components" in codes(run.verify([claims[1], claims[0], *claims[2:]], "direct_component"))
    assert "report_scope_mismatch" in codes(run.verify(claims, "global"))


def test_pagination_requires_all_pages_even_when_the_last_page_is_read(populated):
    run = RecordedRun(populated, "检查查询范围")
    first = run.call("list_open_tickets", {"limit": 10})
    claim = {"kind": "field", "evidence_id": first["items"][0]["evidence"]["evidence_id"], "field": "/priority", "value": "p1"}
    assert "query_not_fully_read" in codes(run.verify([claim]))
    run.call("list_open_tickets", {"offset": 30, "limit": 10})
    assert "query_not_fully_read" in codes(run.verify([claim]))
    run.call("list_open_tickets", {"offset": 10, "limit": 10})
    run.call("list_open_tickets", {"offset": 20, "limit": 10})
    assert run.verify([claim])["report_validation"] == "passed"


def test_document_excerpt_is_source_bound_and_full_coverage_is_independent(populated):
    run = RecordedRun(populated, "查看通话原始证据")
    first = run.call("read_document", {"entity_type": "transcript", "source_id": "TRN-001", "line_count": 10})
    original = "\n".join(line.split(": ", 1)[1] for line in first["text"].splitlines())
    claim = {"kind": "quote", "evidence_id": first["evidence"]["evidence_id"], "field": None, "quote": original[:100]}
    assert "query_not_fully_read" in codes(run.verify([claim]))
    run.call("read_document", {"entity_type": "transcript", "source_id": "TRN-001", "start_line": 11, "line_count": 200})
    assert run.verify([claim])["report_validation"] == "passed"
    forged = {**claim, "quote": "The engineering team promised shipment tomorrow."}
    assert run.verify([forged])["report"]["rejected_claims"][0]["code"] == "quote_not_in_original_source"


def test_missing_document_and_exhausted_budget_never_publish_formal_report(populated):
    run = RecordedRun(populated)
    ref = run.record("account", "ACC-013")
    claim = {"kind": "field", "evidence_id": ref, "field": "/arr", "value": 432000}
    run.call("read_document", {"entity_type": "document", "source_id": "missing"})
    result = run.verify([claim])
    assert "tool_failure" in codes(result) and result["status"] == "partial"
    run.state["stop_reason"] = "tool_call_budget"
    result = run.verify([claim])
    assert "tool_call_budget" in codes(result) and result["report"]["formal_report"] is None


def test_original_record_mismatch_and_unrestricted_narrative_are_blocked(populated):
    run = RecordedRun(populated)
    ref = run.record("account", "ACC-013")
    claim = {"kind": "field", "evidence_id": ref, "field": "/arr", "value": 432000}
    assert run.verify([{**claim, "statement": "The geographic total is 761000 USD"}])["report"]["rejected_claims"][0]["code"] == "invalid_claim_schema"
    populated.execute("UPDATE enterpriseiq.records SET data=jsonb_set(data,'{arr}','999999') WHERE source_id='ACC-013' AND entity_type='account'")
    result = run.verify([claim])
    assert result["report"]["formal_report"] is None
    assert any(item["code"] == "record_source_mismatch" for item in result["report"]["rejected_claims"])


def test_region_ambiguity_and_unsupported_geography_stop_before_model(populated):
    class NeverCalled:
        def complete(self, *args, **kwargs):
            raise AssertionError("Semantic ambiguity must be handled before a paid model call")

    for question, reason in (("按区域统计 ARR 前三", "clarification_required"), ("按客户地理区域统计 ARR", "unsupported_geographic_arr")):
        result = execute_run(build_agent(NeverCalled(), BusinessTools(populated), InMemorySaver()), state=initial_state(question, "test-model"))
        assert result["stop_reason"] == reason and result["model_calls"] == result["tool_calls"] == 0
        assert result["report_validation"] == "blocked" and result["report"]["formal_report"] is None


def test_conflicting_eta_sources_are_reported_even_if_model_omits_one(populated):
    # Fault-injection documents live only in this rolled-back test transaction.
    run = RecordedRun(populated)
    for index, text in enumerate(("ISS-017 No ETA committed.", "ISS-017 ETA: 2026-08-01.")):
        source_id = f"FAULT-ETA-{index}"
        path = f"internal_docs/fault-eta-{index}.txt"
        meta_path = f"internal_docs/fault-eta-{index}.json"
        record = {"document_id": source_id, "content_file": path.rsplit('/', 1)[1], "status": "published"}
        canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        meta = json.dumps([record])
        for source_path, body in ((path, text), (meta_path, meta)):
            populated.execute("INSERT INTO enterpriseiq.source_files VALUES (%s,%s,%s,%s)", (SNAPSHOT_ID, source_path, digest(body.encode()), body))
        populated.execute("INSERT INTO enterpriseiq.records VALUES (%s,'files','document',%s,%s,'/0',%s,%s)", (SNAPSHOT_ID, source_id, meta_path, digest(canonical.encode()), Jsonb(record)))
        populated.execute("INSERT INTO enterpriseiq.document_links VALUES (%s,'files','document',%s,%s)", (SNAPSHOT_ID, source_id, path))
        result = run.call("read_document", {"entity_type": "document", "source_id": source_id})
        if index == 0:
            first_ref = result["evidence"]["evidence_id"]
    claim = {"kind": "eta", "evidence_id": first_ref, "subject_id": "ISS-017", "field": None,
             "quote": "ISS-017 No ETA committed.", "value": "uncommitted"}
    report = run.verify([claim])
    assert "source_conflict" in codes(report)
    assert report["report"]["conflicts"][0]["subject_id"] == "ISS-017"
    assert report["report"]["rejected_claims"][0]["code"] == "conflicting_eta_sources"
    assert report["status"] == "partial" and report["report"]["formal_report"] is None


def test_unexpected_tool_exception_is_a_failed_result_not_a_graph_crash():
    class BrokenTools:
        def execute(self, *args):
            raise RuntimeError("Sensitive infrastructure details must not be returned")

    from test_agent import call
    from enterpriseiq.model import Completion
    class FailureAwareModel:
        def complete(self, messages, tools, timeout):
            message = {"role": "assistant", "content": "Unavailable"}
            if not any(m["role"] == "tool" for m in messages):
                message["tool_calls"] = [call("get_record", {"entity_type": "account", "source_id": "ACC-013"}, 1)]
            return Completion(message, {}, "test-model", "test", "tool_calls" if "tool_calls" in message else "stop")

    result = execute_run(build_agent(FailureAwareModel(), BrokenTools(), InMemorySaver()), state=initial_state("Find account", "test-model", max_tool_calls=1))
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "tool_execution_error"
    assert "Sensitive infrastructure" not in json.dumps(result)


def test_report_feedback_repairs_once_and_keeps_both_attempts(populated):
    from test_agent import call
    from enterpriseiq.model import Completion

    class ReportModel:
        def __init__(self, correct):
            self.correct = correct

        def complete(self, messages, tools, timeout):
            observed = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if not observed:
                message = {"role": "assistant", "content": "Read account", "tool_calls": [call("get_record", {"entity_type": "account", "source_id": "ACC-013"}, 1)]}
            else:
                feedback = any("Independent report validation rejected" in m.get("content", "") for m in messages if m["role"] == "system")
                claims = [{"kind": "field", "evidence_id": observed[0]["item"]["evidence"]["evidence_id"], "field": "/arr", "value": 432000 if feedback and self.correct else 432001}]
                message = {"role": "assistant", "content": json.dumps({"scope": "investigation", "claims": claims})}
            return Completion(message, {"prompt_tokens": 1, "completion_tokens": 1}, "test-model", "fixture", "tool_calls" if "tool_calls" in message else "stop")

    for correct in (True, False):
        state = initial_state("Vantara 的 ARR 是多少", "test-model")
        result = execute_run(build_agent(ReportModel(correct), BusinessTools(populated), InMemorySaver()), state=state)
        assert result["model_calls"] == 3 and result["tool_calls"] == 1
        assert result["report_repair_count"] == 1 and len(result["report_attempts"]) == 2
        assert result["report_attempts"][0]["validation"]["report_validation"] == "blocked"
        assert result["report_validation"] == ("passed" if correct else "blocked")
        assert (result["report"]["formal_report"] is not None) == correct
