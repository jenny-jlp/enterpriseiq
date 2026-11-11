"""One PostgreSQL-backed worker; no Redis, broker, browser connection or HTTP task."""

import argparse
import os
import time

import psycopg

from enterpriseiq.agent import CheckpointCompatibilityError, build_agent, execute_run, postgres_checkpointer, save_result
from enterpriseiq.cases import Cases
from enterpriseiq.jobs import Jobs, WORKER_LOCK
from enterpriseiq.model import ModelError
from enterpriseiq.tools import BusinessTools


class DisabledModel:
    def complete(self, *args, **kwargs):
        raise ModelError("model_calls_disabled")


def process_job(conn, saver, job, *, model=None, metrics_database=None):
    """Run a committed job. Tests inject only the external model, using real tools/verifier."""
    job_id, run_id, command, data = job
    if model is None:
        # Safe local default: only explicit --live selects the durable budget wrapper.
        model = DisabledModel()
    from enterpriseiq.performance import ExecutionMetrics, Timed
    from enterpriseiq.reliability import ReportVerifier
    from enterpriseiq.budget import BudgetedModel

    live = isinstance(model, BudgetedModel)
    metrics_dsn = model.dsn if live else metrics_database
    if not metrics_dsn:
        raise ValueError('Explicit metrics database required')
    submitted = conn.execute('SELECT created_at FROM investigation_web.jobs WHERE job_id=%s', (job_id,)).fetchone()[0]
    # Do not retain a web-table lock while waiting for an external model. API
    # schema checks must remain responsive during browser refresh.
    conn.commit()
    metrics = ExecutionMetrics(metrics_dsn,run_id,'web_worker','live' if live else 'disabled' if isinstance(model,DisabledModel) else 'simulated',submitted)
    graph = build_agent(Timed(model,metrics,'model_adapter'), Timed(BusinessTools(conn),metrics,'tool'), saver,
                        verifier=Timed(ReportVerifier(conn),metrics,'validation'))
    config = {"configurable": {"thread_id": str(run_id)}}
    try:
        if live:
            model.prepare(entrypoint='web_worker')
        existing = graph.get_state(config)
        if not existing.values:
            if command != "start":
                # Registration committed but graph never started. Its initial checkpoint
                # precedes every model/tool request, so restarting here repeats no call.
                data = conn.execute("SELECT input FROM investigation_web.jobs WHERE run_id=%s AND command='start' ORDER BY job_order LIMIT 1", (run_id,)).fetchone()[0]
            # Queue residence is not investigation execution time.
            data["deadline"] = time.time() + data["remaining_seconds"]
            result = execute_run(graph, state=data, on_result=Cases(conn).reconcile)
        else:
            result = execute_run(graph, run_id=str(run_id), clarification=data.get("clarification"), on_result=Cases(conn).reconcile)
        save_result(result)
        status = result["status"]
        error = result.get("stop_reason") if status in ("failed", "partial") else None
        conn.execute("UPDATE investigation_web.jobs SET status=%s,error_code=%s,updated_at=now() WHERE job_id=%s", (status, error, job_id))
        conn.commit()
        metrics.finish(status)
        return result
    except ModelError as exc:
        conn.rollback()
        conn.execute("UPDATE investigation_web.jobs SET status='failed',error_code=%s,updated_at=now() WHERE job_id=%s", (exc.code,job_id))
        conn.commit()
        metrics.finish('failed')
        return None
    except CheckpointCompatibilityError as exc:
        conn.rollback()
        conn.execute("UPDATE investigation_web.jobs SET status='failed',error_code=%s,updated_at=now() WHERE job_id=%s", (exc.code, job_id))
        conn.commit()
        metrics.finish('failed')
        # Existing checkpoint and Case history stay untouched. Start a new run explicitly.
        return None
    except Exception:
        # Do not export raw exception text, credentials, HTTP bodies or source contents.
        conn.rollback()
        conn.execute("UPDATE investigation_web.jobs SET status='interrupted',error_code='execution_interrupted',updated_at=now() WHERE job_id=%s", (job_id,))
        conn.commit()
        metrics.finish('interrupted')
        raise


def run_worker(dsn, *, once=False, model=None, live=False, phase="smoke", budget_database=None):
    with psycopg.connect(dsn, autocommit=True) as control:
        if not control.execute("SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK,)).fetchone()[0]:
            raise ValueError("Another workbench worker is already running")
        try:
            Jobs(control).initialize()
            if live:
                from enterpriseiq.budget import BudgetLedger, budget_dsn
                budget_database = budget_database or budget_dsn()
                with psycopg.connect(budget_database) as budget_conn:
                    BudgetLedger(budget_conn).initialize()
            Jobs(control).recover_interrupted()
            with postgres_checkpointer(dsn) as saver:
                while True:
                    with psycopg.connect(dsn) as conn:
                        with conn.transaction():
                            row = conn.execute("SELECT job_id,run_id,command,input FROM investigation_web.jobs WHERE status='queued' ORDER BY job_order FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
                            if row:
                                conn.execute("UPDATE investigation_web.jobs SET status='running',error_code=NULL,updated_at=now() WHERE job_id=%s", (row[0],))
                        if row:
                            try:
                                selected_model = model
                                if live:
                                    from enterpriseiq.budget import BudgetedModel
                                    selected_model = BudgetedModel(budget_database, str(row[1]), phase=phase)
                                process_job(conn, saver, row, model=selected_model,metrics_database=dsn)
                            except Exception:
                                print("Investigation interrupted; inspect its job and explicitly resume.", flush=True)
                    if once:
                        return bool(row)
                    if not row:
                        time.sleep(1)
        finally:
            control.execute("UPDATE investigation_web.jobs SET status='interrupted',error_code='worker_interrupted',updated_at=now() WHERE status='running'")
            control.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK,))


def main():
    parser = argparse.ArgumentParser(description="Local single worker; --live enables budgeted DeepSeek calls")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job")
    parser.add_argument("--live", action="store_true", help="Enable paid requests under the cumulative M3 budget")
    parser.add_argument("--phase", choices=["smoke", "freeze"], default="smoke")
    args = parser.parse_args()
    run_worker(os.environ["DATABASE_URL"], once=args.once, live=args.live, phase=args.phase)
