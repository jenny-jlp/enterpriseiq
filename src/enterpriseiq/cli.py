import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
import sys
from pathlib import Path
from uuid import UUID

import psycopg

from enterpriseiq.dataset import SNAPSHOT_ID, read_bundle
from enterpriseiq.store import account_links, import_bundle, summary
from enterpriseiq.tools import BusinessTools, TOOL_SPECS
from enterpriseiq.model import ModelError


def main():
    invocation_submitted = datetime.now(timezone.utc)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="EnterpriseIQ Investigation Engine / Cases")
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("import-data", help="Verify and atomically import the audited data.zip")
    load.add_argument("archive", type=Path)
    sub.add_parser("data-status")
    links = sub.add_parser("account-links")
    links.add_argument("account_id")
    tool = sub.add_parser("tool", help="Run one typed read-only business tool")
    tool.add_argument("name", choices=TOOL_SPECS)
    tool.add_argument("--arguments", default="{}", help="JSON arguments; never SQL/SOQL/JQL")
    investigate = sub.add_parser("investigate", help="Start a checkpointed, model-driven investigation")
    investigate.add_argument("question")
    investigate.add_argument("--max-tool-calls", type=int, default=20)
    investigate.add_argument("--max-seconds", type=int, default=180)
    investigate.add_argument("--pause-after-tool", action="store_true", help="Pause at the next tool checkpoint for recovery verification")
    investigate.add_argument("--case-id", type=UUID, help="Follow-up run in an existing Case")
    resume = sub.add_parser("resume", help="Continue an existing investigation from PostgreSQL")
    resume.add_argument("run_id", type=UUID)
    resume.add_argument("--pause-after-tool", action="store_true")
    resume.add_argument("--clarification", help="Supply missing task scope without creating a new run")
    for command in (investigate,resume):
        command.add_argument('--budget-phase', choices=['smoke','freeze'], default=os.environ.get('ENTERPRISEIQ_BUDGET_PHASE','smoke'))
    sub.add_parser('budget-status', help='Read the shared cumulative M3 budget')
    metrics = sub.add_parser('run-metrics', help='Read per-invocation observed timings (trusted local operator)')
    metrics.add_argument('run_id',type=UUID)
    case = sub.add_parser("case-show", help="Show immutable report versions, sources, gaps and review history")
    case.add_argument("case_id", type=UUID)
    sub.add_parser("case-list", help="Find Cases and runs, including registration before a crash")
    review = sub.add_parser("case-review", help="Review a specific report version; actor is an audit label, not authentication")
    review.add_argument("case_id", type=UUID)
    review.add_argument("report_id", type=UUID)
    review.add_argument("action", choices=("accept", "reject", "modify"))
    review.add_argument("--actor", required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--opinion", default="", help="Unverified human opinion, separate from source facts")
    review.add_argument("--request-id", type=UUID, required=True, help="Reuse this ID when retrying the same review")
    review.add_argument("--draft", type=Path, help="Modified ReportEnvelope JSON; facts are independently revalidated")
    reconcile = sub.add_parser("case-sync", help="Reconcile registered runs with checkpoints without model execution")
    reconcile.add_argument("run_id", type=UUID)
    verify = sub.add_parser("verify-run", help="Independently validate a saved run without model calls")
    verify.add_argument("path", type=Path)
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        parser.error("Set DATABASE_URL (see .env.example); credentials are never command-line arguments")
    try:
        bundle = read_bundle(args.archive) if args.command == "import-data" else None
        with psycopg.connect(dsn) as conn:
            if bundle is not None:
                result = import_bundle(conn, bundle)
            elif args.command in ('budget-status','run-metrics'):
                from enterpriseiq.budget import BudgetLedger, budget_dsn
                with psycopg.connect(budget_dsn()) as budget_conn:
                    ledger = BudgetLedger(budget_conn)
                    ledger.initialize()
                    if args.command == 'budget-status':
                        result = {'policy':ledger.policy,**ledger.summary(),'remaining':ledger.remaining(),
                                  'cost_kind':'conservative_rate_estimate_not_actual_invoice'}
                    else:
                        from enterpriseiq.performance import execution_history
                        result = {'executions':execution_history(budget_conn,args.run_id),
                                  'model_attempts':ledger.run_attempts(args.run_id)}
            elif args.command == "data-status":
                result = {"snapshot_id": SNAPSHOT_ID, **summary(conn)}
            elif args.command == "tool":
                result = BusinessTools(conn).execute(args.name, json.loads(args.arguments))
            elif args.command == "verify-run":
                from enterpriseiq.reliability import ReportVerifier

                state = json.loads(args.path.read_text(encoding="utf-8"))
                state["answer"] = state.get("draft_answer", state.get("answer", ""))
                result = {"run_id": state["run_id"], **ReportVerifier(conn).verify(state)}
                path = Path("outputs") / f"{UUID(state['run_id'])}.validation.json"
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                result["output_path"] = str(path.resolve())
            elif args.command in ("case-show", "case-review", "case-list"):
                from enterpriseiq.cases import Cases

                cases = Cases(conn)
                cases.initialize()
                if args.command == "case-list":
                    result = cases.list_cases()
                elif args.command == "case-show":
                    result = cases.show(args.case_id)
                else:
                    draft = json.loads(args.draft.read_text(encoding="utf-8")) if args.draft else None
                    result = cases.review(args.case_id, args.report_id, args.action, args.actor, args.reason,
                                          args.request_id, opinion=args.opinion, draft=draft)
            elif args.command in ("investigate", "resume", "case-sync"):
                from enterpriseiq.agent import build_agent, execute_run, initial_state, postgres_checkpointer, result_summary, save_result
                from enterpriseiq.cases import Cases

                # Commit registration before graph execution. A separate connection projects
                # results inside the run lock; failure leaves a reconcilable registered run.
                with psycopg.connect(dsn, autocommit=True) as case_conn:
                    cases = Cases(case_conn)
                    cases.initialize()
                    from enterpriseiq.budget import BudgetedModel, budget_dsn
                    from enterpriseiq.performance import ExecutionMetrics, Timed
                    from enterpriseiq.reliability import ReportVerifier

                    metrics = None
                    model = None
                    if args.command != 'case-sync':
                        if args.command == 'investigate':
                            state = initial_state(args.question,os.environ.get('DEEPSEEK_MODEL','deepseek-flash'),
                                                  max_tool_calls=args.max_tool_calls,max_seconds=args.max_seconds)
                            run_id = state['run_id']
                        else:
                            run_id = str(args.run_id)
                        model = BudgetedModel(budget_dsn(),run_id,phase=args.budget_phase)
                        if args.command == 'investigate':
                            model.prepare(entrypoint='cli')
                        metrics = ExecutionMetrics(model.dsn,run_id,'cli','live',invocation_submitted)
                    with postgres_checkpointer(dsn) as saver, (metrics if metrics else nullcontext()):
                        graph = build_agent(Timed(model,metrics,'model_adapter') if metrics else model,
                            Timed(BusinessTools(conn),metrics,'tool') if metrics else BusinessTools(conn), saver,
                            pause_after_tool=getattr(args, "pause_after_tool", False),
                            verifier=Timed(ReportVerifier(conn),metrics,'validation') if metrics else None)
                        if args.command == "case-sync":
                            from enterpriseiq.agent import run_lock, _snapshot_result
                            with run_lock(graph, str(args.run_id)):
                                snapshot = graph.get_state({"configurable": {"thread_id": str(args.run_id)}})
                                if not snapshot.values:
                                    raise ValueError("Run has no checkpoint; it may have failed before graph startup")
                                investigation = _snapshot_result(snapshot)
                                case_id = cases.reconcile(investigation)
                        else:
                            if args.command == "investigate":
                                case_id = cases.register(state, args.case_id)
                            else:
                                row = case_conn.execute("SELECT case_id FROM investigation_cases.runs WHERE run_id=%s", (args.run_id,)).fetchone()
                                if not row:
                                    raise ValueError("Run is not registered in a Case")
                                case_id = str(row[0])
                            try:
                                investigation = (execute_run(graph,state=state,on_result=cases.reconcile) if args.command == 'investigate'
                                    else execute_run(graph,run_id=str(args.run_id),clarification=args.clarification,on_result=cases.reconcile))
                            except BaseException:
                                metrics.finish('interrupted')
                                raise
                try:
                    path = save_result(investigation)
                except BaseException:
                    if metrics:
                        metrics.finish('interrupted')
                    raise
                if metrics:
                    metrics.finish(investigation['status'])
                result = {**result_summary(investigation), "case_id": case_id, "output_path": str(path.resolve())}
            else:
                result = account_links(conn, args.account_id)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"Data error: {exc}\n")
    except psycopg.Error:
        # Do not echo connection strings, passwords, or database server details.
        parser.exit(1, "Database operation failed; check PostgreSQL availability and schema compatibility.\n")
    except ModelError as exc:
        parser.exit(1, f"Model configuration error: {exc.code}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
