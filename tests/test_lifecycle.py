"""Maintenance in disposable PostgreSQL only; no paid model or production cleanup."""
from datetime import datetime, timedelta, timezone
import json
import os
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from enterpriseiq.agent import build_agent, execute_run, initial_state, postgres_checkpointer, run_lock
from enterpriseiq.cases import Cases, digest
from enterpriseiq.lifecycle import maintain
from test_workbench_worker import database, SourceModel


def archived_run(database, directory, *, paused=False):
    from enterpriseiq.tools import BusinessTools
    state=initial_state('查看 ACC-013 客户名称','offline-fixture')
    with psycopg.connect(database,autocommit=True) as case_conn,psycopg.connect(database) as conn,postgres_checkpointer(database) as saver:
        cases=Cases(case_conn)
        case=cases.register(state)
        graph=build_agent(SourceModel(),BusinessTools(conn),saver,pause_after_tool=paused)
        result=execute_run(graph,state=state,on_result=cases.reconcile)
        if not paused:
            report=cases.show(case)['reports'][0]
            cases.review(case,report['report_id'],'accept','fixture-reviewer','source checked',uuid4())
        before=cases.show(case)
    directory.mkdir(exist_ok=True)
    path=directory/(result['run_id']+'.json')
    path.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    return result,case,before,path


def test_dry_run_then_cleanup_keeps_report_review_trace_and_sources(database,tmp_path):
    result,case,before,path=archived_run(database,tmp_path)
    future=datetime.now(timezone.utc)+timedelta(days=100)
    plan=maintain(database,outputs=tmp_path,reason='fixture retention check',now=future)
    assert plan['dry_run'] and path.exists()
    assert plan['candidates']['traces']==[result['run_id']]
    assert plan['candidates']['checkpoints']==[result['run_id']]
    applied=maintain(database,outputs=tmp_path,reason='fixture retention check',now=future,apply=True)
    assert applied['status']=='completed' and not path.exists()
    with psycopg.connect(database) as conn,postgres_checkpointer(database) as saver:
        after=Cases(conn).show(case)
        assert digest(after)==digest(before)
        assert not saver.get_tuple({'configurable':{'thread_id':result['run_id']}})
        with pytest.raises(ValueError,match='checkpoint_retired'):
            execute_run(build_agent(None,None,saver),run_id=result['run_id'])
        from enterpriseiq.reliability import ReportVerifier
        assert ReportVerifier(conn).verify({**result,'answer':result['draft_answer']})['report_validation']=='passed'
        assert conn.execute('SELECT count(*) FROM investigation_maintenance.operations').fetchone()[0]==2
        from enterpriseiq.jobs import Jobs
        from enterpriseiq.web_auth import Identity
        conn.execute('INSERT INTO investigation_web.cases VALUES (%s,%s)',(case,'demo'))
        conn.execute('INSERT INTO investigation_web.runs VALUES (%s,%s)',(result['run_id'],'alice'))
        identity=Identity('alice','demo',('viewer',))
        shown=Jobs(conn).show_case(identity,case)
        assert shown['reports']==after['reports'] and shown['runs'][0]['trace']
        evidence_id=next(iter(result['evidence']))
        assert Jobs(conn).evidence(identity,result['run_id'],evidence_id)['content']
    again=maintain(database,outputs=tmp_path,reason='idempotent cleanup',now=future,apply=True)
    assert not again['candidates']['traces'] and not again['candidates']['checkpoints']


@pytest.mark.parametrize('change',['paused','mismatched_trace','recent','unprojected'])
def test_uncertain_or_resumable_records_are_protected(database,tmp_path,change):
    result,case,_,path=archived_run(database,tmp_path,paused=change=='paused')
    future=datetime.now(timezone.utc)+timedelta(days=100)
    if change=='mismatched_trace':
        path.write_text('{}',encoding='utf-8')
    if change=='unprojected':
        with psycopg.connect(database) as conn:
            conn.execute('UPDATE investigation_cases.runs SET state=NULL WHERE run_id=%s',(result['run_id'],))
    plan=maintain(database,outputs=tmp_path,reason='protected fixture',now=datetime.now(timezone.utc) if change=='recent' else future,apply=True)
    assert result['run_id'] not in plan['candidates']['traces']
    assert path.exists()
    if change!='mismatched_trace':
        assert result['run_id'] not in plan['candidates']['checkpoints']


