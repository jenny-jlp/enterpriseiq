"""Case history is independent from graph checkpoints. Appends are transactional."""

import hashlib
from importlib.resources import files
import json
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from enterpriseiq.reliability import ReportVerifier


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Cases:
    def __init__(self, conn):
        self.conn = conn

    def initialize(self):
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(297581325618)")
            self.conn.execute(files("enterpriseiq").joinpath("cases.sql").read_text(encoding="utf-8"))

    def _lock(self, case_id):
        if not self.conn.execute("SELECT case_id FROM investigation_cases.cases WHERE case_id=%s FOR UPDATE", (UUID(str(case_id)),)).fetchone():
            raise ValueError("Case not found")

    def register(self, state, case_id=None):
        provided = case_id is not None
        case_id = str(UUID(str(case_id))) if case_id else str(uuid4())
        with self.conn.transaction():
            if not self.conn.execute("SELECT 1 FROM investigation_cases.cases WHERE case_id=%s", (case_id,)).fetchone():
                if provided:
                    raise ValueError("Case not found")
                # An explicitly provided case must exist; new cases use a generated ID.
                self.conn.execute("INSERT INTO investigation_cases.cases(case_id) VALUES (%s)", (case_id,))
            self._lock(case_id)
            self.conn.execute("INSERT INTO investigation_cases.runs(run_id,case_id,snapshot_id,question) VALUES (%s,%s,%s,%s)",
                              (state["run_id"], case_id, state["snapshot_id"], state["question"]))
        return case_id

    def _latest(self, case_id):
        row = self.conn.execute("SELECT report_id,version,validation,payload,r.run_id FROM investigation_cases.reports r JOIN investigation_cases.runs u ON u.run_id=r.run_id WHERE r.case_id=%s ORDER BY u.run_order DESC,r.version DESC LIMIT 1", (case_id,)).fetchone()
        return None if not row else {"report_id": str(row[0]), "version": row[1], "validation": row[2], "payload": row[3], "run_id": str(row[4])}

    def _append_report(self, case_id, run_id, payload, key):
        found = self.conn.execute("SELECT report_id,version FROM investigation_cases.reports WHERE run_id=%s AND content_key=%s", (run_id, key)).fetchone()
        if found:
            return {"report_id": str(found[0]), "version": found[1]}
        maximum = self.conn.execute("SELECT coalesce(max(version),0) FROM investigation_cases.reports WHERE case_id=%s", (case_id,)).fetchone()[0]
        report_id, version = str(uuid4()), maximum+1
        self.conn.execute("INSERT INTO investigation_cases.reports(report_id,case_id,run_id,version,content_key,validation,payload) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                          (report_id, case_id, run_id, version, key, payload["report_validation"], Jsonb(payload)))
        return {"report_id": report_id, "version": version}

    def reconcile(self, state):
        """Called under the execution lock; repeat after a crash before projection succeeds."""
        with self.conn.transaction():
            row = self.conn.execute("SELECT case_id FROM investigation_cases.runs WHERE run_id=%s", (state["run_id"],)).fetchone()
            if not row:
                raise ValueError("Run is not registered in a Case")
            case_id = str(row[0])
            self._lock(case_id)
            self.conn.execute("UPDATE investigation_cases.runs SET state=%s WHERE run_id=%s", (Jsonb(state), state["run_id"]))
            if state["status"] not in ("paused", "awaiting_input", "running"):
                payload = {"report_validation": state["report_validation"], "report": state["report"],
                           "draft": state.get("draft_answer", ""), "task_contract": state.get("task_contract")}
                self._append_report(case_id, state["run_id"], payload, "run:"+digest(payload))
        return case_id

    def review(self, case_id, report_id, action, actor, reason, request_id, *, opinion="", draft=None):
        if action not in ("accept", "reject", "modify") or not actor.strip() or not reason.strip():
            raise ValueError("Review needs action, actor and reason")
        if (action == "modify") != (draft is not None):
            raise ValueError("Only modify requires a structured draft")
        case_id, report_id, request_id = map(lambda v: str(UUID(str(v))), (case_id, report_id, request_id))
        request_hash = digest([case_id, report_id, action, actor, reason, opinion, draft])
        with self.conn.transaction():
            self._lock(case_id)
            existing = self.conn.execute("SELECT request_hash,result FROM investigation_cases.reviews WHERE request_id=%s", (request_id,)).fetchone()
            if existing:
                if existing[0] != request_hash:
                    raise ValueError("Review request ID has different content")
                return existing[1]
            latest = self._latest(case_id)
            if not latest or latest["report_id"] != report_id:
                raise ValueError("Stale report version")
            if self.conn.execute("SELECT 1 FROM investigation_cases.reviews WHERE report_id=%s", (report_id,)).fetchone():
                raise ValueError("Report has already been reviewed")
            if action == "accept" and latest["validation"] != "passed":
                raise ValueError("Blocked report cannot be accepted")
            # Starting another run invalidates decisions on an earlier investigation.
            newest = self.conn.execute("SELECT run_id FROM investigation_cases.runs WHERE case_id=%s ORDER BY run_order DESC LIMIT 1", (case_id,)).fetchone()
            if str(newest[0]) != latest["run_id"]:
                raise ValueError("A newer investigation is pending")
            result = {"case_id": case_id, "report_id": report_id, "action": action}
            if action == "modify":
                row = self.conn.execute("SELECT state FROM investigation_cases.runs WHERE run_id=%s", (latest["run_id"],)).fetchone()
                state = {**row[0], "answer": json.dumps(draft, ensure_ascii=False)}
                checked = ReportVerifier(self.conn).verify(state)
                payload = {"report_validation": checked["report_validation"], "report": checked["report"],
                           "draft": state["answer"], "task_contract": state.get("task_contract"), "parent_report_id": report_id}
                result["new_report"] = self._append_report(case_id, latest["run_id"], payload, "review:"+request_id)
                result["report_validation"] = checked["report_validation"]
            self.conn.execute("INSERT INTO investigation_cases.reviews(request_id,case_id,report_id,action,actor,reason,opinion,request_hash,result) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                              (request_id, case_id, report_id, action, actor, reason, opinion, request_hash, Jsonb(result)))
            return result

    def show(self, case_id):
        case_id = str(UUID(str(case_id)))
        if not self.conn.execute("SELECT 1 FROM investigation_cases.cases WHERE case_id=%s", (case_id,)).fetchone():
            raise ValueError("Case not found")
        runs = [{"run_id": str(r[0]), "snapshot_id": r[1], "question": r[2],
                 "execution_status": r[3].get("status") if r[3] else "registered",
                 "gaps": r[3].get("investigation_gaps", []) if r[3] else [],
                 "task_contract": r[3].get("task_contract") if r[3] else None,
                 "evidence": r[3].get("evidence", {}) if r[3] else {},
                 "trace": r[3].get("trace", {}) if r[3] else {}}
                for r in self.conn.execute("SELECT run_id,snapshot_id,question,state FROM investigation_cases.runs WHERE case_id=%s ORDER BY run_order", (case_id,))]
        reports = [{"report_id": str(r[0]), "run_id": str(r[1]), "version": r[2], "payload": r[3]}
                   for r in self.conn.execute("SELECT report_id,run_id,version,payload FROM investigation_cases.reports WHERE case_id=%s ORDER BY version", (case_id,))]
        reviews = [{"request_id": str(r[0]), "report_id": str(r[1]), "action": r[2], "actor": r[3], "reason": r[4], "opinion_unverified": r[5], "at": r[6].isoformat(), "result": r[7]}
                   for r in self.conn.execute("SELECT request_id,report_id,action,actor,reason,opinion,created_at,result FROM investigation_cases.reviews WHERE case_id=%s ORDER BY created_at,request_id", (case_id,))]
        current = [r for r in reports if r["run_id"] == runs[-1]["run_id"]] if runs else []
        latest = current[-1] if current else None
        decision = next((r["action"] for r in reviews if latest and r["report_id"] == latest["report_id"]), None)
        status = "investigating" if not latest or runs[-1]["run_id"] != latest["run_id"] else decision or ("awaiting_review" if latest["payload"]["report_validation"] == "passed" else "blocked")
        return {"case_id": case_id, "business_status": status, "runs": runs, "reports": reports, "reviews": reviews}

    def list_cases(self):
        rows = self.conn.execute("""SELECT c.case_id,c.created_at,u.run_id,u.question,u.state
            FROM investigation_cases.cases c LEFT JOIN LATERAL
            (SELECT run_id,question,state FROM investigation_cases.runs WHERE case_id=c.case_id ORDER BY run_order DESC LIMIT 1) u ON true
            ORDER BY c.created_at,c.case_id""").fetchall()
        return [{"case_id": str(r[0]), "created_at": r[1].isoformat(), "run_id": str(r[2]) if r[2] else None,
                 "question": r[3], "execution_status": r[4].get("status") if r[4] else "registered"} for r in rows]
