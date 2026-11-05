"""Typed, parameterized business tools. No model-supplied query language or snapshot."""

from collections import defaultdict
from decimal import Decimal
import hashlib
import json
from typing import Literal

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from enterpriseiq.dataset import SNAPSHOT_ID

Entity = Literal["account", "opportunity", "ticket", "issue", "part", "transcript", "article", "document"]
SYSTEMS = {"account": "crm", "opportunity": "crm", "ticket": "crm", "issue": "pm", "part": "pm",
           "transcript": "files", "article": "kb", "document": "files"}
RECORD_COLUMNS = "r.source_system,r.entity_type,r.source_id,r.source_path,r.json_pointer,r.record_sha256,r.data"


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Page(Arguments):
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=20, ge=1, le=100)


class SearchAccounts(Page):
    name: str = Field(min_length=1, max_length=100, description="Literal substring of account name")


class GetRecord(Arguments):
    entity_type: Entity
    source_id: str = Field(min_length=1, max_length=80)


class ListTickets(Page):
    account_id: str | None = Field(default=None, max_length=80)
    part_id: str | None = Field(default=None, max_length=80)
    priorities: list[Literal["p0", "p1", "p2", "p3"]] = Field(default_factory=lambda: ["p0", "p1"], min_length=1, max_length=4)


class FindIssues(Page):
    part_ids: list[str] = Field(min_length=1, max_length=40)
    priorities: list[Literal["high", "highest", "medium", "low", "lowest"]] = Field(default_factory=lambda: ["high", "highest"], min_length=1, max_length=5)


class ListOpportunities(Page):
    account_id: str = Field(min_length=1, max_length=80)


class ListDocuments(Page):
    account_id: str | None = Field(default=None, max_length=80, description="Limits to that account's transcripts")
    query: str | None = Field(default=None, max_length=100, description="Literal substring in document title or body")


class ReadDocument(Arguments):
    entity_type: Literal["transcript", "article", "document"]
    source_id: str = Field(min_length=1, max_length=80)
    start_line: int = Field(default=1, ge=1)
    line_count: int = Field(default=100, ge=1, le=200)


class CalculateExposure(Arguments):
    priorities: list[Literal["p0", "p1", "p2", "p3"]] = Field(default_factory=lambda: ["p0", "p1"], min_length=1, max_length=4)


TOOL_SPECS = {
    "search_accounts": (SearchAccounts, "Find accounts by a literal name substring. Follow pagination before claiming all matches."),
    "get_record": (GetRecord, "Read an exact business record, its original fields and evidence reference."),
    "list_open_tickets": (ListTickets, "List open tickets, optionally by account or direct component. Default ticket priorities p0/p1."),
    "find_component_issues": (FindIssues, "Find unresolved engineering issues sharing supplied components. Candidate association, not proof of root cause. Default high/highest."),
    "list_account_opportunities": (ListOpportunities, "List non-closed opportunities for an account. ACV and explicit currency are separate from account ARR."),
    "list_documents": (ListDocuments, "Find document metadata and content matches. account_id restricts to that account's transcripts; omit it to search KB/internal documents too."),
    "read_document": (ReadDocument, "Read original document lines with snapshot-bound citations. Follow remaining lines for full coverage."),
    "calculate_arr_exposure": (CalculateExposure, "Compute full-snapshot ARR exposure from open tickets, by direct component and globally. Deterministic Decimal sums; deduplicated accounts; benchmark ARR currency unspecified. Never add group totals or ACV."),
}


def tool_definitions():
    return [{"type": "function", "function": {"name": name, "description": description,
             "parameters": model.model_json_schema()}} for name, (model, description) in TOOL_SPECS.items()]


def evidence_id(snapshot_id, kind, source_id, location):
    return "ev-" + hashlib.sha256(json.dumps([snapshot_id, kind, source_id, location], separators=(",", ":")).encode()).hexdigest()[:24]


