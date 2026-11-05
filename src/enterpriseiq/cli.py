import argparse
import json
import os
from pathlib import Path

import psycopg

from enterpriseiq.dataset import SNAPSHOT_ID, read_bundle
from enterpriseiq.store import account_links, import_bundle, summary
from enterpriseiq.tools import BusinessTools, TOOL_SPECS


def main():
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
            else:
                result = account_links(conn, args.account_id)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"Data error: {exc}\n")
    except psycopg.Error:
        # Do not echo connection strings, passwords, or database server details.
        parser.exit(1, "Database operation failed; check PostgreSQL availability and schema compatibility.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
