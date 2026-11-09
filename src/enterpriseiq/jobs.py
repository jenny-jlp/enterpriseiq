"""PostgreSQL jobs for a single local worker. The existing graph remains unchanged."""

from importlib.resources import files
import os

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from enterpriseiq.agent import initial_state
from enterpriseiq.cases import Cases, digest


# A separate namespace from M1 checkpoint setup and M2 Case schema setup locks.
WORKER_LOCK = int(digest("enterpriseiq-web-worker-v1")[:15], 16)


class Jobs:
    def __init__(self, conn):
        self.conn = conn

    def initialize(self):
        Cases(self.conn).initialize()
        self.conn.execute(files("enterpriseiq").joinpath("web.sql").read_text(encoding="utf-8"))

    def require_case(self, identity, case_id):
        if not self.conn.execute("SELECT 1 FROM investigation_web.cases WHERE case_id=%s AND workspace=%s", (case_id, identity.workspace)).fetchone():
            raise HTTPException(404, "Case not found")

    def require_run(self, identity, run_id):
        row = self.conn.execute("""SELECT r.case_id, w.initiator FROM investigation_cases.runs r
            JOIN investigation_web.runs w USING(run_id) JOIN investigation_web.cases c USING(case_id)
            WHERE r.run_id=%s AND c.workspace=%s""", (run_id, identity.workspace)).fetchone()
        if not row:
            raise HTTPException(404, "Run not found")
        return str(row[0]), row[1]

    def _existing(self, identity, request_id, fingerprint):
        row = self.conn.execute("SELECT workspace,actor,request_hash FROM investigation_web.jobs WHERE job_id=%s", (request_id,)).fetchone()
        if row:
            if row != (identity.workspace, identity.user_id, fingerprint):
                raise HTTPException(409, "Request ID already used with different content or identity")
            return self.get(identity, request_id)

    def enqueue(self, identity, request_id, question, case_id=None):
        fingerprint = digest(["start", question, str(case_id) if case_id else None])
        with self.conn.transaction():
            # Serialize the same request before registering its Case/run.
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(request_id),))
            existing = self._existing(identity, request_id, fingerprint)
            if existing:
                return existing
            if case_id:
                self.require_case(identity, case_id)
                Cases(self.conn)._lock(case_id)
                if self.pending_case(case_id):
                    raise HTTPException(409, "Finish or resolve the existing investigation first")
            state = initial_state(question, os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
            case_id = Cases(self.conn).register(state, case_id)
            self.conn.execute("INSERT INTO investigation_web.cases VALUES (%s,%s) ON CONFLICT DO NOTHING", (case_id, identity.workspace))
            self.conn.execute("INSERT INTO investigation_web.runs VALUES (%s,%s)", (state["run_id"], identity.user_id))
            self.conn.execute("""INSERT INTO investigation_web.jobs(job_id,workspace,actor,request_hash,case_id,run_id,command,input)
                VALUES (%s,%s,%s,%s,%s,%s,'start',%s)""", (request_id, identity.workspace, identity.user_id, fingerprint, case_id, state["run_id"], Jsonb(state)))
            return self.get(identity, request_id)

    def resume(self, identity, request_id, run_id, clarification=None):
        fingerprint = digest(["resume", str(run_id), clarification])
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(request_id),))
            existing = self._existing(identity, request_id, fingerprint)
            if existing:
                return existing
            case_id, owner = self.require_run(identity, run_id)
            if owner != identity.user_id:
                raise HTTPException(403, "Only the investigator who started this run may resume it")
            Cases(self.conn)._lock(case_id)
            self.recover_interrupted()
            newest_run = self.conn.execute("SELECT run_id FROM investigation_cases.runs WHERE case_id=%s ORDER BY run_order DESC LIMIT 1", (case_id,)).fetchone()
            if str(newest_run[0]) != str(run_id):
                raise HTTPException(409, "A newer investigation exists in this Case")
            latest = self.conn.execute("SELECT status FROM investigation_web.jobs WHERE run_id=%s ORDER BY job_order DESC LIMIT 1", (run_id,)).fetchone()
            if not latest or latest[0] not in ("awaiting_input", "paused", "interrupted"):
                raise HTTPException(409, "Run is not waiting for clarification or recovery")
            if (latest[0] == "awaiting_input") != bool(clarification and clarification.strip()):
                raise HTTPException(409, "Supply clarification only when the run is awaiting input")
            self.conn.execute("""INSERT INTO investigation_web.jobs(job_id,workspace,actor,request_hash,case_id,run_id,command,input)
                VALUES (%s,%s,%s,%s,%s,%s,'resume',%s)""", (request_id, identity.workspace, identity.user_id, fingerprint, case_id, run_id, Jsonb({"clarification": clarification})))
            return self.get(identity, request_id)

    def recover_interrupted(self):
        # A session lock is held for the worker's entire lifetime. No timer guesses.
        acquired = self.conn.execute("SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK,)).fetchone()[0]
        if acquired:
            try:
                self.conn.execute("UPDATE investigation_web.jobs SET status='interrupted',error_code='worker_interrupted',updated_at=now() WHERE status='running'")
            finally:
                self.conn.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK,))

    def get(self, identity, job_id):
        row = self.conn.execute("""SELECT job_id,case_id,run_id,status,error_code,created_at,updated_at
            FROM investigation_web.jobs WHERE job_id=%s AND workspace=%s""", (job_id, identity.workspace)).fetchone()
        if not row:
            raise HTTPException(404, "Job not found")
        return dict(zip(("job_id", "case_id", "run_id", "status", "error_code", "created_at", "updated_at"),
                        [str(v) if v is not None else None for v in row]))

    def list_cases(self, identity):
        self.recover_interrupted()
        ids = {str(r[0]) for r in self.conn.execute("SELECT case_id FROM investigation_web.cases WHERE workspace=%s", (identity.workspace,))}
        cases = [c for c in Cases(self.conn).list_cases() if c["case_id"] in ids]
        for case in cases:
            case["jobs"] = self.case_jobs(identity, case["case_id"])
        return cases

    def case_jobs(self, identity, case_id):
        return [self.get(identity, r[0]) for r in self.conn.execute("SELECT job_id FROM investigation_web.jobs WHERE workspace=%s AND case_id=%s ORDER BY job_order", (identity.workspace, case_id)).fetchall()]

    def pending_case(self, case_id):
        latest = self.conn.execute("SELECT status FROM investigation_web.jobs WHERE case_id=%s ORDER BY job_order DESC LIMIT 1", (case_id,)).fetchone()
        return bool(latest and latest[0] in ("queued", "running", "awaiting_input", "paused", "interrupted"))

    def show_case(self, identity, case_id):
        self.require_case(identity, case_id)
        self.recover_interrupted()
        result = Cases(self.conn).show(case_id)
        result["jobs"] = self.case_jobs(identity, case_id)
        owners = dict((str(r[0]), r[1]) for r in self.conn.execute("SELECT r.run_id,w.initiator FROM investigation_cases.runs r JOIN investigation_web.runs w USING(run_id) WHERE case_id=%s", (case_id,)))
        for run in result["runs"]:
            run["initiator"] = owners.get(run["run_id"])
        if result["runs"]:
            run_id = result["runs"][-1]["run_id"]
            saved = self.conn.execute("SELECT state FROM investigation_cases.runs WHERE run_id=%s", (run_id,)).fetchone()[0]
            state = self.checkpoint_state(run_id) or saved or {}
            result["progress"] = {key: state.get(key, 0) for key in ("model_calls", "tool_calls", "prompt_tokens", "completion_tokens")}
            result["progress"]["usage_incomplete"] = state.get("usage_incomplete", False)
            if state.get("trace"):
                result["runs"][-1]["trace"] = state["trace"]
        return result

    def checkpoint_state(self, run_id):
        if not self.conn.execute("SELECT to_regclass('investigation_checkpoint.checkpoints')").fetchone()[0]:
            return None
        from langgraph.checkpoint.postgres import PostgresSaver
        with self.conn.transaction():
            previous = self.conn.execute("SHOW search_path").fetchone()[0]
            try:
                self.conn.execute("SELECT set_config('search_path', 'investigation_checkpoint', true)")
                checkpoint = PostgresSaver(self.conn).get_tuple({"configurable": {"thread_id": str(run_id)}})
                return checkpoint.checkpoint["channel_values"] if checkpoint else None
            finally:
                self.conn.execute("SELECT set_config('search_path', %s, true)", (previous,))

    def evidence(self, identity, run_id, evidence_id):
        self.require_run(identity, run_id)
        row = self.conn.execute("SELECT state FROM investigation_cases.runs WHERE run_id=%s", (run_id,)).fetchone()
        state = row[0] if row else None
        if not state or evidence_id not in state.get("evidence", {}):
            raise HTTPException(404, "Evidence not found in this investigation")
        # Reuse the original-source resolver. No arbitrary path, entity ID or file reads.
        from enterpriseiq.reliability import ReportVerifier, UnsupportedClaim
        verifier = ReportVerifier(self.conn)
        verifier.verify(state)
        try:
            reference, content = verifier._resolve(evidence_id)
        except UnsupportedClaim:
            raise HTTPException(409, "Evidence source is unavailable or no longer matches its saved reference") from None
        return {"reference": reference, "content": content}

    def review(self, identity, case_id, body):
        self.require_case(identity, case_id)
        if self.conn.execute("SELECT 1 FROM investigation_cases.reviews WHERE request_id=%s AND case_id=%s", (body.request_id, case_id)).fetchone():
            # Acknowledge an already committed request even if a follow-up has since begun.
            # Cases.review checks the exact content hash, including the trusted actor.
            return Cases(self.conn).review(case_id, body.report_id, body.action, identity.user_id,
                                          body.reason, body.request_id, opinion=body.opinion, draft=body.draft)
        # Check the initiator independently of the client-supplied report ID.
        row = self.conn.execute("SELECT w.initiator FROM investigation_cases.runs r JOIN investigation_web.runs w USING(run_id) WHERE case_id=%s ORDER BY run_order DESC LIMIT 1", (case_id,)).fetchone()
        if not row or row[0] == identity.user_id:
            raise HTTPException(403, "Investigators cannot review their own investigation reports")
        if self.pending_case(case_id):
            raise HTTPException(409, "Investigation is still pending")
        return Cases(self.conn).review(case_id, body.report_id, body.action, identity.user_id,
                                      body.reason, body.request_id, opinion=body.opinion, draft=body.draft)
