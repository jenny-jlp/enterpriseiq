"""Acceptance orchestration tests never call a network model."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from uuid import uuid4

import psycopg
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import accept_m3 as acceptance
from enterpriseiq.model import ModelError
from test_workbench_worker import database,SourceModel
from test_reliability import RecordedRun


def test_plan_is_exactly_21_fresh_trials_and_preserves_questions():
    scenarios,trials=acceptance.plan()
    assert len(trials)==21 and len({r['run_id'] for r in trials})==21
    assert [s['repetitions'] for s in scenarios]==[3,3,3,3,3,2,2,2]
    assert sum(t['checkpoint_test'] for t in trials)==1
    assert all(t['status']=='not_started' for t in trials)
    acceptance.m3.verify_frozen_scoring()


def test_task_failure_and_partial_are_retained_without_replacement():
    _,trials=acceptance.plan()
    calls=[]
    def execute(t,resume):
        calls.append((t['trial_id'],resume))
        return {**t,'status':'failed' if len(calls)==1 else 'partial','automatic_business_pass':False}
    rows=acceptance.drive(trials,execute,lambda _:None,lambda:None)
    assert len(rows)==len(calls)==21 and rows[0]['status']=='failed'
    assert not any(r['automatic_business_pass'] for r in rows)


def test_budget_stop_retains_actual_partial_and_all_unexecuted_slots():
    _,trials=acceptance.plan()
    def execute(t,resume):
        return {**t,'status':'partial','stop_reason':'model_budget_exhausted','automatic_business_pass':False}
    persisted=[]
    rows=acceptance.drive(trials,execute,lambda value:persisted.append(deepcopy(value)),lambda:None)
    assert rows[0]['status']=='partial'
    assert sum(r['status']=='not_started' for r in rows)==20
    assert all(r.get('not_executed_reason')=='model_budget_exhausted' for r in rows[1:])
    assert persisted[-1]==rows


def test_preflight_budget_denial_starts_no_trial():
    _,trials=acceptance.plan()
    def fail():
        raise ModelError('model_budget_exhausted')
    rows=acceptance.drive(trials,lambda *_:pytest.fail('No invocation when preflight denies'),lambda _:None,fail)
    assert all(r['status']=='not_started' for r in rows)


def test_only_planned_pause_resumes_same_run_once():
    _,trials=acceptance.plan()
    target=next(t for t in trials if t['checkpoint_test'])
    seen=[]
    def execute(t,resume):
        seen.append((t['run_id'],resume))
        return {**t,'status':'completed' if resume else 'paused','automatic_business_pass':resume}
    rows=acceptance.drive([target],execute,lambda _:None,lambda:None)
    assert seen==[(target['run_id'],False),(target['run_id'],True)]
    assert rows[0]['checkpoint_restored_same_run']


def test_gap_tools_are_faulted_but_source_records_still_available(database):
    with psycopg.connect(database) as conn:
        tools=acceptance.FixtureTools(conn,'gap')
        assert tools.execute('list_documents',{})['error']['code']=='source_unavailable'
        assert tools.execute('read_document',{'entity_type':'transcript','source_id':'unknown'})['status']=='error'
        assert tools.execute('get_record',{'entity_type':'account','source_id':'ACC-004'})['status']=='ok'


def test_conflict_fixture_is_discoverable_and_original_verifier_keeps_both_sources(database):
    with psycopg.connect(database) as conn:
        acceptance.seed_fixture(conn,'conflict')
        tools=acceptance.FixtureTools(conn,'conflict')
        listed=tools.execute('list_documents',{'query':'ISS-032'})
        assert {'M3-CONFLICT-0','M3-CONFLICT-1'}<={i['data']['document_id'] for i in listed['items'] if 'document_id' in i['data']}
        run=RecordedRun(conn,acceptance.m3.NEW_SCENARIOS[2]['question'])
        refs=[]
        for source in acceptance.FIXTURES['conflict']['documents']:
            refs.append(run.call('read_document',{'entity_type':'document','source_id':source})['evidence']['evidence_id'])
        run.verify([{'kind':'eta','subject_id':'ISS-032','evidence_id':refs[0],'field':None,'quote':'ISS-032 No ETA committed.','value':'uncommitted'}])
        run.state['draft_answer']=run.state['answer']
        checked=acceptance.assess(conn,run.state,'m3-source-conflict')
        assert checked['automatic_business']['business_pass']
        assert checked['source_validation']['report_validation']=='blocked'
        assert not checked['critical_errors']


def test_critical_error_in_repaired_draft_is_not_erased(database):
    with psycopg.connect(database) as conn:
        run=RecordedRun(conn,'查看 ACC-013 客户名称')
        ref=run.record('account','ACC-013')
        bad=run.verify([{'kind':'field','evidence_id':ref,'field':'/arr','value':1}])
        bad_draft=run.state['answer']
        run.verify([{'kind':'field','evidence_id':ref,'field':'/account_name','value':'Vantara'}])
        run.state['draft_answer']=run.state['answer']
        run.state['report_attempts']=[{'draft':bad_draft,'validation':bad}]
        result=acceptance.assess(conn,run.state,'m2-vantara')
        assert result['critical_errors'][0]['code']=='field_value_not_supported'


def test_frozen_trial_executes_real_graph_tools_and_budget_with_simulated_provider(database,tmp_path,monkeypatch):
    class Provider(SourceModel):
        name='deepseek-flash'
    monkeypatch.setattr('enterpriseiq.budget.DeepSeekModel',Provider)
    monkeypatch.setenv('DATABASE_URL',database)
    monkeypatch.setenv('BUDGET_DATABASE_URL',database)
    from enterpriseiq.budget import BudgetLedger
    with psycopg.connect(database) as conn:
        BudgetLedger(conn).initialize()
        sha=acceptance.data_hash(conn)
    scenarios,trials=acceptance.plan()
    trial=trials[0]
    from psycopg.conninfo import conninfo_to_dict
    manifest={'fingerprint':acceptance.fingerprint(),'fixture_definition_sha256':acceptance.hash_value(acceptance.FIXTURES),
        'budget_authority_sha256':acceptance.authority_hash(),'scenarios':scenarios,'trials':trials,
        'databases':{'baseline':{'name':conninfo_to_dict(database)['dbname'],'data_sha256':sha}}}
    acceptance.seal_manifest(tmp_path,manifest)
    answer=acceptance.execute_trial(tmp_path,trial['trial_id'])
    assert answer['model_calls']>=2 and answer['attempts']
    assert not answer['automatic_business_pass']  # A valid account field is not a full 31-ticket investigation.
    target=tmp_path/'trials'/trial['trial_id']
    assert all((target/f).exists() for f in ('trace.json','report.json','assessment.json','source-validation.json','result.json'))
    with pytest.raises(ValueError,match='already_started'):
        acceptance.execute_trial(tmp_path,trial['trial_id'])


def test_fingerprint_drift_refuses_before_model_construction(monkeypatch):
    monkeypatch.setattr(acceptance,'fingerprint',lambda:{'changed':'x'})
    with pytest.raises(ValueError,match='fingerprint'):
        acceptance.check_manifest({'fingerprint':{},'fixture_definition_sha256':acceptance.hash_value(acceptance.FIXTURES)})


def test_manifest_tampering_is_rejected(tmp_path):
    acceptance.seal_manifest(tmp_path,{'fixture':'frozen'})
    acceptance.write(tmp_path/'manifest.json',{'fixture':'changed'})
    with pytest.raises(ValueError,match='manifest_changed'):
        acceptance.load_manifest(tmp_path)


def test_each_model_request_checks_freeze_before_budget_or_provider(tmp_path,monkeypatch):
    manifest={'fingerprint':{},'fixture_definition_sha256':acceptance.hash_value(acceptance.FIXTURES)}
    acceptance.seal_manifest(tmp_path,manifest)
    monkeypatch.setattr(acceptance,'fingerprint',lambda:{'changed':'yes'})
    class Forbidden:
        def complete(self,*args,**kwargs):
            pytest.fail('No reservation or network after drift')
    with pytest.raises(ValueError,match='fingerprint_changed'):
        acceptance.FrozenModel(Forbidden(),manifest,tmp_path).complete([],[],1)
