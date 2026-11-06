"""Frozen M1 grading + two M2 investigations, all repetitions retained.

Evaluation only: this module and its baselines never enter business retrieval.
"""

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

import accept_m1 as m1


NEW_SCENARIOS = [
    {"id": "m2-vantara", "question": "调查 Vantara 扩展交易的阻塞与推进条件，连接客户、机会、支持工单、工程问题和通话原文；核实 ISS-017 是否有明确 ETA，列出待确认事项，不能把候选关联当作根因或把未见完成记录当作未完成。"},
    {"id": "m2-techflow", "question": "逐一调查 TechFlow Payments Inc（ACC-001）的全部开放 P0/P1 支持工单，列出工单 ID、主题、客户名称与 ARR、直接产品组件 ID 与名称、未完成工程问题的数量及代表 Issue。共享组件只表示候选关联，保留币种限制；工程明细仅需代表记录。"},
]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def invoke(args):
    process = m1.cli(args)
    if process.returncode:
        raise RuntimeError("cli_exit_"+str(process.returncode))
    return json.loads(process.stdout)


def grade_new(scenario, run, baseline):
    if scenario == "m2-vantara":
        result = m1.grade("sales-l2-b", run, baseline)
        result["checks"]["pending_questions_explicit"] = bool(run["report"].get("open_questions_unverified"))
        result["business_pass"] = result["report_pass"] and all(result["checks"].values())
        return result
    rows = [r for table in run.get("report", {}).get("tables", []) if table["view"] == "ticket_contexts" for r in table["rows"]]
    expected = {t["ticket_id"]: t for t in baseline["tickets"] if t["account_id"] == "ACC-001" and t["status"] == "open" and t["priority"] in ("p0", "p1")}
    account = baseline["accounts"]["ACC-001"]
    checks = {"all_customer_tickets": {r["ticket_id"] for r in rows} == set(expected) and len(rows) == len(expected),
              "fields_and_candidates": True, "explicit_unknown_currency": all(r["currency"] == "UNSPECIFIED_BENCHMARK" for r in rows)}
    for row in rows:
        ticket = expected.get(row["ticket_id"])
        if ticket is None:
            checks["fields_and_candidates"] = False
            continue
        correct = row["subject"] == ticket["subject"] and row["account_name"] == account["account_name"] and row["arr"] == str(account["arr"])
        correct = correct and {c["part_id"] for c in row["components"]} == set(ticket["components"])
        for component in row["components"]:
            part = component["part_id"]
            issues = {i["issue_id"] for i in baseline["issues"] if i["status"] == "in_progress" and part in i["components"]}
            correct = correct and component["part_name"] == baseline["parts"][part]["title"] and component["open_issue_count"] == len(issues)
            correct = correct and (component["representative_issue_id"] in issues if issues else component["representative_issue_id"] is None)
            correct = correct and component["relation_type"] == "shared_component"
        checks["fields_and_candidates"] &= correct
    valid = run.get("status") == "completed" and run.get("report_validation") == "passed" and run["report"].get("formal_report") is not None
    return {"report_pass": valid, "business_pass": valid and all(checks.values()), "checks": checks,
            "details": {"expected_ticket_ids": sorted(expected), "published_ticket_ids": [r["ticket_id"] for r in rows]}}


def workflow(directory, row):
    """Real CLI business lifecycle using an actual verified run, without more model calls."""
    case = invoke(["case-show", row["case_id"]])
    original = case["reports"][-1]
    draft = json.loads(original["payload"]["draft"])
    draft.setdefault("open_questions", []).append("人工待确认：客户负责人是否已收到并接受后续安排？")
    draft_path = directory / "human-edited-draft.json"
    write(draft_path, draft)
    modified = invoke(["case-review", row["case_id"], original["report_id"], "modify", "--actor", "m2-review-demo", "--reason", "保留独立待确认问题",
                       "--request-id", str(uuid4()), "--draft", str(draft_path)])
    assert modified["report_validation"] == "passed"
    request_id = str(uuid4())
    args = ["case-review", row["case_id"], modified["new_report"]["report_id"], "accept", "--actor", "m2-review-demo", "--reason", "演示审核：已核对原始字段与引用", "--request-id", request_id]
    accepted = invoke(args)
    assert invoke(args) == accepted
    invoke(["case-sync", row["run_id"]])
    shown = invoke(["case-show", row["case_id"]])
    assert len(shown["reviews"]) == 2 and len(shown["reports"]) == 2 and shown["business_status"] == "accept"
    assert shown["reports"][0] == original
    return {"case_id": row["case_id"], "run_id": row["run_id"], "reports": len(shown["reports"]), "reviews": len(shown["reviews"]),
            "business_status": shown["business_status"], "idempotent_accept": True, "original_preserved": True}


