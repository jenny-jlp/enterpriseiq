"""Fail-closed report validation against original source files, not model prose."""

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from enterpriseiq.dataset import ARCHIVE_SHA256, SNAPSHOT_ID
from enterpriseiq.tools import TOOL_SPECS, evidence_id


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FieldClaim(ClosedModel):
    kind: Literal["field"]
    evidence_id: str
    field: str = Field(pattern=r"^/[a-z_]+$")
    value: JsonValue


class QuoteClaim(ClosedModel):
    kind: Literal["quote"]
    evidence_id: str
    field: str | None = None
    quote: str = Field(min_length=8, max_length=2000)


class CandidateClaim(ClosedModel):
    kind: Literal["candidate"]
    ticket_evidence_id: str
    issue_evidence_id: str
    part_id: str


class ReferenceClaim(ClosedModel):
    kind: Literal["reference"]
    evidence_id: str
    field: str
    target_evidence_id: str


class ExposureClaim(ClosedModel):
    kind: Literal["arr"]
    calculation_call_id: str
    scope: Literal["component", "global"]
    part_id: str | None = None
    amount: str
    account_ids: list[str] = Field(min_length=1, max_length=100)
    currency: Literal["UNSPECIFIED_BENCHMARK"]


class EtaClaim(ClosedModel):
    kind: Literal["eta"]
    evidence_id: str
    subject_id: str = Field(pattern=r"^ISS-\d+$", description="Source record source_id must equal subject_id, OR the exact quote must explicitly contain subject_id. A generic transcript ETA sentence is only a quote, not an issue-scoped eta claim.")
    field: str | None = None
    quote: str = Field(min_length=8, max_length=1000)
    value: str = Field(description="uncommitted or explicit YYYY-MM-DD")


class TableClaim(ClosedModel):
    kind: Literal["table"]
    view: Literal["ticket_contexts", "arr_exposure"]
    source_call_ids: list[str] = Field(min_length=1, max_length=20)
    top_n: int | None = Field(default=None, ge=1, le=100)


Claim = Annotated[FieldClaim | QuoteClaim | CandidateClaim | ReferenceClaim | ExposureClaim | EtaClaim | TableClaim, Field(discriminator="kind")]
CLAIM_ADAPTER = TypeAdapter(Claim)


class ReportEnvelope(ClosedModel):
    scope: Literal["direct_component", "global", "investigation"]
    claims: list[dict] = Field(min_length=1, max_length=60)
    open_questions: list[str] = Field(default_factory=list, max_length=10)


def claim_identity(value):
    normalized = dict(value)
    if normalized["kind"] == "arr":
        try:
            normalized["amount"] = str(Decimal(normalized["amount"]).normalize())
        except InvalidOperation:
            pass
        normalized["account_ids"] = sorted(normalized["account_ids"])
    if normalized["kind"] == "table":
        normalized["source_call_ids"] = sorted(normalized["source_call_ids"])
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False)


def report_instructions():
    return """Your final answer MUST be a JSON object (no markdown fences or prose outside JSON):
{"scope":"direct_component|global|investigation","claims":[...],"open_questions":[...]}
Every factual statement must be a typed claim. Do not add narrative statements, custom headings, summaries or arbitrary conclusions.
Report size limits: claims must contain 1 to 60 items; open_questions must contain at most 10 strings. Combine related pending questions into at most 10 entries without discarding material unknowns.
Supported claim item JSON schema:
""" + json.dumps(CLAIM_ADAPTER.json_schema(), ensure_ascii=False) + """
Rules: field is a slash-prefixed raw record field such as /arr, /status, /acv, /currency.
For collections and financial breakdowns use ONE table claim referencing actual tool call_ids, not dozens of copied per-row claims.
Do not repeat the table's contents as individual field/arr/candidate claims; the verifier expands their original-source proof automatically.
ticket_contexts tables reference all needed list_ticket_contexts page call_ids; arr_exposure tables reference calculate_arr_exposure.
The verifier checks every row against original sources and renders names/counts/amounts deterministically. Do not copy table values or guess field names.
Only set top_n if the user requested a top-N subset; otherwise publish the entire collection.
quote must be an EXACT original substring (no line-number prefixes); for a document field=null, for a record field=/description or another actual text field.
The quote must occur in THAT evidence_id and THAT field, not merely somewhere in the combined tool results. Keep the report minimal and non-redundant while satisfying every required output.
When requires_source_actions=true, quote ALL explicit numbered ACTION ITEMS from the read transcripts, either as individual complete original lines or the whole numbered section. Include due/status markers; do not infer that any action is completed.
An eta claim needs an explicit original quote supporting uncommitted or an explicit date; no inferred deadline.
ETA attribution is literal: either the evidence's source_id equals subject_id (the issue's own record), OR the exact quote contains subject_id.
Do NOT turn a generic transcript statement like 'I do not have an ETA' into an issue-scoped eta claim, even if nearby conversation implies the issue.
Keep such general statements as kind=quote. Do not repeat an already supported issue ETA with weaker, unscoped evidence.
arr claims MUST use the call_id from calculate_arr_exposure's result, exact account_ids and computed amount/currency.
For a top-N question include exactly the requested highest N component arr claims, plus a global arr claim if asked; ties are ordered by PART-ID.
candidate means shared component only; reference means a literal entity ID in the cited original field, never proof of causality.
Product area means a direct component. Geographic ARR aggregation is unsupported. Never invent arithmetic, exclusions or an aggregation dimension.
open_questions are unverified questions, not conclusions. Tool completeness/errors will be checked independently; do not hide gaps.
"""