def test_application_identity_cannot_run_even_dry_run_or_create_audit(database,tmp_path):
    role='m3_unprivileged_'+uuid4().hex
    with psycopg.connect(database,autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
        try:
            conn.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
            with pytest.raises(PermissionError,match='maintenance'):
                maintain(database,outputs=tmp_path,reason='forged actor',connection=conn)
        finally:
            conn.execute('RESET ROLE')
            assert not conn.execute("SELECT to_regclass('investigation_maintenance.operations')").fetchone()[0]
            conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def test_cleanup_rejects_active_worker(database,tmp_path):
    from enterpriseiq.jobs import WORKER_LOCK
    with psycopg.connect(database,autocommit=True) as conn:
        conn.execute('SELECT pg_advisory_lock(%s)',(WORKER_LOCK,))
        try:
            with pytest.raises(ValueError,match='worker'):
                maintain(database,outputs=tmp_path,reason='worker active',apply=True)
        finally:
            conn.execute('SELECT pg_advisory_unlock(%s)',(WORKER_LOCK,))


def test_non_run_files_and_changed_content_are_never_deleted(database,tmp_path):
    _,_,_,path=archived_run(database,tmp_path)
    other=tmp_path/'manifest.json'
    other.write_text('{}')
    path.write_text('{}')
    plan=maintain(database,outputs=tmp_path,reason='mismatch',now=datetime.now(timezone.utc)+timedelta(days=100),apply=True)
    assert path.exists() and other.exists() and not plan['candidates']['traces']


def test_frozen_acceptance_index_pins_trace_and_checkpoint_even_after_retention(database,tmp_path):
    result,_,_,path=archived_run(database,tmp_path)
    history=tmp_path/'m2-final-fixture'
    history.mkdir()
    (history/'results.json').write_text(json.dumps([{'run_id':result['run_id']}]))
    plan=maintain(database,outputs=tmp_path,reason='frozen history',now=datetime.now(timezone.utc)+timedelta(days=100),apply=True)
    assert not plan['candidates']['traces'] and not plan['candidates']['checkpoints'] and path.exists()


def test_active_cli_run_lock_protects_checkpoint_and_trace(database,tmp_path):
    result,_,_,path=archived_run(database,tmp_path)
    with postgres_checkpointer(database) as saver:
        graph=build_agent(None,None,saver)
        with run_lock(graph,result['run_id']):
            plan=maintain(database,outputs=tmp_path,reason='concurrent CLI',now=datetime.now(timezone.utc)+timedelta(days=100),apply=True)
    assert not plan['candidates']['traces'] and not plan['candidates']['checkpoints'] and path.exists()


def test_filesystem_failure_preserves_audit_intent_and_checkpoint(database,tmp_path,monkeypatch):
    from pathlib import Path
    result,_,_,path=archived_run(database,tmp_path)
    def fail_unlink(self,*args,**kwargs):
        raise OSError('fixture unlink failure')
    with monkeypatch.context() as local:
        local.setattr(Path,'unlink',fail_unlink)
        with pytest.raises(OSError):
            maintain(database,outputs=tmp_path,reason='injected filesystem error',now=datetime.now(timezone.utc)+timedelta(days=100),apply=True)
    with psycopg.connect(database) as conn,postgres_checkpointer(database) as saver:
        assert conn.execute('SELECT status FROM investigation_maintenance.operations').fetchone()[0]=='failed'
        assert conn.execute('SELECT status FROM investigation_maintenance.actions').fetchone()[0]=='intent'
        assert saver.get_tuple({'configurable':{'thread_id':result['run_id']}})
        with pytest.raises(psycopg.Error):
            with conn.transaction():
                conn.execute('DELETE FROM investigation_maintenance.operations')
    assert path.exists()


@pytest.mark.parametrize('pin',['none','registered','orphan_checkpoint','orphan_trace'])
def test_only_expired_unreferenced_nonbaseline_snapshot_is_removed(database,bundle,tmp_path,pin):
    from enterpriseiq.store import import_bundle
    from enterpriseiq.dataset import digest as bytes_digest,read_bundle,SNAPSHOT_ID
    from test_ingestion_contract import changed_archive
    path,_=changed_archive(bundle,tmp_path,'combined')
    changed=read_bundle(path,expected_sha256=bytes_digest(path.read_bytes()))
    snapshot='fixture-'+changed.archive_sha256[:16]
    with psycopg.connect(database,autocommit=True) as conn:
        import_bundle(conn,changed,snapshot)
        state=initial_state('fixture reference','offline-fixture')
        state['snapshot_id']=snapshot
        if pin=='registered':
            Cases(conn).register(state)
        elif pin=='orphan_checkpoint':
            with postgres_checkpointer(database) as saver:
                graph=build_agent(None,None,saver)
                graph.update_state({'configurable':{'thread_id':state['run_id']}},state)
        elif pin=='orphan_trace':
            (tmp_path/(state['run_id']+'.json')).write_text(json.dumps(state),encoding='utf-8')
    result=maintain(database,outputs=tmp_path,reason='snapshot fixture',now=datetime.now(timezone.utc)+timedelta(days=100),apply=True)
    assert (snapshot in result['candidates']['snapshots'])==(pin=='none')
    with psycopg.connect(database) as conn:
        remaining={r[0] for r in conn.execute('SELECT snapshot_id FROM enterpriseiq.snapshots')}
        assert SNAPSHOT_ID in remaining
        assert (snapshot in remaining)==(pin!='none')


def test_later_source_corruption_is_explicit_and_does_not_rewrite_accepted_history(database,tmp_path):
    from fastapi import HTTPException
    from enterpriseiq.jobs import Jobs
    from enterpriseiq.web_auth import Identity
    from enterpriseiq.reliability import ReportVerifier
    result,case,before,_=archived_run(database,tmp_path)
    evidence_id=next(iter(result['evidence']))
    ref=result['evidence'][evidence_id]
    with psycopg.connect(database) as conn:
        conn.execute('INSERT INTO investigation_web.cases VALUES (%s,%s)',(case,'demo'))
        conn.execute('INSERT INTO investigation_web.runs VALUES (%s,%s)',(result['run_id'],'alice'))
        conn.execute("UPDATE enterpriseiq.source_files SET body='source unavailable' WHERE snapshot_id=%s AND path=%s",(ref['snapshot_id'],ref['source_path']))
        checked=ReportVerifier(conn).verify({**result,'answer':result['draft_answer']})
        assert checked['report_validation']=='blocked'
        with pytest.raises(HTTPException) as exc:
            Jobs(conn).evidence(Identity('alice','demo',('viewer',)),result['run_id'],evidence_id)
        assert exc.value.status_code==409
        assert digest(Cases(conn).show(case))==digest(before)
