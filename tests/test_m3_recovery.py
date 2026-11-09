"""Exact-version restore rules; mismatch refusal must precede model/tool/checkpoint writes."""

from copy import deepcopy
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
import psycopg
import pytest

from enterpriseiq.agent import build_agent, execute_run, initial_state, postgres_checkpointer
from enterpriseiq.cases import Cases
from enterpriseiq.jobs import Jobs
from enterpriseiq.web_auth import Identity
from enterpriseiq.worker import run_worker
from test_agent import AdaptiveModel, EvidenceTools, FixtureVerifier
from test_workbench_worker import database, SourceModel


@pytest.mark.parametrize('field,value',[
    ('engine_version','m1-alpha-3'),('snapshot_id','other-snapshot'),
    ('task_contract_version',2),('task_contract_version',True),('task_contract',None),('contract_shape','missing_field'),
])
def test_mismatched_checkpoint_is_rejected_before_any_execution_or_write(field,value):
    tools=EvidenceTools()
    graph=build_agent(AdaptiveModel(),tools,InMemorySaver(),pause_after_tool=True,verifier=FixtureVerifier())
    paused=execute_run(graph,state=initial_state('Find account','offline-fixture'))
    config={'configurable':{'thread_id':paused['run_id']}}
    if field=='contract_shape':
        contract={k:v for k,v in paused['task_contract'].items() if k!='requires_source_actions'}
        graph.update_state(config,{'task_contract':contract})
    elif field=='task_contract_version':
        graph.update_state(config,{'task_contract':{**paused['task_contract'],'version':value}})
    else:
        graph.update_state(config,{field:value})
    before=graph.get_state(config)
    history=len(list(graph.get_state_history(config)))
    with pytest.raises(ValueError,match='incompatible'):
        execute_run(graph,run_id=paused['run_id'])
    after=graph.get_state(config)
    assert after.values==before.values and len(list(graph.get_state_history(config)))==history
    assert tools.calls==['search_accounts']


def test_completed_checkpoint_with_wrong_version_is_not_returned_as_current_success():
    graph=build_agent(AdaptiveModel(),EvidenceTools(),InMemorySaver(),verifier=FixtureVerifier())
    finished=execute_run(graph,state=initial_state('Find account','offline-fixture'))
    config={'configurable':{'thread_id':finished['run_id']}}
    graph.update_state(config,{'engine_version':'future-version'})
    with pytest.raises(ValueError,match='incompatible'):
        execute_run(graph,run_id=finished['run_id'])


def test_worker_marks_incompatible_checkpoint_failed_with_an_explicit_code(database):
    identity=Identity('alice','demo',('investigator',))
    with psycopg.connect(database,autocommit=True) as conn:
        job=Jobs(conn).enqueue(identity,uuid4(),'按区域统计 ARR 前三')
    run_worker(database,once=True)
    with postgres_checkpointer(database) as saver:
        graph=build_agent(SourceModel(),None,saver,verifier=FixtureVerifier())
        config={'configurable':{'thread_id':job['run_id']}}
        graph.update_state(config,{'engine_version':'obsolete-version'})
        before=graph.get_state(config).values
    with psycopg.connect(database,autocommit=True) as conn:
        resume=Jobs(conn).resume(identity,uuid4(),job['run_id'],'产品组件，不是地理区域')
    run_worker(database,once=True,model=SourceModel())
    with psycopg.connect(database,autocommit=True) as conn:
        result=Jobs(conn).get(identity,resume['job_id'])
        assert result['status']=='failed' and result['error_code']=='checkpoint_engine_incompatible'
        assert Cases(conn).show(job['case_id'])['reports']==[]
    with postgres_checkpointer(database) as saver:
        graph=build_agent(SourceModel(),None,saver,verifier=FixtureVerifier())
        assert graph.get_state(config).values==before


def test_queued_old_version_is_refused_before_first_checkpoint(database):
    identity=Identity('alice','demo',('investigator',))
    with psycopg.connect(database,autocommit=True) as conn:
        job=Jobs(conn).enqueue(identity,uuid4(),'查看客户名称')
        conn.execute("UPDATE investigation_web.jobs SET input=jsonb_set(input,'{engine_version}','\"obsolete-version\"') WHERE job_id=%s",(job['job_id'],))
    run_worker(database,once=True,model=SourceModel())
    with psycopg.connect(database,autocommit=True) as conn:
        result=Jobs(conn).get(identity,job['job_id'])
        assert result['status']=='failed' and result['error_code']=='checkpoint_engine_incompatible'
        assert Jobs(conn).checkpoint_state(job['run_id']) is None


def test_same_version_restores_in_a_new_process_without_repeating_the_completed_tool(database):
    from enterpriseiq.tools import BusinessTools
    state=initial_state('查看 ACC-013 客户名称','offline-fixture')
    with postgres_checkpointer(database) as saver,psycopg.connect(database) as conn:
        graph=build_agent(SourceModel(),BusinessTools(conn),saver,pause_after_tool=True)
        paused=execute_run(graph,state=state)
        assert paused['status']=='paused' and paused['tool_calls']==paused['model_calls']==1
    program='''
import json,os,sys
sys.path.insert(0,os.path.join(os.getcwd(),'tests'))
from enterpriseiq.agent import build_agent,execute_run,postgres_checkpointer
from enterpriseiq.tools import BusinessTools
from test_workbench_worker import SourceModel
import psycopg
try:
    with postgres_checkpointer(os.environ['TEST_DATABASE_URL']) as saver,psycopg.connect(os.environ['TEST_DATABASE_URL']) as conn:
        graph=build_agent(SourceModel(),BusinessTools(conn),saver)
        result=execute_run(graph,run_id=sys.argv[1])
        print(json.dumps({k:result[k] for k in ('status','report_validation','model_calls','tool_calls')}))
except Exception as exc:
    print(json.dumps({'error_type':type(exc).__name__}))
    sys.exit(1)
'''
    environment={**os.environ,'TEST_DATABASE_URL':database}
    environment.pop('DEEPSEEK_API_KEY',None)
    for _ in range(2):
        child=subprocess.run([sys.executable,'-c',program,state['run_id']],cwd=Path(__file__).resolve().parents[1],env=environment,capture_output=True,text=True,timeout=30)
        assert child.returncode==0,child.stdout
        result=json.loads(child.stdout)
        assert result=={'status':'completed','report_validation':'passed','model_calls':2,'tool_calls':1}