def main():
    directory = m1.ROOT / "outputs" / ("m2-final-"+datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S"))
    directory.mkdir(parents=True)
    fingerprint, baseline = m1.engine_fingerprint(), m1.raw_baseline()
    scoring_hash = hashlib.sha256((m1.ROOT / "scripts/accept_m1.py").read_bytes()).hexdigest()
    assert scoring_hash == "805e915782bab4dd42a11610b37411ee2afb1baef4ceeab726eae4c5329712f3"
    scenarios = m1.SCENARIOS + NEW_SCENARIOS
    manifest = {"started_at": m1.now(), "engine_fingerprint": fingerprint, "frozen_m1_scoring_sha256": scoring_hash,
                "m2_evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "scenarios": scenarios, "repetitions": 3, "tool_budget": 20, "model_budget": 20, "active_seconds_budget": 180,
                "policy": "All 15 runs retained. Frozen M1 scores and independent source review are separate. No selective reruns."}
    write(directory / "manifest.json", manifest)
    results = []
    for scenario in scenarios:
        for repetition in range(1, 4):
            assert m1.engine_fingerprint() == fingerprint, "Engine changed during freeze"
            start = time.perf_counter()
            row = {"scenario": scenario["id"], "repetition": repetition, "started_at": m1.now()}
            try:
                pause = scenario["id"] == "sales-l2-b" and repetition == 3
                summary = invoke(["investigate", scenario["question"]] + (["--pause-after-tool"] if pause else []))
                if pause:
                    row["paused_checkpoint"] = {k: summary[k] for k in ("run_id", "tool_calls", "model_calls", "checkpoint_id")}
                    summary = invoke(["resume", summary["run_id"]])
                run = json.loads(Path(summary["output_path"]).read_text(encoding="utf-8"))
                row.update({k: run[k] for k in ("run_id", "status", "report_validation", "model_calls", "tool_calls", "prompt_tokens", "completion_tokens", "usage_incomplete", "stop_reason")})
                row.update(case_id=summary["case_id"], output_path=summary["output_path"], total_tokens=run["prompt_tokens"]+run["completion_tokens"],
                           gap_codes=[g["code"] for g in run["report"].get("gaps", [])], action_path=[e["name"] for e in run["trace"].values() if e["type"] == "tool"])
                row.update(grade_new(scenario["id"], run, baseline) if scenario["id"].startswith("m2-") else m1.grade(scenario["id"], run, baseline))
                if pause:
                    row["checkpoint_restored"] = row["paused_checkpoint"]["tool_calls"] == 1 and Counter(row["action_path"])["search_accounts"] == 1
            except Exception as exc:
                row.update(status="failed", business_pass=False, report_pass=False, error_type=type(exc).__name__)
            row["latency_seconds"] = round(time.perf_counter()-start, 3)
            results.append(row)
            write(directory / "results.json", results)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    target = next((r for r in results if r["scenario"] == "m2-techflow" and r.get("report_pass")), None)
    if target:
        write(directory / "workflow.json", workflow(directory, target))
    manifest.update(ended_at=m1.now(), engine_unchanged=m1.engine_fingerprint() == fingerprint)
    write(directory / "manifest.json", manifest)
    print("Acceptance artifacts: "+str(directory), flush=True)


if __name__ == "__main__":
    main()