def repair_feedback(state, report):
    """Identify the rejected assertion, using only the model's draft and observed refs."""
    try:
        raw = json.loads(state["answer"])
        claims = raw.get("claims", []) if isinstance(raw, dict) else []
    except (ValueError, TypeError):
        claims = []
    rejected = []
    for item in report.get("rejected_claims", []):
        index = item["index"]
        claim = claims[index] if isinstance(claims, list) and isinstance(index, int) and 0 <= index < len(claims) else None
        details = {**item, "rejected_claim": claim}
        if item["code"] == "quote_not_in_original_source":
            details["explanation"] = "This exact quote is absent from the referenced evidence_id/field. Correct the source using actual tool results, or remove this unsupported optional claim. Do not repeat this unchanged claim."
        if isinstance(claim, dict):
            details["referenced_source_id"] = state.get("evidence", {}).get(claim.get("evidence_id"), {}).get("source_id")
        rejected.append(details)
    return {"gaps": report["gaps"], "rejected_assertions": rejected,
            "correction_rule": "Do not return any rejected assertion unchanged. Correct it using observed sources or omit an unsupported optional claim; preserve all required task outputs and all observed conflicts. Never fabricate a quote or evidence ID."}


from enterpriseiq.tasks import semantic_scope, task_requirements, task_contract, normalize_question


def source_action_items(text):
    """Only explicit labelled lists; no inferred commitments or semantic extraction."""
    active, items = False, []
    for line_number, line in enumerate(text.splitlines(), 1):
        if re.match(r"^\s*(?:ACTION ITEMS|行动项|后续行动)", line, re.I):
            active = True
            continue
        if active:
            match = re.match(r"^\s*\d+[.)、]\s+(.+)$", line)
            if match:
                items.append((line_number, match[1]))
            elif line.strip():
                active = False
    return items


def tool_results(state):
    calls = {}
    for message in state.get("messages", []):
        if message.get("role") == "assistant":
            for call in message.get("tool_calls", []):
                try:
                    arguments = json.loads(call["function"]["arguments"])
                except (ValueError, TypeError):
                    arguments = None
                if call["id"] in calls:
                    calls["duplicate:"+call["id"]] = {"name": "invalid", "arguments": None,
                        "result": {"status": "error", "error": {"code": "duplicate_call_id"}}}
                calls[call["id"]] = {"name": call["function"]["name"], "arguments": arguments}
        elif message.get("role") == "tool":
            call_id = message["tool_call_id"]
            if call_id in calls:
                try:
                    calls[call_id]["result"] = json.loads(message["content"])
                except (ValueError, TypeError):
                    calls[call_id]["result"] = {"status": "error", "error": {"code": "invalid_tool_result"}}
    return calls


def coverage_gaps(state):
    groups, gaps = {}, []
    for call_id, call in tool_results(state).items():
        result = call.get("result")
        if result is None:
            gaps.append({"code": "unexecuted_tool", "call_id": call_id})
            continue
        if result.get("status") != "ok":
            gaps.append({"code": "tool_failure", "call_id": call_id, "error": result.get("error", {}).get("code", "unknown")})
            continue
        try:
            normalized = TOOL_SPECS[call["name"]][0].model_validate(call["arguments"]).model_dump()
        except (KeyError, ValidationError):
            gaps.append({"code": "invalid_query_scope", "call_id": call_id})
            continue
        if "total_matches" in result or "total_lines" in result:
            document = "total_lines" in result
            scope = {key: value for key, value in normalized.items() if key not in ("offset", "limit", "start_line", "line_count")}
            key = json.dumps([call["name"], scope], sort_keys=True)
            group = groups.setdefault(key, {"totals": set(), "intervals": [], "ids": set(), "call_ids": [], "document": document})
            group["call_ids"].append(call_id)
            total = result["total_lines"] if document else result["total_matches"]
            group["totals"].add(total)
            if document:
                ref = result.get("evidence", {})
                start, end = ref.get("start_line", 0)-1, ref.get("end_line", 0)
                if start != normalized["start_line"]-1 or end != min(total, start+normalized["line_count"]):
                    gaps.append({"code": "document_range_mismatch", "call_id": call_id})
            else:
                start = result.get("offset", -1)
                items = result.get("items", [])
                end = start+len(items)
                group["ids"].update(item.get("evidence", {}).get("evidence_id") for item in items)
                if result.get("returned") != len(items):
                    gaps.append({"code": "invalid_page_count", "call_id": call_id})
                if start != normalized["offset"] or len(items) > normalized["limit"]:
                    gaps.append({"code": "query_range_mismatch", "call_id": call_id})
            if not isinstance(total, int) or total < 0 or start < 0 or end < start or end > total:
                gaps.append({"code": "invalid_page_range", "call_id": call_id})
            group["intervals"].append((start, end))
        elif not result.get("complete", False):
            gaps.append({"code": "incomplete_result", "call_id": call_id})
    for group in groups.values():
        covered = 0
        for start, end in sorted(group["intervals"]):
            if start > covered:
                break
            covered = max(covered, end)
        total = next(iter(group["totals"])) if len(group["totals"]) == 1 else None
        if total is None or covered != total or (not group["document"] and len(group["ids"]-{None}) != total):
            gaps.append({"code": "query_not_fully_read", "call_ids": group["call_ids"], "total": total, "covered_prefix": covered})
    return gaps