class BusinessTools:
    def __init__(self, conn, snapshot_id=SNAPSHOT_ID):
        self.conn, self.snapshot_id = conn, snapshot_id

    def _rows(self, query, params=()):
        with self.conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(query, params).fetchall()

    def _record(self, row):
        ref = {key: row[key] for key in ("source_system", "entity_type", "source_id", "source_path", "json_pointer", "record_sha256")}
        ref.update(snapshot_id=self.snapshot_id,
                   evidence_id=evidence_id(self.snapshot_id, row["entity_type"], row["source_id"], row["json_pointer"]))
        return {"data": row["data"], "evidence": ref}

    def _page(self, where, params, args):
        base = f"FROM enterpriseiq.records r WHERE r.snapshot_id=%s AND ({where})"
        values = (self.snapshot_id, *params)
        total = self._rows("SELECT count(*) AS n " + base, values)[0]["n"]
        rows = self._rows(f"SELECT {RECORD_COLUMNS} " + base + " ORDER BY r.source_system,r.entity_type,r.source_id LIMIT %s OFFSET %s",
                          (*values, args.limit, args.offset))
        next_offset = args.offset + len(rows) if args.offset + len(rows) < total else None
        return {"items": [self._record(r) for r in rows], "total_matches": total, "returned": len(rows),
                "offset": args.offset, "next_offset": next_offset,
                "complete": args.offset == 0 and len(rows) == total}

    def execute(self, name, arguments):
        """Every public tool returns a bounded result or an explicit, sanitized error."""
        if name not in TOOL_SPECS:
            return self._error("unknown_tool", "Tool is not allowed")
        try:
            args = TOOL_SPECS[name][0].model_validate(arguments)
        except ValidationError:
            return self._error("invalid_arguments", "Arguments do not match the tool schema")
        try:
            with self.conn.transaction():
                self.conn.execute("SET LOCAL statement_timeout='10s'")
                if not self._rows("SELECT 1 FROM enterpriseiq.snapshots WHERE snapshot_id=%s", (self.snapshot_id,)):
                    return self._error("snapshot_not_found", "Requested snapshot has not been imported")
                data = getattr(self, "_" + name)(args)
                return {"status": "ok", "snapshot_id": self.snapshot_id, **data}
        except LookupError:
            return self._error("not_found", "Requested record or document does not exist")
        except psycopg.errors.QueryCanceled:
            return self._error("query_timeout", "Query exceeded its time budget", retryable=True)
        except psycopg.OperationalError:
            return self._error("connection_error", "Database connection is unavailable", retryable=True)
        except psycopg.Error:
            return self._error("query_error", "Business query failed; no complete result is available")

    def _error(self, code, message, retryable=False):
        return {"status": "error", "snapshot_id": self.snapshot_id, "complete": False,
                "error": {"code": code, "message": message, "retryable": retryable}}

    def _search_accounts(self, args):
        return self._page("r.source_system='crm' AND r.entity_type='account' AND strpos(lower(r.data->>'account_name'),lower(%s))>0", (args.name,), args)

    def _get_record(self, args):
        rows = self._rows(f"SELECT {RECORD_COLUMNS} FROM enterpriseiq.records r WHERE snapshot_id=%s AND source_system=%s AND entity_type=%s AND source_id=%s",
                          (self.snapshot_id, SYSTEMS[args.entity_type], args.entity_type, args.source_id))
        if not rows:
            raise LookupError
        return {"item": self._record(rows[0]), "complete": True}

    def _list_open_tickets(self, args):
        return self._page("""r.source_system='crm' AND r.entity_type='ticket' AND r.data->>'status'='open'
            AND r.data->>'priority'=ANY(%s) AND (%s::text IS NULL OR r.data->>'account_id'=%s)
            AND (%s::text IS NULL OR r.data->'components' ? %s)""",
            (args.priorities, args.account_id, args.account_id, args.part_id, args.part_id), args)

    def _find_component_issues(self, args):
        result = self._page("""r.source_system='pm' AND r.entity_type='issue' AND r.data->>'status'='in_progress'
            AND r.data->>'priority'=ANY(%s) AND r.data->'components' ?| %s""", (args.priorities, args.part_ids), args)
        result["relation_type"] = "shared_component"
        return result

    def _list_account_opportunities(self, args):
        return self._page("""r.source_system='crm' AND r.entity_type='opportunity' AND r.data->>'account_id'=%s
            AND r.data->>'stage' NOT IN ('closed_won','closed_lost')""", (args.account_id,), args)

    def _list_documents(self, args):
        return self._page("""r.entity_type IN ('transcript','article','document')
            AND (%s::text IS NULL OR (r.entity_type='transcript' AND r.data->>'account_id'=%s))
            AND (%s::text IS NULL OR strpos(lower(r.data->>'title'),lower(%s))>0 OR EXISTS(
                SELECT 1 FROM enterpriseiq.document_links d JOIN enterpriseiq.source_files f
                  ON f.snapshot_id=d.snapshot_id AND f.path=d.content_path
                WHERE (d.snapshot_id,d.source_system,d.entity_type,d.source_id)=
                      (r.snapshot_id,r.source_system,r.entity_type,r.source_id)
                  AND strpos(lower(f.body),lower(%s))>0))""",
            (args.account_id, args.account_id, args.query, args.query, args.query), args)

    def _read_document(self, args):
        rows = self._rows("""SELECT d.content_path,f.body,f.sha256 FROM enterpriseiq.document_links d
            JOIN enterpriseiq.source_files f ON f.snapshot_id=d.snapshot_id AND f.path=d.content_path
            WHERE d.snapshot_id=%s AND d.source_system=%s AND d.entity_type=%s AND d.source_id=%s""",
            (self.snapshot_id, SYSTEMS[args.entity_type], args.entity_type, args.source_id))
        if not rows:
            raise LookupError
        row = rows[0]
        lines = row["body"].splitlines()
        if args.start_line > len(lines):
            return self._error("invalid_range", "Start line exceeds document length")
        end = min(args.start_line + args.line_count - 1, len(lines))
        location = f"lines:{args.start_line}-{end}"
        ref = dict(snapshot_id=self.snapshot_id, source_system=SYSTEMS[args.entity_type], entity_type=args.entity_type,
                   source_id=args.source_id, source_path=row["content_path"], file_sha256=row["sha256"],
                   start_line=args.start_line, end_line=end,
                   evidence_id=evidence_id(self.snapshot_id, args.entity_type, args.source_id, location))
        return {"text": "\n".join(f"{i}: {lines[i-1]}" for i in range(args.start_line, end+1)), "evidence": ref,
                "total_lines": len(lines), "next_line": end+1 if end<len(lines) else None,
                "complete": args.start_line == 1 and end == len(lines)}

    def _calculate_arr_exposure(self, args):
        rows = self._rows("""SELECT t.source_id AS ticket_id,c.to_id AS part_id,a.source_id AS account_id,
            (a.data->>'arr')::numeric AS arr
            FROM enterpriseiq.records t JOIN enterpriseiq.relations c
              ON (c.snapshot_id,c.from_system,c.from_type,c.from_id)=(t.snapshot_id,t.source_system,t.entity_type,t.source_id)
            JOIN enterpriseiq.records a ON a.snapshot_id=t.snapshot_id AND a.source_system='crm'
              AND a.entity_type='account' AND a.source_id=t.data->>'account_id'
            WHERE t.snapshot_id=%s AND t.source_system='crm' AND t.entity_type='ticket'
              AND t.data->>'status'='open' AND t.data->>'priority'=ANY(%s)
              AND c.to_system='pm' AND c.to_type='part' AND c.relation_type='explicit_foreign_key'
            ORDER BY c.to_id,a.source_id,t.source_id""", (self.snapshot_id, args.priorities))
        by_part, global_accounts, global_tickets = defaultdict(lambda: {"accounts": {}, "tickets": set()}), {}, set()
        for row in rows:
            by_part[row["part_id"]]["accounts"][row["account_id"]] = row["arr"]
            by_part[row["part_id"]]["tickets"].add(row["ticket_id"])
            global_accounts[row["account_id"]] = row["arr"]
            global_tickets.add(row["ticket_id"])

        def metric(accounts, tickets):
            missing = sorted(key for key, value in accounts.items() if value is None)
            return {"arr": str(sum(accounts.values(), Decimal(0))) if not missing else None,
                    "account_ids": sorted(accounts), "ticket_ids": sorted(tickets), "missing_arr_account_ids": missing}

        groups = [{"part_id": part, **metric(values["accounts"], values["tickets"])} for part, values in sorted(by_part.items())]
        # Include source lineage rather than asking a model to reconstruct arithmetic.
        lineage = self._rows(f"SELECT {RECORD_COLUMNS} FROM enterpriseiq.records r WHERE r.snapshot_id=%s AND ("
                             "(r.entity_type='account' AND r.source_id=ANY(%s)) OR "
                             "(r.entity_type='ticket' AND r.source_id=ANY(%s)) OR "
                             "(r.entity_type='part' AND r.source_id=ANY(%s))) ORDER BY r.entity_type,r.source_id",
                             (self.snapshot_id, sorted(global_accounts), sorted(global_tickets), sorted(by_part)))
        names = {row["source_id"]: row["data"].get("account_name", row["data"].get("title"))
                 for row in lineage if row["entity_type"] in ("account", "part")}
        for group in groups:
            group["part_name"] = names[group["part_id"]]
            group["accounts"] = [{"account_id": key, "account_name": names[key],
                                  "arr": str(by_part[group["part_id"]]["accounts"][key])
                                         if by_part[group["part_id"]]["accounts"][key] is not None else None}
                                 for key in group["account_ids"]]
        return {"groups": groups, "global": metric(global_accounts, global_tickets),
                "currency": "UNSPECIFIED_BENCHMARK", "metric": "ARR", "grouping": "direct_component",
                "deduplication": "account_id within each group and again globally; never sum group ARR",
                "complete": all(value is not None for value in global_accounts.values()),
                "evidence": [self._record(row)["evidence"] for row in lineage]}
