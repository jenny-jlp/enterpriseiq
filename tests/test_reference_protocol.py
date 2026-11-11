"""Reference-location regressions; no provider construction or paid requests."""
from copy import deepcopy
import json
import os
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict,make_conninfo
import pytest

from enterpriseiq.reliability import ReportVerifier,ReferenceClaim,repair_feedback,report_instructions
from test_reliability import RecordedRun
from test_m3_scenarios import inject_document


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    def forbidden(*args,**kwargs):
        pytest.fail('Reference protocol tests must never construct a provider')
    monkeypatch.setattr('enterpriseiq.model.DeepSeekModel.__init__',forbidden)


def reference_fixture(conn,text='ISS-017 is explicitly mentioned here; this is not a root-cause assertion.'):
    inject_document(conn,'REF-PROTOCOL',text)
    run=RecordedRun(conn,'核对来源中的文字关联')
    target=run.record('issue','ISS-017')
    metadata=run.record('document','REF-PROTOCOL')
    body=run.call('read_document',{'entity_type':'document','source_id':'REF-PROTOCOL'})['evidence']['evidence_id']
    return run,target,metadata,body


def test_reference_schema_supports_document_lines_without_a_record_field():
    claim=ReferenceClaim(kind='reference',evidence_id='observed-lines',field=None,target_evidence_id='observed-target')
    assert claim.field is None
    assert ReferenceClaim(kind='reference',evidence_id='observed-lines',target_evidence_id='observed-target').field is None


def test_valid_body_reference_checks_literal_target_and_renders_line_location(populated):
    run,target,_,body=reference_fixture(populated)
    result=run.verify([{'kind':'reference','evidence_id':body,'field':None,'target_evidence_id':target}])
    assert result['report_validation']=='passed'
    rendered=result['report']['formal_report']
    assert 'lines:1-1' in rendered and 'None' not in rendered
    assert '不自动推断因果' in rendered


@pytest.mark.parametrize('location',['metadata_on_body','null_on_record','metadata_path_not_mention'])
def test_record_and_document_locations_cannot_be_silently_substituted(populated,location):
    run,target,metadata,body=reference_fixture(populated)
    source,field,code={
        'metadata_on_body':(body,'/content_file','document_has_no_record_field'),
        'null_on_record':(metadata,None,'invalid_text_field'),
        'metadata_path_not_mention':(metadata,'/content_file','explicit_reference_not_found'),
    }[location]
    result=run.verify([{'kind':'reference','evidence_id':source,'field':field,'target_evidence_id':target}])
    assert result['report_validation']=='blocked'
    assert any(c['code']==code for c in result['report']['rejected_claims'])


@pytest.mark.parametrize('text',['No issue identifier is recorded here.','ISS-0170 is a different identifier.'])
def test_document_reference_requires_exact_literal_id_without_inference(populated,text):
    run,target,_,body=reference_fixture(populated,text)
    result=run.verify([{'kind':'reference','evidence_id':body,'field':None,'target_evidence_id':target}])
    assert result['report_validation']=='blocked'
    assert result['report']['rejected_claims'][0]['code']=='explicit_reference_not_found'


def test_valid_original_record_text_reference_remains_valid(populated):
    run=RecordedRun(populated,'核对文字关联')
    source=run.record('opportunity','OPP-006')
    target=run.record('issue','ISS-017')
    checked=run.verify([{'kind':'reference','evidence_id':source,'field':'/description','target_evidence_id':target}])
    assert checked['report_validation']=='passed'


def test_line_range_cannot_borrow_a_target_identifier_from_another_excerpt(populated):
    inject_document(populated,'REF-RANGE','No entity identifier on this line.\nISS-017 is mentioned only on this second line.')
    run=RecordedRun(populated,'核对原文位置')
    target=run.record('issue','ISS-017')
    first=run.call('read_document',{'entity_type':'document','source_id':'REF-RANGE','line_count':1})['evidence']['evidence_id']
    second=run.call('read_document',{'entity_type':'document','source_id':'REF-RANGE','start_line':2,'line_count':1})['evidence']['evidence_id']
    claim={'kind':'reference','evidence_id':first,'field':None,'target_evidence_id':target}
    checked=run.verify([claim])
    assert checked['report']['rejected_claims'][0]['code']=='explicit_reference_not_found'
    assert run.verify([{**claim,'evidence_id':second}])['report_validation']=='passed'


@pytest.mark.parametrize('tampering',['source_hash','unobserved_target'])
def test_document_protocol_preserves_source_integrity_and_successful_tool_lineage(populated,tampering):
    run,target,_,body=reference_fixture(populated)
    if tampering=='source_hash':
        message=run.state['messages'][-1]
        value=json.loads(message['content'])
        value['evidence']['file_sha256']='0'*64
        message['content']=json.dumps(value)
        run.state['evidence'][body]['file_sha256']='0'*64
        code='document_source_mismatch'
    else:
        target='unobserved-target'
        run.state['evidence'][target]={'source_id':'ISS-017'}
        code='citation_not_from_successful_tool'
    checked=run.verify([{'kind':'reference','evidence_id':body,'field':None,'target_evidence_id':target}])
    assert checked['report_validation']=='blocked'
    assert any(r['code']==code for r in checked['report']['rejected_claims'])