class UnsupportedClaim(ValueError):
    pass


def sha(value):
    return hashlib.sha256(value).hexdigest()


def eta_values(text):
    values = set()
    if re.search(r"\bno\s+(?:engineering\s+)?(?:committed\s+)?ETA\b|\bETA\s+(?:is\s+)?not\s+committed\b|未承诺.{0,10}(?:ETA|修复日期)", text, re.I):
        values.add("uncommitted")
    values.update(re.findall(r"\bETA\s*(?:is\s+|committed\s*[:=]?\s*|[:=]\s*)(\d{4}-\d{2}-\d{2})", text, re.I))
    return values


def scoped_eta_values(ref, text, subject):
    values = set()
    for clause in re.split(r"[\n。;；]|(?<=[.!?])\s+", text):
        mentioned = set(re.findall(r"\bISS-\d+\b", clause))
        own_record = ref["entity_type"] == "issue" and ref["source_id"] == subject
        if mentioned == {subject} or (own_record and not mentioned):
            values.update(eta_values(clause))
    return values


class ReportVerifier:
    def __init__(self, conn):
        self.conn = conn

    def _file(self, path):
        if path not in self.files:
            row = self.conn.execute("SELECT body,sha256 FROM enterpriseiq.source_files WHERE snapshot_id=%s AND path=%s", (SNAPSHOT_ID, path)).fetchone()
            if not row or sha(row[0].encode("utf-8")) != row[1]:
                raise UnsupportedClaim("source_file_integrity")
            self.files[path] = row[0], row[1]
        return self.files[path]

    def _json(self, path):
        if path not in self.json_files:
            self.json_files[path] = json.loads(self._file(path)[0], parse_float=Decimal)
        return self.json_files[path]

    def _resolve(self, ref_id):
        if ref_id in self.resolved:
            return self.resolved[ref_id]
        ref = self.state.get("evidence", {}).get(ref_id)
        if not ref or ref.get("snapshot_id") != SNAPSHOT_ID or ref != self.offered.get(ref_id):
            raise UnsupportedClaim("citation_not_from_successful_tool")
        path = ref["source_path"]
        body, file_sha = self._file(path)
        if "json_pointer" in ref:
            try:
                raw = self._json(path)[int(ref["json_pointer"][1:])]
            except (KeyError, IndexError, ValueError, TypeError):
                raise UnsupportedClaim("invalid_record_position") from None
            row = self.conn.execute("""SELECT data,source_path,json_pointer,record_sha256 FROM enterpriseiq.records
                WHERE snapshot_id=%s AND source_system=%s AND entity_type=%s AND source_id=%s""",
                (SNAPSHOT_ID, ref["source_system"], ref["entity_type"], ref["source_id"])).fetchone()
            # Original current corpus has integer monetary fields. Compare JSON values separately
            # so Decimal parsing does not alter numeric meaning in future decimal-valued inputs.
            if path not in self.plain_json_files:
                self.plain_json_files[path] = json.loads(body)
            original = self.plain_json_files[path][int(ref["json_pointer"][1:])]
            original_canonical = json.dumps(original, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            if not row or row[0] != original or row[1:] != (path, ref["json_pointer"], ref["record_sha256"]) or sha(original_canonical.encode()) != ref["record_sha256"]:
                raise UnsupportedClaim("record_source_mismatch")
            if evidence_id(SNAPSHOT_ID, ref["entity_type"], ref["source_id"], ref["json_pointer"]) != ref_id:
                raise UnsupportedClaim("citation_identity_mismatch")
            content = raw
        else:
            link = self.conn.execute("""SELECT content_path FROM enterpriseiq.document_links WHERE snapshot_id=%s
                AND source_system=%s AND entity_type=%s AND source_id=%s""",
                (SNAPSHOT_ID, ref["source_system"], ref["entity_type"], ref["source_id"])).fetchone()
            start, end = ref["start_line"], ref["end_line"]
            lines = body.splitlines()
            if not link or link[0] != path or ref.get("file_sha256") != file_sha or not 1 <= start <= end <= len(lines):
                raise UnsupportedClaim("document_source_mismatch")
            if evidence_id(SNAPSHOT_ID, ref["entity_type"], ref["source_id"], f"lines:{start}-{end}") != ref_id:
                raise UnsupportedClaim("citation_identity_mismatch")
            content = "\n".join(lines[start-1:end])
        self.resolved[ref_id] = ref, content
        return ref, content

    def _text(self, ref_id, field):
        ref, content = self._resolve(ref_id)
        if isinstance(content, dict):
            if not field or not re.fullmatch(r"/[a-z_]+", field):
                raise UnsupportedClaim("invalid_text_field")
            content = content.get(field[1:])
        elif field is not None:
            raise UnsupportedClaim("document_has_no_record_field")
        if not isinstance(content, str):
            raise UnsupportedClaim("source_is_not_text")
        return ref, content

    def _exposure(self, claim):
        call = self.calls.get(claim.calculation_call_id, {})
        result = call.get("result", {})
        if call.get("name") != "calculate_arr_exposure" or result.get("status") != "ok" or result.get("complete") is not True:
            raise UnsupportedClaim("calculation_not_complete")
        priorities = TOOL_SPECS["calculate_arr_exposure"][0].model_validate(call["arguments"]).priorities
        if self.expected_priorities is not None and set(priorities) != self.expected_priorities:
            raise UnsupportedClaim("wrong_priority_scope")
        accounts = {r["account_id"]: r for r in self._json("crm_json_data/accounts.json")}
        all_tickets = self._json("crm_json_data/tickets.json")
        selected = [t for t in all_tickets if t["status"] == "open" and t["priority"] in priorities
                    and (claim.scope == "global" or claim.part_id in t["components"])]
        if claim.scope == "global" and claim.part_id is not None:
            raise UnsupportedClaim("invalid_global_group")
        if claim.scope == "component" and claim.part_id is None:
            raise UnsupportedClaim("missing_component")
        ids = {t["account_id"] for t in selected}
        if set(claim.account_ids) != ids or len(claim.account_ids) != len(ids):
            raise UnsupportedClaim("wrong_account_set")
        if any(accounts[key].get("arr") is None for key in ids):
            raise UnsupportedClaim("missing_arr")
        amount = sum((Decimal(str(accounts[key]["arr"])) for key in ids), Decimal(0))
        try:
            proposed = Decimal(claim.amount)
        except InvalidOperation:
            raise UnsupportedClaim("invalid_amount") from None
        if proposed != amount:
            raise UnsupportedClaim("wrong_amount")
        stored = result.get("global") if claim.scope == "global" else next((g for g in result.get("groups", []) if g["part_id"] == claim.part_id), None)
        if not stored or Decimal(stored["arr"]) != amount or set(stored["account_ids"]) != ids:
            raise UnsupportedClaim("calculation_source_mismatch")
        # Every source lineage reference from this calculation must resolve to original bytes.
        for ref in result.get("evidence", []):
            self._resolve(ref["evidence_id"])
        label = "全局客户去重" if claim.scope == "global" else f"直接产品组件 {claim.part_id} 的客户去重"
        return f"{label} ARR = {amount}；客户 {', '.join(sorted(ids))}；币种 UNSPECIFIED_BENCHMARK。"

    def _claim(self, claim):
        if isinstance(claim, ExposureClaim):
            return self._exposure(claim)
        if isinstance(claim, CandidateClaim):
            ticket_ref, ticket = self._resolve(claim.ticket_evidence_id)
            issue_ref, issue = self._resolve(claim.issue_evidence_id)
            if ticket_ref["entity_type"] != "ticket" or issue_ref["entity_type"] != "issue" or claim.part_id not in ticket.get("components", []) or claim.part_id not in issue.get("components", []):
                raise UnsupportedClaim("unsupported_component_relation")
            return f"{ticket_ref['source_id']} 与 {issue_ref['source_id']} 共享组件 {claim.part_id}：候选关联，不证明根因。"
        if isinstance(claim, FieldClaim):
            ref, raw = self._resolve(claim.evidence_id)
            if not isinstance(raw, dict) or claim.field[1:] not in raw:
                raise UnsupportedClaim("missing_source_field")
            actual = raw[claim.field[1:]]
            numeric = isinstance(actual, (int, float, Decimal)) and isinstance(claim.value, (int, float, Decimal)) and not isinstance(actual, bool) and not isinstance(claim.value, bool)
            equal = Decimal(str(actual)) == Decimal(str(claim.value)) if numeric else actual == claim.value
            if not equal or isinstance(actual, bool) != isinstance(claim.value, bool):
                raise UnsupportedClaim("field_value_not_supported")
            currency = "（账户 ARR 币种未指定）" if claim.field == "/arr" else f"（币种 {raw.get('currency', '未指定')}）" if claim.field == "/acv" else ""
            return f"{ref['source_id']}{claim.field} = {json.dumps(claim.value, ensure_ascii=False)}{currency}。"
        if isinstance(claim, ReferenceClaim):
            ref, text = self._text(claim.evidence_id, claim.field)
            target, _ = self._resolve(claim.target_evidence_id)
            if not re.search(r"(?<![A-Za-z0-9-])"+re.escape(target["source_id"])+r"(?![A-Za-z0-9-])", text):
                raise UnsupportedClaim("explicit_reference_not_found")
            return f"{ref['source_id']}{claim.field} 原文明确提及 {target['source_id']}；不自动推断因果。"
        ref, text = self._text(claim.evidence_id, claim.field)
        if claim.quote not in text:
            raise UnsupportedClaim("quote_not_in_original_source")
        if isinstance(claim, EtaClaim):
            if claim.value != "uncommitted":
                try:
                    date.fromisoformat(claim.value)
                except ValueError:
                    raise UnsupportedClaim("invalid_eta_date") from None
            if ref["source_id"] != claim.subject_id and not re.search(r"\b"+re.escape(claim.subject_id)+r"\b", claim.quote):
                raise UnsupportedClaim("eta_subject_not_supported")
            if claim.value not in scoped_eta_values(ref, claim.quote, claim.subject_id):
                raise UnsupportedClaim("eta_value_not_supported")
            return f"{ref['source_id']} 对 {claim.subject_id} 的 ETA 来源观测为 {claim.value}；原文：{claim.quote}"
        return f"{ref['source_id']} 来源原文：{claim.quote}"

    def _proof(self, claim):
        """Expose original atomic proof as well as a rendered collection, without LLM copying."""
        statement = self._claim(claim)
        raw = claim.model_dump()
        refs = [v for k, v in raw.items() if k.endswith("evidence_id")]
        if isinstance(claim, ExposureClaim):
            refs = [r["evidence_id"] for r in self.calls[claim.calculation_call_id]["result"]["evidence"]]
        return {"claim": raw, "statement": statement, "evidence_ids": refs}

    def _source_ref(self, kind, source_id):
        for ref_id, ref in self.offered.items():
            if ref.get("entity_type") == kind and ref.get("source_id") == source_id:
                self._resolve(ref_id)
                return ref_id
        raise UnsupportedClaim("missing_collection_source")

    def _table(self, claim):
        if len(set(claim.source_call_ids)) != len(claim.source_call_ids):
            raise UnsupportedClaim("duplicate_collection_call")
        calls = [self.calls.get(key, {}) for key in claim.source_call_ids]
        if any(c.get("result", {}).get("status") != "ok" for c in calls):
            raise UnsupportedClaim("collection_call_not_successful")
        if claim.top_n != self.contract["top_n"]:
            raise UnsupportedClaim("collection_subset_not_requested")
        if claim.view == "arr_exposure":
            if len(calls) != 1 or calls[0].get("name") != "calculate_arr_exposure" or not calls[0]["result"].get("complete"):
                raise UnsupportedClaim("calculation_not_complete")
            result = calls[0]["result"]
            priorities = TOOL_SPECS["calculate_arr_exposure"][0].model_validate(calls[0]["arguments"]).priorities
            if self.expected_priorities is not None and set(priorities) != self.expected_priorities:
                raise UnsupportedClaim("wrong_priority_scope")
            parts = {p["part_id"]: p for p in self._json("pm_json_data/maple_parts.json")}
            tickets = [t for t in self._json("crm_json_data/tickets.json") if t["status"] == "open" and t["priority"] in priorities]
            expected_parts = {p for t in tickets for p in t["components"]}
            groups = result.get("groups", [])
            if len(groups) != len(expected_parts) or {g["part_id"] for g in groups} != expected_parts:
                raise UnsupportedClaim("missing_collection_rows")
            groups = sorted(groups, key=lambda g: (-Decimal(g["arr"]), g["part_id"]))
            if claim.top_n is not None:
                groups = groups[:claim.top_n]
            rows, proofs = [], []
            for group in groups:
                part_id = group["part_id"]
                ticket_count = len({t["ticket_id"] for t in tickets if part_id in t["components"]})
                if group.get("part_name") != parts[part_id]["title"] or group.get("ticket_count") != ticket_count:
                    raise UnsupportedClaim("derived_collection_value_mismatch")
                amount = ExposureClaim(kind="arr", calculation_call_id=claim.source_call_ids[0], scope="component",
                    part_id=part_id, amount=group["arr"], account_ids=group["account_ids"], currency=result["currency"])
                proof = self._proof(amount)
                proof["statement"] += f" 组件名称={parts[part_id]['title']}；开放工单数量 = {ticket_count}；"
                proofs.append(proof)
                proofs.append(self._proof(FieldClaim(kind="field", evidence_id=self._source_ref("part", part_id), field="/title", value=parts[part_id]["title"])))
                rows.append({"part_id": part_id, "part_name": parts[part_id]["title"], "arr": group["arr"],
                    "ticket_count": ticket_count, "account_ids": group["account_ids"], "currency": result["currency"]})
            output = {"view": claim.view, "columns": ["part_id", "part_name", "arr", "ticket_count", "account_ids", "currency"], "rows": rows,
                      "source_call_ids": claim.source_call_ids}
            if re.search(r"全局|global", self.state["question"], re.I):
                proofs.append(self._proof(ExposureClaim(kind="arr", calculation_call_id=claim.source_call_ids[0], scope="global",
                    amount=result["global"]["arr"], account_ids=result["global"]["account_ids"], currency=result["currency"])))
                output["global"] = result["global"]
            return output, proofs

        if any(c.get("name") != "list_ticket_contexts" for c in calls) or claim.top_n is not None:
            raise UnsupportedClaim("wrong_collection_source")
        specs = [TOOL_SPECS["list_ticket_contexts"][0].model_validate(c["arguments"]) for c in calls]
        filters = [{k: v for k, v in s.model_dump().items() if k not in ("offset", "limit")} for s in specs]
        if any(f != filters[0] for f in filters):
            raise UnsupportedClaim("mixed_collection_scope")
        spec = specs[0]
        if self.expected_priorities is not None and set(spec.priorities) != self.expected_priorities:
            raise UnsupportedClaim("wrong_priority_scope")
        accounts = {a["account_id"]: a for a in self._json("crm_json_data/accounts.json")}
        if "ticket_contexts" in self.contract["required_tables"]:
            q = self.state["question"]
            mentioned = set(re.findall(r"(?<![A-Za-z0-9])ACC-\d+(?![A-Za-z0-9])", q))
            mentioned.update(a for a in accounts if accounts[a]["account_name"].casefold() in q.casefold())
            named_parts = set(re.findall(r"(?<![A-Za-z0-9])PART-\d+(?![A-Za-z0-9])", q))
            if (set([spec.account_id]) if spec.account_id else set()) != mentioned or (set([spec.part_id]) if spec.part_id else set()) != named_parts:
                raise UnsupportedClaim("collection_scope_narrowed")
        selected = {t["ticket_id"]: t for t in self._json("crm_json_data/tickets.json") if t["status"] == "open" and t["priority"] in spec.priorities
                    and (spec.account_id is None or t["account_id"] == spec.account_id)
                    and (spec.part_id is None or spec.part_id in t["components"])}
        by_id = {}
        for call in calls:
            result = call["result"]
            if result.get("total_matches") != len(selected) or result.get("missing_context_ticket_ids") or result.get("issue_scope") != "exact_existence_and_count_with_representative":
                raise UnsupportedClaim("incomplete_collection")
            for item in result["items"]:
                key = item["data"]["ticket_id"]
                if key in by_id and by_id[key] != item["data"]:
                    raise UnsupportedClaim("inconsistent_collection_row")
                by_id[key] = item["data"]
        if set(by_id) != set(selected):
            raise UnsupportedClaim("missing_collection_rows")
        parts = {p["part_id"]: p for p in self._json("pm_json_data/maple_parts.json")}
        issues_by_part = defaultdict(list)
        for issue in self._json("pm_json_data/issues.json"):
            if issue["status"] == "in_progress":
                for part in set(issue["components"]):
                    issues_by_part[part].append(issue)
        priority_rank = {value: rank for rank, value in enumerate(("highest", "high", "medium", "low", "lowest"))}
        for issues in issues_by_part.values():
            issues.sort(key=lambda i: (priority_rank.get(i["priority"], 4), i["issue_id"]))
        rows, proofs = [], []
        for key, ticket in sorted(selected.items()):
            account = accounts[ticket["account_id"]]
            if account.get("arr") is None:
                raise UnsupportedClaim("missing_arr")
            expected_components = []
            ticket_ref = self._source_ref("ticket", key)
            for field, value in (("subject", ticket["subject"]), ("components", ticket["components"]), ("account_id", ticket["account_id"])):
                proofs.append(self._proof(FieldClaim(kind="field", evidence_id=ticket_ref, field="/"+field, value=value)))
            for field in ("account_name", "arr"):
                proofs.append(self._proof(FieldClaim(kind="field", evidence_id=self._source_ref("account", account["account_id"]), field="/"+field, value=account[field])))
            for part_id in ticket["components"]:
                matches = issues_by_part[part_id]
                witness = matches[0] if matches else None
                expected_components.append({"part_id": part_id, "part_name": parts[part_id]["title"], "open_issue_count": len(matches),
                    "representative_issue_id": witness["issue_id"] if witness else None, "representative_issue_status": "in_progress" if witness else None,
                    "enumeration_complete": len(matches) <= 1, "existence_and_count_complete": True, "relation_type": "shared_component"})
                proofs.append(self._proof(FieldClaim(kind="field", evidence_id=self._source_ref("part", part_id), field="/title", value=parts[part_id]["title"])))
                if witness:
                    issue_ref = self._source_ref("issue", witness["issue_id"])
                    proofs.append(self._proof(FieldClaim(kind="field", evidence_id=issue_ref, field="/status", value="in_progress")))
                    proofs.append(self._proof(CandidateClaim(kind="candidate", ticket_evidence_id=ticket_ref, issue_evidence_id=issue_ref, part_id=part_id)))
            expected = {"ticket_id": key, "subject": ticket["subject"], "account_id": account["account_id"], "account_name": account["account_name"],
                        "arr": str(account["arr"]), "components": expected_components}
            if by_id[key] != expected:
                raise UnsupportedClaim("derived_collection_value_mismatch")
            if re.search(r"(?:全部|所有).{0,8}工程问题|all\s+(?:open\s+)?engineering issues", self.state["question"], re.I) and any(not c["enumeration_complete"] for c in expected_components):
                raise UnsupportedClaim("issue_enumeration_not_complete")
            rows.append({**expected, "currency": "UNSPECIFIED_BENCHMARK"})
        return {"view": claim.view, "columns": ["ticket_id", "subject", "components", "account_id", "account_name", "arr", "currency"],
                "rows": rows, "source_call_ids": claim.source_call_ids,
                "issue_scope": "exact_existence_and_count_with_representative"}, proofs

    def _conflicts(self):
        facts = defaultdict(lambda: defaultdict(set))
        for ref_id, (ref, content) in self.resolved.items():
            if isinstance(content, dict):
                text = "\n".join(value for key, value in content.items() if isinstance(value, str) and key in ("description", "next_step", "body"))
                if ref["entity_type"] == "issue":
                    for value in scoped_eta_values(ref, text, ref["source_id"]):
                        facts[ref["source_id"]][value].add(ref_id)
            else:
                text = content
            # Only explicit issue-scoped clauses; general date mentions are not treated as ETAs.
            for subject in set(re.findall(r"\bISS-\d+\b", text)):
                for value in scoped_eta_values(ref, text, subject):
                    facts[subject][value].add(ref_id)
        return [{"code": "source_conflict", "subject_id": subject, "predicate": "eta",
                 "values": {value: sorted(refs) for value, refs in values.items()}}
                for subject, values in facts.items() if len(values) > 1]

    def _requirements(self, scope, claims):
        gaps = []
        typed = [item["claim"] for item in claims]
        arr = [claim for claim in typed if claim["kind"] == "arr"]
        question = normalize_question(self.state["question"])
        supplied_tables = {c["view"] for c in typed if c["kind"] == "table"}
        for required in self.contract["required_tables"]:
            if required not in supplied_tables:
                gaps.append({"code": "required_table_missing", "view": required})
        cited = {self.offered[ref].get("source_id") for c in claims for ref in c.get("evidence_ids", []) if ref in self.offered}
        for entity in self.contract["entity_ids"]:
            if entity not in cited:
                gaps.append({"code": "requested_entity_not_supported", "entity_id": entity})
        if self.contract["requires_source_actions"]:
            inspected = set()
            for ref_id, (ref, text) in self.resolved.items():
                if ref.get("entity_type") != "transcript" or not isinstance(text, str) or ref["source_path"] in inspected:
                    continue
                inspected.add(ref["source_path"])
                original = self._file(ref["source_path"])[0]
                quotes = [c["quote"] for c in typed if c["kind"] == "quote" and c["field"] is None
                          and self.offered.get(c["evidence_id"], {}).get("source_path") == ref["source_path"]]
                missing = [line for line, action in source_action_items(original)
                           if not any(action in quote for quote in quotes)]
                if missing:
                    gaps.append({"code": "source_actions_not_covered", "source_id": ref["source_id"],
                                 "missing_lines": missing, "evidence_id": ref_id})
        if scope == "global" and not any(c["scope"] == "global" for c in arr):
            gaps.append({"code": "missing_global_calculation"})
        if scope == "direct_component":
            component = [c for c in arr if c["scope"] == "component"]
            if not component:
                gaps.append({"code": "missing_component_calculation"})
            count = self.contract["top_n"]
            if count and component:
                priorities = self.expected_priorities
                if priorities is None:
                    priorities = set(TOOL_SPECS["calculate_arr_exposure"][0].model_validate(
                        self.calls[component[0]["calculation_call_id"]]["arguments"]).priorities)
                groups = defaultdict(set)
                for ticket in self._json("crm_json_data/tickets.json"):
                    if ticket["status"] == "open" and ticket["priority"] in priorities:
                        for part in ticket["components"]:
                            groups[part].add(ticket["account_id"])
                accounts = {a["account_id"]: a for a in self._json("crm_json_data/accounts.json")}
                ranking = sorted(groups, key=lambda part: (-sum(Decimal(str(accounts[a]["arr"])) for a in groups[part]), part))
                if [c["part_id"] for c in component] != ranking[:count]:
                    gaps.append({"code": "wrong_top_components", "expected_part_ids": ranking[:count]})
            if re.search(r"全局|global", question, re.I) and not any(c["scope"] == "global" for c in arr):
                gaps.append({"code": "missing_global_calculation"})
        if scope == "investigation":
            if re.search(r"通话|会议|QBR|transcript", question, re.I) and not any(
                call["name"] == "read_document" and call.get("result", {}).get("status") == "ok" for call in self.calls.values()
            ):
                gaps.append({"code": "required_original_document_not_read"})
            if re.search(r"ETA|承诺.*(?:日期|修复)", question, re.I) and not any(c["kind"] == "eta" for c in typed):
                gaps.append({"code": "eta_not_explicitly_supported"})
        return gaps

    def verify(self, state):
        self.state, self.files, self.json_files, self.resolved = state, {}, {}, {}
        self.plain_json_files = {}
        self.calls = tool_results(state)
        self.offered = {}

        def collect(value):
            if isinstance(value, dict):
                if "evidence_id" in value:
                    self.offered[value["evidence_id"]] = value
                else:
                    for child in value.values():
                        collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        for call in self.calls.values():
            if call.get("result", {}).get("status") == "ok":
                collect(call["result"])
        contract = task_contract(state["question"])
        scope = contract["scope"]
        self.contract = contract
        self.expected_priorities = set(self.contract["priorities"]) if self.contract["priorities"] else None
        gaps = coverage_gaps(state)
        if state.get("task_contract", contract) != contract:
            gaps.append({"code": "task_contract_mismatch"})
        if state.get("stop_reason") != "answered":
            gaps.append({"code": state.get("stop_reason") or "investigation_not_finished"})
        if state.get("errors"):
            gaps.append({"code": "execution_errors", "errors": state["errors"]})
        if scope in ("clarification_required", "unsupported_geographic_arr"):
            gaps.append({"code": scope})
        row = self.conn.execute("SELECT archive_sha256 FROM enterpriseiq.snapshots WHERE snapshot_id=%s", (SNAPSHOT_ID,)).fetchone()
        if state.get("snapshot_id") != SNAPSHOT_ID or not row or row[0] != ARCHIVE_SHA256:
            gaps.append({"code": "snapshot_integrity"})
        claims, rejected, open_questions, tables = [], [], [], []
        declared_tables = set()
        try:
            draft = ReportEnvelope.model_validate(json.loads(state.get("answer", "")))
            open_questions = draft.open_questions
            if draft.scope != scope or scope in ("clarification_required", "unsupported_geographic_arr"):
                gaps.append({"code": "report_scope_mismatch", "declared": draft.scope, "expected": scope})
            else:
                for index, raw in enumerate(draft.claims):
                    try:
                        claim = CLAIM_ADAPTER.validate_python(raw)
                        if isinstance(claim, TableClaim):
                            identity = claim_identity(claim.model_dump())
                            if identity in declared_tables:
                                continue
                            table, proofs = self._table(claim)
                            declared_tables.add(identity)
                            statement = render_table(table)
                            tables.append(table)
                            claims.append({"index": index, "claim": claim.model_dump(), "statement": statement,
                                           "evidence_ids": sorted({ref for p in proofs for ref in p["evidence_ids"]})})
                            claims.extend({"index": index, **proof, "from_table": True} for proof in proofs)
                            continue
                        statement = self._claim(claim)
                        if isinstance(claim, ExposureClaim):
                            refs = [ref["evidence_id"] for ref in self.calls[claim.calculation_call_id]["result"].get("evidence", [])]
                            citation = f" [计算 {claim.calculation_call_id}；原始来源见 evidence_ids]"
                        else:
                            refs = [value for key, value in claim.model_dump().items() if key.endswith("evidence_id")]
                            citation = " " + "".join(f"[{ref}]" for ref in refs)
                        claims.append({"index": index, "claim": claim.model_dump(), "statement": statement+citation, "evidence_ids": refs})
                    except (ValidationError, UnsupportedClaim, KeyError, TypeError, InvalidOperation) as exc:
                        code = str(exc) if isinstance(exc, UnsupportedClaim) else "invalid_claim_schema"
                        rejected_item = {"index": index, "code": code}
                        if code == "eta_subject_not_supported":
                            rejected_item["explanation"] = "The exact quote does not contain subject_id and this source is not that issue's record. Keep general statements as kind=quote; do not repeat this unscoped eta."
                        rejected.append(rejected_item)
        except ValidationError as exc:
            gaps.append({"code": "unstructured_or_invalid_report", "schema_errors": [
                {"location": list(error["loc"]), "type": error["type"],
                 **{k: v for k, v in error.get("ctx", {}).items() if k in ("max_length", "min_length")}}
                for error in exc.errors(include_url=False, include_input=False)]})
        except (ValueError, TypeError):
            gaps.append({"code": "unstructured_or_invalid_report"})
        # Inspect all observed evidence for known explicit conflicts, even if omitted from the draft.
        for ref_id in self.offered:
            try:
                self._resolve(ref_id)
            except (UnsupportedClaim, KeyError, TypeError) as exc:
                gaps.append({"code": str(exc) if isinstance(exc, UnsupportedClaim) else "invalid_source_reference", "evidence_id": ref_id})
        conflicts = self._conflicts()
        gaps.extend(conflicts)
        if conflicts:
            disputed = {conflict["subject_id"] for conflict in conflicts}
            kept = []
            for item in claims:
                if item["claim"]["kind"] == "eta" and item["claim"]["subject_id"] in disputed:
                    rejected.append({"index": item["index"], "code": "conflicting_eta_sources"})
                else:
                    kept.append(item)
            claims = kept
        if rejected:
            gaps.append({"code": "unsupported_claims", "claims": rejected})
        # A table is the canonical presentation of its collection. Prefer its independently
        # ordered proof, and suppress semantically identical copied facts without hiding errors.
        original_count, unique, seen = len(claims), [], set()
        for item in [c for c in claims if c.get("from_table")] + [c for c in claims if not c.get("from_table")]:
            identity = claim_identity(item["claim"])
            if identity not in seen:
                unique.append(item)
                seen.add(identity)
        claims = unique
        if not claims:
            gaps.append({"code": "no_verified_claims"})
        gaps.extend(self._requirements(scope, claims))
        if self.contract["requires_open_questions"] and not open_questions:
            gaps.append({"code": "required_open_questions_missing"})
        # Incomplete or ambiguous runs never publish a formal report.
        accepted = bool(claims) and not gaps
        status = "completed" if accepted else "partial" if claims or self.offered else "failed"
        return {"status": status, "report_validation": "passed" if accepted else "blocked",
                "report": {"scope": scope, "verified_claims": claims, "rejected_claims": rejected,
                           "task_requirements": self.contract, "tables": tables,
                           "duplicate_claims_removed": original_count-len(claims),
                           "gaps": gaps, "conflicts": conflicts, "open_questions_unverified": open_questions,
                           "formal_report": "\n".join(item["statement"] for item in claims if not item.get("from_table")) if accepted else None}}


def render_table(table):
    """Render only independently checked rows; names and counts cannot be deleted by the model."""
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    if table["view"] == "arr_exposure":
        lines = ["| PART-ID | 组件名称 | ARR | 开放工单数量 | 客户 ID | 币种 |", "|---|---|---:|---:|---|---|"]
        for row in table["rows"]:
            lines.append("| " + " | ".join(cell(v) for v in (row["part_id"], row["part_name"], row["arr"], row["ticket_count"], ", ".join(row["account_ids"]), row["currency"])) + " |")
        if "global" in table:
            lines.append("全局客户去重 ARR = " + table["global"]["arr"] + "；不可累加各组件金额。")
    else:
        lines = ["| Ticket ID | Subject | 产品组件 | 未完成工程问题（候选） | 客户 ID / 名称 | ARR |", "|---|---|---|---|---|---:|"]
        for row in table["rows"]:
            components = "; ".join(c["part_id"]+" / "+c["part_name"] for c in row["components"])
            issues = "; ".join((c["representative_issue_id"] or "none") + f"（匹配 {c['open_issue_count']}；代表记录，非全量枚举）" for c in row["components"])
            lines.append("| " + " | ".join(cell(v) for v in (row["ticket_id"], row["subject"], components, issues, row["account_id"]+" / "+row["account_name"], row["arr"])) + " |")
        lines.append("账户 ARR 币种未指定。共享组件仅为候选关联，不证明根因；未完成 Issue 的存在性与数量取全，列表展示代表记录。")
    lines.append("来源工具调用：" + ", ".join(table["source_call_ids"]) + "；逐项原始引用见 verified_claims / evidence_ids。")
    return "\n".join(lines)
