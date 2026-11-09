from uuid import uuid4

import psycopg
import pytest

from enterpriseiq.performance import ExecutionMetrics, Timed, execution_history
from test_workbench_worker import database


def test_timing_records_errors_and_distinguishes_observation_from_estimate(database):
    run=str(uuid4())
    metrics=ExecutionMetrics(database,run,'cli','simulated')
    class Target:
        timeout_ms=10000
        def execute(self):
            raise ValueError('fixture')
    timed=Timed(Target(),metrics,'tool')
    timed.timeout_ms=20
    assert timed.target.timeout_ms==20
    with pytest.raises(ValueError):
        timed.execute()
    metrics.finish('interrupted')
    with psycopg.connect(database) as conn:
        row=execution_history(conn,run)[0]
    assert row['observation']=='simulated' and row['status']=='interrupted'
    assert row['queue_seconds']==0
    assert row['execution_seconds']>0 and row['end_to_end_seconds']>0
    assert row['timings'][0]['outcome']=='error' and row['timings'][0]['seconds']>0


def test_unfinished_process_is_not_fabricated_as_zero_latency(database):
    run=str(uuid4())
    ExecutionMetrics(database,run,'web_worker','simulated')
    with psycopg.connect(database) as conn:
        row=execution_history(conn,run)[0]
    assert row['status']=='incomplete'
    assert row['execution_seconds'] is None and row['end_to_end_seconds'] is None


def test_worker_records_model_tool_validation_and_queue(database):
    from enterpriseiq.jobs import Jobs
    from enterpriseiq.web_auth import Identity
    from enterpriseiq.agent import postgres_checkpointer
    from enterpriseiq.worker import process_job
    from test_workbench_worker import SourceModel
    with psycopg.connect(database,autocommit=True) as conn:
        job=Jobs(conn).enqueue(Identity('alice','demo',('investigator',)),uuid4(),'查看 ACC-013 客户名称')
        row=conn.execute('SELECT job_id,run_id,command,input FROM investigation_web.jobs WHERE job_id=%s',(job['job_id'],)).fetchone()
    with psycopg.connect(database) as conn,postgres_checkpointer(database) as saver:
        result=process_job(conn,saver,row,model=SourceModel(),metrics_database=database)
    assert result['status']=='completed'
    with psycopg.connect(database) as conn:
        row=execution_history(conn,job['run_id'])[0]
    assert row['queue_seconds']>0
    assert {t['component'] for t in row['timings']}=={'model_adapter','tool','validation'}
