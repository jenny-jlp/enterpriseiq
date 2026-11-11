import json
from uuid import uuid4

import psycopg

from enterpriseiq.budget import BudgetLedger
from enterpriseiq.cli import main
from enterpriseiq.jobs import Jobs
from enterpriseiq.performance import execution_history
from enterpriseiq.web_auth import Identity
from enterpriseiq.worker import run_worker
from test_workbench_worker import database, SourceModel


def test_cli_checkpoint_resume_and_web_worker_share_the_same_authority(database,monkeypatch,capsys):
    class Provider(SourceModel):
        name='deepseek-flash'
    monkeypatch.setattr('enterpriseiq.budget.DeepSeekModel',Provider)
    monkeypatch.setenv('DATABASE_URL',database)
    monkeypatch.setenv('BUDGET_DATABASE_URL',database)
    monkeypatch.setattr('sys.argv',['enterpriseiq','investigate','查看 ACC-013 客户名称','--pause-after-tool'])
    main()
    first=json.loads(capsys.readouterr().out)
    assert first['status']=='paused'
    monkeypatch.setattr('sys.argv',['enterpriseiq','resume',first['run_id']])
    main()
    resumed=json.loads(capsys.readouterr().out)
    assert resumed['status']=='completed'
    # Reading a completed checkpoint makes no additional provider request.
    main()
    assert json.loads(capsys.readouterr().out)['status']=='completed'
    with psycopg.connect(database,autocommit=True) as conn:
        job=Jobs(conn).enqueue(Identity('alice','demo',('investigator',)),uuid4(),'查看 ACC-013 客户名称')
    run_worker(database,once=True,live=True,budget_database=database)
    with psycopg.connect(database) as conn:
        summary=BudgetLedger(conn).summary()
        assert summary['runs']==2 and summary['attempts']==4 and summary['known_tokens']==28
        assert Jobs(conn).get(Identity('alice','demo',('investigator',)),job['job_id'])['status']=='completed'
        cli_rows=execution_history(conn,first['run_id'])
        worker_rows=execution_history(conn,job['run_id'])
    assert len(cli_rows)==3 and cli_rows[2]['timings']==[]
    assert worker_rows[0]['entrypoint']=='web_worker'
    monkeypatch.setattr('sys.argv',['enterpriseiq','run-metrics',first['run_id']])
    main()
    report=json.loads(capsys.readouterr().out)
    assert len(report['model_attempts'])==2
    assert all(row['cost_kind']=='conservative_rate_estimate' for row in report['model_attempts'])
    assert all(row['provider_seconds']>0 for row in report['model_attempts'])


def test_cli_missing_authority_cannot_fall_back_to_an_empty_budget(database,monkeypatch,capsys):
    import pytest
    monkeypatch.setenv('DATABASE_URL',database)
    monkeypatch.delenv('BUDGET_DATABASE_URL',raising=False)
    monkeypatch.setattr('sys.argv',['enterpriseiq','investigate','查看 ACC-013 客户名称'])
    with pytest.raises(SystemExit):
        main()
    assert 'model_budget_database_required' in capsys.readouterr().err