def test_feedback_identifies_location_and_target_without_fetching_or_fabricating_evidence(populated):
    run,target,_,body=reference_fixture(populated)
    claim={'kind':'reference','evidence_id':body,'field':'/content_file','target_evidence_id':target}
    checked=run.verify([claim])
    before=deepcopy(run.state)
    detail=repair_feedback(run.state,checked['report'])['rejected_assertions'][0]
    assert detail['rejected_claim']==claim and detail['evidence_kind']=='document_lines'
    assert detail['expected_field'] is None and detail['target_source_id']=='ISS-017'
    assert detail['source_location']['start_line']==1
    assert 'field=null' in detail['explanation']
    assert run.state==before
    instructions=report_instructions()
    assert 'json_pointer' in instructions and 'start_line' in instructions


@pytest.fixture
def frozen_trace():
    root=Path('outputs/m3-final-20261111')
    if not (root/'manifest.json').exists() or not os.environ.get('DATABASE_URL'):
        pytest.skip('Local immutable M3 trace/data environment not present')
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    def load(trial,fixture):
        state=json.loads((root/'trials'/trial/'trace.json').read_text(encoding='utf-8'))
        params=conninfo_to_dict(os.environ['DATABASE_URL'])
        params['dbname']=manifest['databases'][fixture]['name']
        return state,make_conninfo(**params)
    return load


def test_actual_conflict_trace_still_rejects_the_original_wrong_field(frozen_trace):
    state,dsn=frozen_trace('m3-source-conflict-r1','conflict')
    with psycopg.connect(dsn) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        checked=ReportVerifier(conn).verify({**state,'answer':state['draft_answer']})
    assert checked['report_validation']=='blocked'
    assert any(c['code']=='document_has_no_record_field' for c in checked['report']['rejected_claims'])
    assert checked['report']['conflicts']


def test_actual_conflict_trace_with_only_reference_location_corrected_stays_partial(frozen_trace):
    state,dsn=frozen_trace('m3-source-conflict-r1','conflict')
    draft=json.loads(state['draft_answer'])
    rejected=next(r for r in state['report']['rejected_claims'] if r['code']=='document_has_no_record_field')
    draft['claims'][rejected['index']]['field']=None
    with psycopg.connect(dsn) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        checked=ReportVerifier(conn).verify({**state,'answer':json.dumps(draft)})
    assert checked['report_validation']=='blocked' and checked['status']=='partial'
    assert checked['report']['formal_report'] is None and checked['report']['conflicts']
    assert {r['code'] for r in checked['report']['rejected_claims']}=={'conflicting_eta_sources'}


def test_actual_vantara_false_explicit_reference_stays_rejected_and_original_correction_passes(frozen_trace):
    state,dsn=frozen_trace('m2-vantara-r1','baseline')
    first=state['report_attempts'][0]['draft']
    with psycopg.connect(dsn) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        checked=ReportVerifier(conn).verify({**state,'answer':first})
        feedback=repair_feedback({**state,'answer':first},checked['report'])
        corrected=ReportVerifier(conn).verify({**state,'answer':state['draft_answer']})
    assert checked['report_validation']=='blocked'
    detail=next(r for r in feedback['rejected_assertions'] if r['code']=='explicit_reference_not_found')
    assert detail['evidence_kind']=='record' and detail['target_source_id']=='ISS-017'
    assert 'literal' in detail['explanation']
    assert corrected['report_validation']=='passed'
    assert state['report_attempts'][0]['validation']['report']['rejected_claims'][0]['code']=='explicit_reference_not_found'


@pytest.mark.parametrize('correct_location',[False,True])
def test_actual_conflict_context_does_not_gain_a_retry_or_publish_a_report(frozen_trace,correct_location):
    from time import time
    from uuid import uuid4
    from langgraph.checkpoint.memory import InMemorySaver
    from enterpriseiq.agent import build_agent,execute_run
    from enterpriseiq.model import Completion
    from enterpriseiq.tools import BusinessTools
    state,dsn=frozen_trace('m3-source-conflict-r1','conflict')
    draft=json.loads(state['draft_answer'])
    if correct_location:
        rejected=next(r for r in state['report']['rejected_claims'] if r['code']=='document_has_no_record_field')
        draft['claims'][rejected['index']]['field']=None
    class RecordedDraft:
        calls=0
        def complete(self,*args,**kwargs):
            self.calls+=1
            return Completion({'role':'assistant','content':json.dumps(draft)}, {},'offline-recorded-draft','offline','stop')
    model=RecordedDraft()
    state.update(run_id=str(uuid4()),deadline=time()+30,remaining_seconds=30,model_calls=0,trace={},
                 report_attempts=[],report_repair_count=0,needs_report_repair=False,report={},
                 report_validation='pending',answer='',draft_answer='',stop_reason='',status='running')
    with psycopg.connect(dsn) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        graph=build_agent(model,BusinessTools(conn),InMemorySaver())
        result=execute_run(graph,state=state)
    assert model.calls==1 and len(result['report_attempts'])==1
    assert result['report_repair_count']==0
    assert result['status']=='partial' and result['report_validation']=='blocked'
    assert result['report']['formal_report'] is None and result['report']['conflicts']
