"""M1 acceptance runner: nine fresh CLI runs, frozen engine, no selective reruns.

This is evaluation code, never available to the Agent's business-data tools.
Results and raw CLI records stay under ignored outputs/.
"""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import psycopg

from enterpriseiq.dataset import ARCHIVE_SHA256, SNAPSHOT_ID

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = [
    {"id": "eng-l1-a", "question": "逐一调查固定快照中所有开放 P1 支持工单：给出工单 ID、主题、直接产品组件（PART-ID 与名称）、同组件是否存在未完成工程问题（有则给出关联 Issue ID，无则注明未发现），以及提交工单的客户名称和 ARR。形成可核对列表，覆盖完整工单集合；共享组件只表示候选关联，不等于根因，保留币种限制。"},
    {"id": "eng-l2-a", "question": "针对固定快照中的开放 P0/P1 支持工单，按直接产品组件（产品区域）统计客户去重 ARR。列出每个有匹配工单的 PART-ID、组件名称、去重 ARR 和该组件开放 P0/P1 工单数量，按 ARR 从高到低排序；不向父组件汇总，保留币种限制。"},
    {"id": "sales-l2-b", "question": "调查 Vantara 的扩展交易：哪些事项阻止成交，需要满足什么条件才能继续推进？请连接客户、机会、支持工单、工程问题及相关通话原文，区分候选关联和明确文本证据，保留未知 ETA 与尚未证实的行动完成情况。"},
]
for scenario in SCENARIOS:
    scenario["source"] = f"https://github.com/devrev/enterprise-bench/blob/6220f818b0cf678471168d2c626a12fcce24ccf6/tasks/{scenario['id']}/instruction.md"


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def engine_fingerprint():
    paths = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "src").rglob("*.sql")) + [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def raw_baseline():
    archive = ROOT / "data/enterprise-bench-data.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == ARCHIVE_SHA256
    with ZipFile(archive) as z:
        load = lambda path: json.loads(z.read(path), parse_float=Decimal)
        return {"tickets": load("crm_json_data/tickets.json"), "issues": load("pm_json_data/issues.json"),
                "accounts": {a["account_id"]: a for a in load("crm_json_data/accounts.json")},
                "parts": {p["part_id"]: p for p in load("pm_json_data/maple_parts.json")}}


def grade(scenario, run, baseline):
    """Separate verified claim correctness from completion of the requested task."""
    verified = run.get("report", {}).get("verified_claims", [])
    claims = [item["claim"] for item in verified]
    refs = run.get("evidence", {})

    def subject(ref_id):
        return refs.get(ref_id, {}).get("source_id")

    def has_field(source_id, field, value):
        return any(c["kind"] == "field" and subject(c["evidence_id"]) == source_id and c["field"] == "/"+field and c["value"] == value for c in claims)

    if scenario == "eng-l1-a":
        rows, missing = [], {}
        open_issues = {i["issue_id"]: i for i in baseline["issues"] if i["status"] == "in_progress"}
        for ticket in baseline["tickets"]:
            if ticket["status"] != "open" or ticket["priority"] != "p1":
                continue
            key, parts = ticket["ticket_id"], ticket["components"]
            account = baseline["accounts"][ticket["account_id"]]
            checks = {
                "subject": has_field(key, "subject", ticket["subject"]),
                "component_ids": has_field(key, "components", parts),
                "component_names": all(has_field(p, "title", baseline["parts"][p]["title"]) for p in parts),
                "account_link": has_field(key, "account_id", ticket["account_id"]),
                "account_name": has_field(account["account_id"], "account_name", account["account_name"]),
                "account_arr": has_field(account["account_id"], "arr", account["arr"]),
                "open_issue_candidate": any(c["kind"] == "candidate" and subject(c["ticket_evidence_id"]) == key
                    and subject(c["issue_evidence_id"]) in open_issues
                    and c["part_id"] in parts and c["part_id"] in open_issues[subject(c["issue_evidence_id"])]["components"]
                    and has_field(subject(c["issue_evidence_id"]), "status", "in_progress") for c in claims),
            }
            if all(checks.values()):
                rows.append(key)
            else:
                missing[key] = [name for name, passed in checks.items() if not passed]
        checks = {"all_31_ticket_rows_complete": len(rows) == 31}
        details = {"expected_ticket_count": 31, "complete_ticket_rows": rows, "missing_details": missing}
    elif scenario == "eng-l2-a":
        groups = defaultdict(lambda: {"accounts": set(), "tickets": set()})
        for ticket in baseline["tickets"]:
            if ticket["status"] == "open" and ticket["priority"] in ("p0", "p1"):
                for part in ticket["components"]:
                    groups[part]["accounts"].add(ticket["account_id"])
                    groups[part]["tickets"].add(ticket["ticket_id"])
        amounts = {part: sum(Decimal(str(baseline["accounts"][a]["arr"])) for a in group["accounts"]) for part, group in groups.items()}
        ranking = sorted(groups, key=lambda part: (-amounts[part], part))
        arr = [c for c in claims if c["kind"] == "arr" and c["scope"] == "component"]
        published = {c["part_id"]: c for c in arr}
        amounts_ok = all(part in published and Decimal(published[part]["amount"]) == amounts[part]
                         and set(published[part]["account_ids"]) == groups[part]["accounts"] for part in ranking)
        # Count only quantities actually published in the verified response. Tool-only
        # ticket_ids and evaluator-computed counts do not fill missing report columns.
        statements = "\n".join(item["statement"] for item in verified)
        import re
        counts = {part: bool(re.search(re.escape(part)+r"[^\n]*?(?:工单数量|工单数|ticket count)\s*[:=：]?\s*"+str(len(groups[part]["tickets"]))+r"\b", statements, re.I)) for part in ranking}
        checks = {"all_12_component_amounts_correct": amounts_ok and set(published) == set(ranking),
                  "ranked_highest_to_lowest": [c["part_id"] for c in arr] == ranking,
                  "all_component_names_published": all(has_field(p, "title", baseline["parts"][p]["title"]) for p in ranking),
                  "all_ticket_counts_published": all(counts.values())}
        details = {"expected_components": ranking, "reported_components": list(published),
                   "missing_component_names": [p for p in ranking if not has_field(p, "title", baseline["parts"][p]["title"])],
                   "missing_ticket_counts": [p for p in ranking if not counts[p]],
                   "expected_ticket_counts": {p: len(groups[p]["tickets"]) for p in ranking}}
    else:
        texts = defaultdict(list)
        for c in claims:
            if c["kind"] == "quote":
                texts[subject(c["evidence_id"])] .append(c["quote"].lower())
            elif c["kind"] == "field" and isinstance(c["value"], str):
                texts[subject(c["evidence_id"])] .append(c["value"].lower())
        opp, transcript = " ".join(texts["OPP-006"]), " ".join(texts["TRN-001"])
        checks = {
            "opportunity_explicit_blocker": "iss-017" in opp and "blocker" in opp,
            "ticket_issue_candidate_link": any(c["kind"] == "candidate" and subject(c["ticket_evidence_id"]) == "TKT-119"
                and subject(c["issue_evidence_id"]) == "ISS-017" and c["part_id"] == "PART-010" for c in claims),
            "eta_explicitly_uncommitted": any(c["kind"] == "eta" and c["subject_id"] == "ISS-017" and c["value"] == "uncommitted" for c in claims),
            "board_deadline_and_hold_condition": "january 10" in transcript and "goes on hold" in transcript,
            "engineering_update_or_workaround_condition": ("written update" in transcript or "written eta" in transcript)
                and ("workaround" in transcript or "eta" in transcript),
            "migration_path_condition": "migration path" in transcript and ("clean" in transcript or "specific bug" in transcript),
        }
        details = {"supported_conditions": [key for key, value in checks.items() if value]}
    report_ok = run.get("status") == "completed" and run.get("report_validation") == "passed" and run.get("report", {}).get("formal_report") is not None
    return {"business_pass": report_ok and all(checks.values()), "report_pass": report_ok,
            "checks": checks, "details": details}


def checkpoint_threads():
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        return {row[0] for row in conn.execute("SELECT DISTINCT thread_id FROM investigation_checkpoint.checkpoints")}


def cli(arguments):
    return subprocess.run(["uv", "run", "--locked", "--env-file", ".env", "enterpriseiq", *arguments],
                          cwd=ROOT, capture_output=True, encoding="utf-8", timeout=240)


def main():
    os.chdir(ROOT)
    directory = ROOT / "outputs" / ("m1-final-"+datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S"))
    directory.mkdir(parents=True)
    fingerprint, baseline = engine_fingerprint(), raw_baseline()
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    manifest = {"started_at": now(), "source_commit": source_commit, "snapshot_id": SNAPSHOT_ID, "archive_sha256": ARCHIVE_SHA256,
                "engine_fingerprint": fingerprint, "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
                "tool_budget": 20, "seconds_budget": 180, "repetitions": 3, "scenarios": SCENARIOS,
                "grading_policy": "All nine fresh runs count; report validity and complete business task are separate. Missing columns/rows are failures. No selective reruns or code changes.",
                "checkpoint_check": "sales-l2-b repetition 3 pauses after first tool and resumes in a new CLI process; its latency includes both invocations."}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    results = []
    for scenario in SCENARIOS:
        for repetition in range(1, 4):
            assert engine_fingerprint() == fingerprint, "Engine changed after acceptance freeze"
            before_threads = checkpoint_threads()
            started, start = now(), time.perf_counter()
            checkpoint_test = scenario["id"] == "sales-l2-b" and repetition == 3
            row = {"scenario": scenario["id"], "repetition": repetition, "started_at": started, "checkpoint_test": checkpoint_test}
            try:
                command = ["investigate", scenario["question"]] + (["--pause-after-tool"] if checkpoint_test else [])
                process = cli(command)
                if process.returncode:
                    raise RuntimeError("cli_exit_"+str(process.returncode))
                summary = json.loads(process.stdout)
                if checkpoint_test and summary["status"] == "paused":
                    row["paused_checkpoint"] = {key: summary[key] for key in ("run_id", "checkpoint_id", "model_calls", "tool_calls", "next_nodes")}
                    process = cli(["resume", summary["run_id"]])
                    if process.returncode:
                        raise RuntimeError("resume_exit_"+str(process.returncode))
                    summary = json.loads(process.stdout)
                run = json.loads(Path(summary["output_path"]).read_text(encoding="utf-8"))
                row.update({key: run[key] for key in ("run_id", "status", "report_validation", "model_calls", "tool_calls", "prompt_tokens", "completion_tokens", "stop_reason")})
                row.update(grade(scenario["id"], run, baseline))
                row["total_tokens"] = run["prompt_tokens"]+run["completion_tokens"]
                row["metrics_complete"] = True
                row["model_api_seconds"] = round(sum(e.get("elapsed_seconds", 0) for e in run["trace"].values() if e["type"] == "model"), 3)
                row["gap_codes"] = [g["code"] for g in run.get("report", {}).get("gaps", [])]
                row["rejected_claims"] = run.get("report", {}).get("rejected_claims", [])
                row["report_repair_count"] = run.get("report_repair_count", 0)
                row["output_path"] = str(Path(summary["output_path"]).relative_to(ROOT))
                if checkpoint_test:
                    paused_id = row["paused_checkpoint"]["run_id"]
                    row["checkpoint_restored"] = run["run_id"] == paused_id and run["tool_calls"] > 1 and sum(e.get("name") == "search_accounts" for e in run["trace"].values()) == 1
            except Exception as exc:
                row.update(status="failed", report_validation="blocked", business_pass=False, report_pass=False,
                           error_type=type(exc).__name__, metrics_complete=False)
                if isinstance(exc, RuntimeError):
                    row["error_code"] = str(exc)
                row["new_checkpoint_threads"] = sorted(checkpoint_threads()-before_threads)
            row["ended_at"], row["latency_seconds"] = now(), round(time.perf_counter()-start, 3)
            results.append(row)
            (directory / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({key: row.get(key) for key in ("scenario", "repetition", "run_id", "status", "report_validation", "business_pass", "model_calls", "tool_calls", "total_tokens", "latency_seconds", "gap_codes", "error_type")}), flush=True)
    manifest["ended_at"] = now()
    manifest["engine_unchanged"] = engine_fingerprint() == fingerprint
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Acceptance artifacts: "+str(directory), flush=True)


if __name__ == "__main__":
    main()
