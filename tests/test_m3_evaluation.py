"""M3 replay and recovery tests are offline; no provider requests."""

import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import evaluate_m3 as evaluation
from test_reliability import RecordedRun


def replay_fixture(tmp_path, populated):
    run=RecordedRun(populated)
    ref=run.record('account','ACC-013')
    checked=run.verify([{'kind':'field','evidence_id':ref,'field':'/account_name','value':'Vantara'}])
    state={**run.state,'draft_answer':run.state['answer'],**checked}
    outputs=tmp_path/'outputs'
    directory=outputs/'m2-final-fixture'
    directory.mkdir(parents=True)
    trace=outputs/(state['run_id']+'.json')
    trace.write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8')
    (directory/'manifest.json').write_text(json.dumps({'scenarios':[{'id':'sales-l2-b'}],'repetitions':1}),encoding='utf-8')
    row={'run_id':state['run_id'],'scenario':'sales-l2-b','business_pass':False,'report_pass':True}
    (directory/'results.json').write_text(json.dumps([row]),encoding='utf-8')
    return directory,trace


def test_offline_replay_separates_business_score_source_validation_and_review(tmp_path,populated,monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('Offline replay must not construct a paid model')
    monkeypatch.setattr('enterpriseiq.model.DeepSeekModel.__init__',forbidden)
    directory,trace=replay_fixture(tmp_path,populated)
    original={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (trace,directory/'results.json',directory/'manifest.json')}
    result=evaluation.evaluate_batch(populated,directory,root=tmp_path)
    row=result['rows'][0]
    assert row['original']['report_pass'] is True
    assert row['source_validation']['passed'] is True
    assert row['automatic_business']['business_pass'] is False
    assert row['review']['performed_this_run'] is False
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==sha for p,sha in original.items())


def test_missing_or_malformed_trace_is_an_explicit_result_not_dropped(tmp_path,populated):
    directory,trace=replay_fixture(tmp_path,populated)
    trace.unlink()
    missing=evaluation.evaluate_batch(populated,directory,root=tmp_path)
    assert len(missing['rows'])==1 and missing['rows'][0]['error_code']=='missing_trace'
    trace.write_text('{broken',encoding='utf-8')
    malformed=evaluation.evaluate_batch(populated,directory,root=tmp_path)
    assert malformed['rows'][0]['error_code']=='invalid_trace_json'
    assert malformed['errors']==1


def test_truncated_results_and_unknown_scenarios_are_not_silently_accepted(tmp_path,populated):
    directory,trace=replay_fixture(tmp_path,populated)
    (directory/'manifest.json').write_text(json.dumps({'scenarios':[{'id':'sales-l2-b'}],'repetitions':2}),encoding='utf-8')
    (directory/'results.json').write_text(json.dumps([{'run_id':trace.stem,'scenario':'unknown','business_pass':True}]),encoding='utf-8')
    result=evaluation.evaluate_batch(populated,directory,root=tmp_path)
    assert 'result_count_mismatch' in result['input_issues']
    assert result['rows'][0]['error_code']=='unsupported_scenario'


def test_replay_refuses_a_trace_whose_identity_disagrees_with_the_result_row(tmp_path,populated):
    directory,trace=replay_fixture(tmp_path,populated)
    state=json.loads(trace.read_text(encoding='utf-8'))
    state['run_id']=str(uuid4())
    trace.write_text(json.dumps(state),encoding='utf-8')
    result=evaluation.evaluate_batch(populated,directory,root=tmp_path)
    assert result['rows'][0]['error_code']=='trace_identity_mismatch'


def test_frozen_scorer_modification_is_refused(tmp_path):
    (tmp_path/'scripts').mkdir()
    (tmp_path/'scripts/accept_m1.py').write_text('changed',encoding='utf-8')
    with pytest.raises(ValueError,match='frozen_scoring_changed'):
        evaluation.verify_frozen_scoring(tmp_path)
