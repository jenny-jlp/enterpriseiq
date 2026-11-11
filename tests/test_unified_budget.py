"""Durable budget behavior; simulated provider, real PostgreSQL, no paid requests."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest

from enterpriseiq.budget import BudgetLedger, BudgetedModel
from enterpriseiq.model import Completion, DeepSeekModel, ModelError
from test_workbench_worker import database


class Provider:
    name='deepseek-flash'
    calls=0
    def complete(self,*args,**kwargs):
        type(self).calls+=1
        return Completion({'role':'assistant','content':'fixture'}, {'prompt_tokens':5,'completion_tokens':2},self.name,'fake-id','stop')


@pytest.fixture
def provider(monkeypatch):
    Provider.calls=0
    monkeypatch.setattr('enterpriseiq.budget.DeepSeekModel',Provider)
    return Provider


def test_raw_provider_call_is_denied_without_a_budget_grant(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','local-fixture-not-a-real-key')
    monkeypatch.setattr('enterpriseiq.model.httpx.Client',lambda *a,**k:pytest.fail('No raw HTTP before budget grant'))
    with pytest.raises(ModelError,match='budget_context_required'):
        DeepSeekModel().complete([],[],1)


def test_same_logical_request_reuses_durable_response_across_new_model_objects(database,provider):
    run=str(uuid4())
    model=BudgetedModel(database,run)
    model.prepare(entrypoint='cli')
    first=model.for_attempt(1).complete([{'role':'user','content':'test'}],[],1)
    restored=BudgetedModel(database,run)
    restored.prepare(entrypoint='web_worker')
    second=restored.for_attempt(1).complete([{'role':'user','content':'test'}],[],1)
    assert second==first and provider.calls==1
    with psycopg.connect(database) as conn:
        summary=BudgetLedger(conn).summary()
        assert summary['runs']==summary['attempts']==1 and summary['known_tokens']==7
    with pytest.raises(ModelError,match='content_conflict'):
        restored.for_attempt(1).complete([{'role':'user','content':'changed'}],[],1)


def test_failed_attempt_and_retry_are_both_counted_and_unknown_reservation_survives(database,monkeypatch):
    class Failing(Provider):
        def complete(self,*args,**kwargs):
            raise ModelError('model_timeout')
    monkeypatch.setattr('enterpriseiq.budget.DeepSeekModel',Failing)
    run=str(uuid4())
    model=BudgetedModel(database,run)
    model.prepare(entrypoint='cli')
    for number in (1,2):
        with pytest.raises(ModelError,match='model_timeout'):
            model.for_attempt(number).complete([],[],1)
    with psycopg.connect(database) as conn:
        summary=BudgetLedger(conn).summary()
        assert summary['runs']==1 and summary['attempts']==summary['unknown_attempts']==2
        assert summary['charged_tokens']>0
    with pytest.raises(ModelError,match='outcome_unknown'):
        BudgetedModel(database,run).for_attempt(1).complete([],[],1)


def test_admission_counts_a_failed_start_with_no_provider_request(conn):
    budget=BudgetLedger(conn,name='test-'+uuid4().hex,max_runs=1)
    budget.initialize()
    run=str(uuid4())
    budget.register_run(run,entrypoint='cli')
    budget.register_run(run,entrypoint='web_worker')
    assert budget.summary()['runs']==1 and budget.summary()['attempts']==0
    with pytest.raises(ModelError,match='budget_exhausted'):
        budget.register_run(str(uuid4()),entrypoint='cli')


def test_missing_credentials_are_known_not_sent_but_do_not_refund_run_or_attempt(database,monkeypatch):
    def missing():
        raise ModelError('missing_deepseek_key')
    monkeypatch.setattr('enterpriseiq.budget.DeepSeekModel',missing)
    model=BudgetedModel(database,str(uuid4()))
    model.prepare(entrypoint='cli')
    with pytest.raises(ModelError,match='missing_deepseek_key'):
        model.for_attempt(1).complete([],[],1)
    with psycopg.connect(database) as conn:
        status=BudgetLedger(conn).summary()
        assert status['runs']==status['attempts']==1
        assert status['known_tokens']==status['charged_tokens']==status['unknown_attempts']==0


def test_concurrent_clients_cannot_both_take_the_last_request_slot(database):
    with psycopg.connect(database) as conn:
        BudgetLedger(conn,max_calls=1).initialize()
    def reserve(_):
        try:
            with psycopg.connect(database) as conn:
                BudgetLedger(conn,max_calls=1).reserve(str(uuid4()),10,1)
            return 'reserved'
        except ModelError:
            return 'blocked'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(reserve,range(2)))
    assert sorted(results)==['blocked','reserved']


def test_phase_switch_and_settlement_conflict_cannot_reset_budget(conn):
    budget=BudgetLedger(conn,name='test-'+uuid4().hex)
    budget.initialize()
    run=str(uuid4())
    budget.register_run(run,entrypoint='cli',phase='smoke')
    with pytest.raises(ModelError,match='phase'):
        budget.register_run(run,entrypoint='evaluation',phase='freeze')
    attempt=budget.reserve(run,10,5)
    budget.settle(attempt,{'prompt_tokens':5,'completion_tokens':2})
    budget.settle(attempt,{'prompt_tokens':50,'completion_tokens':2})
    assert budget.summary()['charged_tokens']>=52
    budget.initialize()  # Even a blocked ledger remains inspectable.
    with pytest.raises(ModelError,match='blocked'):
        budget.reserve(run,1,1)


def test_completed_response_with_missing_usage_is_cached_but_fully_charged(database,monkeypatch):
    class Unknown(Provider):
        def complete(self,*args,**kwargs):
            reply=super().complete(*args,**kwargs)
            reply.usage=None
            return reply
    monkeypatch.setattr('enterpriseiq.budget.DeepSeekModel',Unknown)
    model=BudgetedModel(database,str(uuid4()))
    reply=model.for_attempt(1).complete([],[],1)
    assert model.for_attempt(1).complete([],[],1)==reply
    with psycopg.connect(database) as conn:
        summary=BudgetLedger(conn).summary()
    assert summary['attempts']==summary['unknown_attempts']==1
    assert summary['charged_tokens']>=6144


def test_response_replays_in_another_process_without_provider_construction(database,provider):
    import os
    import subprocess
    import sys
    run=str(uuid4())
    BudgetedModel(database,run).for_attempt(1).complete([],[],1)
    program='''
import os,sys
import enterpriseiq.budget as b
def forbidden():
    raise AssertionError('provider must not be constructed')
b.DeepSeekModel=forbidden
reply=b.BudgetedModel(os.environ['TEST_DATABASE_URL'],sys.argv[1]).for_attempt(1).complete([],[],1)
assert reply.response_id=='fake-id'
print('durable_response_reused')
'''
    result=subprocess.run([sys.executable,'-c',program,run],env={**os.environ,'TEST_DATABASE_URL':database},capture_output=True,text=True,timeout=20)
    assert result.returncode==0, 'cross-process replay failed (stderr suppressed to protect configuration)'
    assert 'durable_response_reused' in result.stdout


def test_same_request_in_flight_cannot_trigger_a_second_provider_call(database,monkeypatch):
    import threading
    entered,release=threading.Event(),threading.Event()
    class Blocking(Provider):
        calls=0
        def complete(self,*args,**kwargs):
            entered.set()
            assert release.wait(10)
            return super().complete(*args,**kwargs)
    monkeypatch.setattr('enterpriseiq.budget.DeepSeekModel',Blocking)
    model=BudgetedModel(database,str(uuid4()))
    model.prepare(entrypoint='cli')
    with ThreadPoolExecutor(max_workers=1) as pool:
        running=pool.submit(model.for_attempt(1).complete,[],[],1)
        try:
            assert entered.wait(5)
            with pytest.raises(ModelError,match='outcome_unknown'):
                model.for_attempt(1).complete([],[],1)
        finally:
            release.set()
        running.result(timeout=10)
    assert Blocking.calls==1


def test_abrupt_process_exit_leaves_committed_reservation_and_refuses_silent_retry(database,monkeypatch):
    import os
    import subprocess
    import sys
    run=str(uuid4())
    program='''
import os,sys,psycopg
from enterpriseiq.budget import BudgetLedger
from enterpriseiq.model import request_digest
with psycopg.connect(os.environ['TEST_DATABASE_URL']) as conn:
    b=BudgetLedger(conn)
    b.initialize()
    b.reserve(sys.argv[1],2052,4096,request_key='model-1',digest=request_digest([],[]))
os._exit(23)
'''
    result=subprocess.run([sys.executable,'-c',program,run],env={**os.environ,'TEST_DATABASE_URL':database},capture_output=True,timeout=20)
    assert result.returncode==23
    monkeypatch.setattr('enterpriseiq.budget.DeepSeekModel',lambda:pytest.fail('No resend after unknown process exit'))
    with pytest.raises(ModelError,match='outcome_unknown'):
        BudgetedModel(database,run).for_attempt(1).complete([],[],1)
    with psycopg.connect(database) as conn:
        summary=BudgetLedger(conn).summary()
    assert summary['attempts']==summary['unknown_attempts']==1 and summary['charged_tokens']==6148
