import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID

import psycopg

from enterpriseiq.dataset import SNAPSHOT_ID, read_bundle
from enterpriseiq.store import account_links, import_bundle, summary
from enterpriseiq.tools import BusinessTools, TOOL_SPECS
from enterpriseiq.model import DeepSeekModel, ModelError


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="EnterpriseIQ Investigation Engine Alpha")
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
    resume = sub.add_parser("resume", help="Continue an existing investigation from PostgreSQL")
    resume.add_argument("run_id", type=UUID)
    resume.add_argument("--pause-after-tool", action="store_true")
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        parser.error("Set DATABASE_URL (see .env.example); credentials are never command-line arguments")
    try:
        bundle = read_bundle(args.archive) if args.command == "import-data" else None
        with psycopg.connect(dsn) as conn:
            if bundle is not None:
                result = import_bundle(conn, bundle)
            elif args.command == "data-status":
                result = {"snapshot_id": SNAPSHOT_ID, **summary(conn)}
            elif args.command == "tool":
                result = BusinessTools(conn).execute(args.name, json.loads(args.arguments))
            elif args.command in ("investigate", "resume"):
                from enterpriseiq.agent import build_agent, execute_run, initial_state, postgres_checkpointer, result_summary, save_result

                model = DeepSeekModel()
                with postgres_checkpointer(dsn) as saver:
                    graph = build_agent(model, BusinessTools(conn), saver, pause_after_tool=args.pause_after_tool)
                    if args.command == "investigate":
                        state = initial_state(args.question, model.name, max_tool_calls=args.max_tool_calls, max_seconds=args.max_seconds)
                        investigation = execute_run(graph, state=state)
                    else:
                        investigation = execute_run(graph, run_id=str(args.run_id))
                path = save_result(investigation)
                result = {**result_summary(investigation), "output_path": str(path.resolve())}
            else:
                result = account_links(conn, args.account_id)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"Data error: {exc}\n")
    except psycopg.Error:
        # Do not echo connection strings, passwords, or database server details.
        parser.exit(1, "Database operation failed; check PostgreSQL availability and schema compatibility.\n")
    except ModelError as exc:
        parser.exit(1, f"Model configuration error: {exc.code}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
