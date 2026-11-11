"""Direct HTTP authorization tests; real PostgreSQL, zero provider requests."""

from contextlib import contextmanager
import hashlib
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from enterpriseiq.cases import Cases
from enterpriseiq.jobs import Jobs
from enterpriseiq.web import create_app
from enterpriseiq.web_auth import Identity, load_users


@pytest.fixture
def authorization(conn):
    Jobs(conn).initialize()
    users = {
        hashlib.sha256(name.encode()).hexdigest(): Identity(name, workspace, roles)
        for name, workspace, roles in (
            ('alice', 'demo', ('investigator',)), ('dave', 'demo', ('investigator',)),
            ('dual', 'demo', ('investigator', 'reviewer')), ('bob', 'demo', ('reviewer',)),
            ('erin', 'demo', ('reviewer',)), ('carol', 'demo', ('viewer',)),
            ('mallory', 'other', ('investigator', 'reviewer')),
        )
    }

    @contextmanager
    def connect():
        yield conn

    with TestClient(create_app(connect=connect, users=users)) as client:
        yield client, conn, connect, users


def sign_in(client, name='alice'):
    client.cookies.clear()
    response = client.post('/api/session', json={'token': name})
    assert response.status_code == 200
    return client.cookies.get('enterpriseiq_session')


def start(client):
    response = client.post('/api/jobs', json={'request_id': str(uuid4()), 'question': '调查客户'})
    assert response.status_code == 202
    return response.json()


def verified_case(client, conn, bundle, *, owner='alice'):
    from enterpriseiq.store import import_bundle
    from test_reliability import RecordedRun
    import_bundle(conn, bundle)
    sign_in(client, owner)
    job = start(client)
    recorded = RecordedRun(conn)
    recorded.state['run_id'] = job['run_id']
    ref = recorded.record('account', 'ACC-013')
    checked = recorded.verify([{'kind': 'field', 'evidence_id': ref, 'field': '/account_name', 'value': 'Vantara'}])
    Cases(conn).reconcile({**recorded.state, 'draft_answer': recorded.state['answer'], **checked})
    conn.execute("UPDATE investigation_web.jobs SET status='completed' WHERE job_id=%s", (job['job_id'],))
    case = client.get('/api/cases/' + job['case_id']).json()
    report = case['reports'][-1]
    return job, report, ref


@pytest.mark.parametrize('name,can_start,can_review', [('carol',False,False),('alice',True,False),('bob',False,True),('dual',True,True)])
def test_role_matrix_is_enforced_on_direct_requests(authorization, name, can_start, can_review):
    client, _, _, _ = authorization
    sign_in(client, name)
    created = client.post('/api/jobs', json={'request_id': str(uuid4()), 'question': '调查客户'})
    assert created.status_code == (202 if can_start else 403)
    resumed = client.post(f'/api/runs/{uuid4()}/resume', json={'request_id': str(uuid4())})
    assert resumed.status_code == (404 if can_start else 403)
    for action in ('modify', 'accept', 'reject'):
        reviewed = client.post(f'/api/cases/{uuid4()}/review', json={'request_id': str(uuid4()), 'report_id':str(uuid4()), 'action':action, 'reason':'direct API'})
        assert reviewed.status_code == (404 if can_review else 403)


@pytest.mark.parametrize('resource', ['list', 'case', 'foreign_job', 'unknown_job'])
def test_reads_never_mutate_another_identity_or_workspace_job(authorization, resource):
    client, conn, _, _ = authorization
    sign_in(client, 'mallory')
    foreign = start(client)
    conn.execute("UPDATE investigation_web.jobs SET status='running' WHERE job_id=%s", (foreign['job_id'],))
    before = conn.execute('SELECT job_id,status,error_code,updated_at FROM investigation_web.jobs ORDER BY job_order').fetchall()
    sign_in(client, 'carol')
    paths = {'list':'/api/cases', 'case':'/api/cases/'+foreign['case_id'], 'foreign_job':'/api/jobs/'+foreign['job_id'], 'unknown_job':'/api/jobs/'+str(uuid4())}
    result = client.get(paths[resource])
    assert result.status_code == (200 if resource == 'list' else 404)
    assert conn.execute('SELECT job_id,status,error_code,updated_at FROM investigation_web.jobs ORDER BY job_order').fetchall() == before


def test_own_orphaned_job_read_is_projection_only_and_resume_is_owner_only(authorization):
    client, conn, _, _ = authorization
    sign_in(client)
    job = start(client)
    conn.execute("UPDATE investigation_web.jobs SET status='running' WHERE job_id=%s", (job['job_id'],))
    sign_in(client, 'dave')
    assert client.get('/api/jobs/'+job['job_id']).json()['status'] == 'interrupted'
    assert conn.execute('SELECT status FROM investigation_web.jobs WHERE job_id=%s',(job['job_id'],)).fetchone()[0] == 'running'
    assert client.post('/api/runs/'+job['run_id']+'/resume',json={'request_id':str(uuid4())}).status_code == 403
    sign_in(client)
    assert client.post('/api/runs/'+job['run_id']+'/resume',json={'request_id':str(uuid4())}).status_code == 202


@pytest.mark.parametrize('replacement', [Identity('mallory','other',('reviewer',)), Identity('alice','other',('investigator',)), Identity('alice','demo',('reviewer',))])
def test_existing_session_cannot_inherit_a_rebound_credential(authorization, replacement):
    client, _, connect, users = authorization
    old_session = sign_in(client)
    changed = {**users, hashlib.sha256(b'alice').hexdigest(): replacement}
    with TestClient(create_app(connect=connect, users=changed)) as restarted:
        restarted.cookies.set('enterpriseiq_session', old_session)
        assert restarted.get('/api/me').status_code == 401
        assert restarted.get('/api/cases').status_code == 401
        assert restarted.post('/api/session',json={'token':'alice'}).status_code == 200
        assert restarted.get('/api/me').json()['workspace'] == replacement.workspace


def test_expired_forged_logged_out_and_removed_sessions_are_denied(authorization):
    client, conn, connect, users = authorization
    old_session = sign_in(client)
    conn.execute("UPDATE investigation_web.sessions SET expires_at=now()-interval '1 second'")
    assert client.get('/api/me').status_code == 401
    client.cookies.set('enterpriseiq_session','forged-session')
    assert client.get('/api/cases').status_code == 401
    old_session = sign_in(client)
    assert client.delete('/api/session').status_code == 200
    client.cookies.set('enterpriseiq_session',old_session)
    assert client.get('/api/me').status_code == 401
    old_session = sign_in(client)
    removed = {key:value for key,value in users.items() if value.user_id != 'alice'}
    with TestClient(create_app(connect=connect,users=removed)) as restarted:
        restarted.cookies.set('enterpriseiq_session',old_session)
        assert restarted.get('/api/me').status_code == 401


def test_client_cannot_supply_trusted_identity_or_replay_someone_elses_request(authorization):
    client, conn, _, _ = authorization
    sign_in(client)
    request = {'request_id':str(uuid4()), 'question':'调查客户'}
    job = client.post('/api/jobs',json=request).json()
    before = conn.execute('SELECT count(*) FROM investigation_web.jobs').fetchone()[0]
    sign_in(client,'dave')
    assert client.post('/api/jobs',json=request).status_code == 409
    for key, value in [('actor','alice'),('roles',['reviewer']),('workspace','other'),('initiator','alice')]:
        assert client.post('/api/jobs',json={**request, 'request_id':str(uuid4()),key:value}).status_code == 422
    sign_in(client,'carol')
    assert client.post('/api/jobs',json={'request_id':str(uuid4()),'question':'bypass'},headers={'X-User':'alice','X-Role':'investigator','Authorization':'Bearer alice'}).status_code == 403
    assert conn.execute('SELECT count(*) FROM investigation_web.jobs').fetchone()[0] == before


def test_forged_case_report_run_and_evidence_ids_do_not_cross_resources(authorization, bundle):
    client, conn, _, _ = authorization
    own, report, ref = verified_case(client,conn,bundle)
    sign_in(client,'mallory')
    foreign = start(client)
    foreign_id = foreign['case_id']
    sign_in(client,'bob')
    body={'request_id':str(uuid4()),'report_id':report['report_id'],'action':'accept','reason':'cross resource'}
    assert client.get('/api/cases/'+foreign_id).status_code == 404
    assert client.get('/api/jobs/'+foreign['job_id']).status_code == 404
    assert client.get(f"/api/runs/{foreign['run_id']}/evidence/{ref}").status_code == 404
    assert client.post('/api/cases/'+foreign_id+'/review',json=body).status_code == 404
    assert client.post('/api/cases/'+own['case_id']+'/review',json={**body,'report_id':str(uuid4())}).status_code == 409
    sign_in(client,'alice')
    assert client.post('/api/jobs',json={'request_id':str(uuid4()),'question':'foreign followup','case_id':foreign_id}).status_code == 404
    sign_in(client,'carol')
    assert client.get(f"/api/runs/{own['run_id']}/evidence/{ref}").status_code == 200
    assert client.get(f"/api/runs/{own['run_id']}/evidence/ev-forged").status_code == 404
    assert conn.execute('SELECT count(*) FROM investigation_cases.reviews').fetchone()[0] == 0


@pytest.mark.parametrize('action', ['modify','accept','reject'])
def test_dual_role_cannot_review_any_version_of_own_investigation(authorization, bundle, action):
    client,conn,_,_=authorization
    job,report,_ = verified_case(client,conn,bundle,owner='dual')
    body={'request_id':str(uuid4()),'report_id':report['report_id'],'action':action,'reason':'self review'}
    if action=='modify': body['draft']=json.loads(report['payload']['draft'])
    assert client.post('/api/cases/'+job['case_id']+'/review',json=body).status_code == 403
    assert conn.execute('SELECT count(*) FROM investigation_cases.reviews').fetchone()[0] == 0


def test_review_requires_the_actual_latest_run_to_have_a_trusted_initiator(authorization, bundle):
    from test_reliability import RecordedRun
    client,conn,_,_=authorization
    job,_,_ = verified_case(client,conn,bundle)
    # Trusted CLI may append a run, but HTTP cannot infer who initiated that run.
    recorded=RecordedRun(conn)
    Cases(conn).register(recorded.state,job['case_id'])
    ref=recorded.record('account','ACC-013')
    checked=recorded.verify([{'kind':'field','evidence_id':ref,'field':'/account_name','value':'Vantara'}])
    Cases(conn).reconcile({**recorded.state,'draft_answer':recorded.state['answer'],**checked})
    report=Cases(conn).show(job['case_id'])['reports'][-1]
    sign_in(client,'bob')
    body={'request_id':str(uuid4()),'report_id':report['report_id'],'action':'accept','reason':'unknown initiator'}
    assert client.post('/api/cases/'+job['case_id']+'/review',json=body).status_code == 403
    assert conn.execute('SELECT count(*) FROM investigation_cases.reviews').fetchone()[0] == 0


@pytest.mark.parametrize('patch',[{'user_id':7},{'workspace':False},{'roles':['viewer','viewer']},{'extra':'must reject'}])
def test_invalid_identity_config_fails_closed_without_echoing_credentials(tmp_path, monkeypatch, patch):
    path=tmp_path/'users.json'
    entry={'user_id':'alice','workspace':'demo','roles':['viewer'],'token_sha256':hashlib.sha256(b'local-test-only').hexdigest(),**patch}
    path.write_text(json.dumps([entry]),encoding='utf-8')
    monkeypatch.setenv('ENTERPRISEIQ_USERS_FILE',str(path))
    with pytest.raises(ValueError) as failure:
        load_users()
    assert entry['token_sha256'] not in str(failure.value)


def test_provisioning_same_user_in_two_workspaces_preserves_both_credentials(tmp_path, monkeypatch):
    from enterpriseiq.web_auth import main
    monkeypatch.chdir(tmp_path)
    for workspace in ('one','two'):
        monkeypatch.setattr('sys.argv',['enterpriseiq-web-user','same-user','--workspace',workspace,'--roles','viewer'])
        main()
    files=list((tmp_path/'.secrets').rglob('*.login.txt'))
    assert len(files)==2
    users=load_users()
    identities={users[hashlib.sha256(path.read_text().encode()).hexdigest()].workspace for path in files}
    assert identities=={'one','two'}


@pytest.mark.parametrize('arguments',[
    ['alice','--roles','viewer','viewer'],
    ['a'*65,'--roles','viewer'],
    ['alice','--workspace','  ','--roles','viewer'],
])
def test_invalid_provisioning_cannot_write_a_broken_identity_file(tmp_path, monkeypatch, arguments):
    from enterpriseiq.web_auth import main
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv',['enterpriseiq-web-user',*arguments])
    with pytest.raises(SystemExit):
        main()
    assert not (tmp_path/'.secrets/web-users.json').exists()


def test_investigator_cannot_review_a_revision_created_by_someone_else(authorization, bundle):
    client,conn,_,_=authorization
    job,report,_=verified_case(client,conn,bundle,owner='dual')
    sign_in(client,'bob')
    draft=json.loads(report['payload']['draft'])
    draft['open_questions']=['人工待确认事项']
    amended=client.post('/api/cases/'+job['case_id']+'/review',json={'request_id':str(uuid4()),'report_id':report['report_id'],'action':'modify','reason':'补充待确认','draft':draft})
    assert amended.status_code==200
    revised_id=amended.json()['new_report']['report_id']
    sign_in(client,'dual')
    for action in ('modify','accept','reject'):
        body={'request_id':str(uuid4()),'report_id':revised_id,'action':action,'reason':'禁止变相自审'}
        if action=='modify': body['draft']=draft
        assert client.post('/api/cases/'+job['case_id']+'/review',json=body).status_code==403
    assert conn.execute('SELECT count(*) FROM investigation_cases.reviews').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM investigation_cases.reports').fetchone()[0]==2


def test_old_unbound_sessions_and_role_downgrades_cannot_keep_access(authorization):
    client,conn,connect,users=authorization
    old=sign_in(client)
    conn.execute('UPDATE investigation_web.sessions SET identity_binding=NULL')
    assert client.get('/api/me').status_code==401
    old=sign_in(client)
    changed={**users,hashlib.sha256(b'alice').hexdigest():Identity('alice','demo',('viewer',))}
    with TestClient(create_app(connect=connect,users=changed)) as restarted:
        restarted.cookies.set('enterpriseiq_session',old)
        assert restarted.get('/api/me').status_code==401
        sign_in(restarted)
        assert restarted.get('/api/me').json()['roles']==['viewer']
        assert restarted.post('/api/jobs',json={'request_id':str(uuid4()),'question':'no privilege'}).status_code==403


def test_every_resource_route_requires_a_session_and_denials_have_no_job_side_effect(authorization):
    client,conn,_,_=authorization
    sign_in(client)
    job=start(client)
    conn.execute("UPDATE investigation_web.jobs SET status='running' WHERE job_id=%s",(job['job_id'],))
    client.cookies.clear()
    routes=[
        ('GET','/api/me',None),('GET','/api/cases',None),('GET','/api/cases/'+job['case_id'],None),
        ('GET','/api/jobs/'+job['job_id'],None),('GET',f"/api/runs/{job['run_id']}/evidence/ev-forged",None),
        ('POST','/api/jobs',{'request_id':str(uuid4()),'question':'anonymous'}),
        ('POST',f"/api/runs/{job['run_id']}/resume",{'request_id':str(uuid4())}),
        ('POST',f"/api/cases/{job['case_id']}/review",{'request_id':str(uuid4()),'report_id':str(uuid4()),'action':'accept','reason':'anonymous'}),
    ]
    for method,path,body in routes:
        assert client.request(method,path,json=body).status_code==401
    assert conn.execute('SELECT status FROM investigation_web.jobs WHERE job_id=%s',(job['job_id'],)).fetchone()[0]=='running'
    assert conn.execute('SELECT count(*) FROM investigation_web.jobs').fetchone()[0]==1


def test_legacy_unmapped_case_cannot_be_read_or_adopted_by_forging_its_id(authorization):
    from enterpriseiq.agent import initial_state
    client,conn,_,_=authorization
    state=initial_state('Legacy CLI investigation','offline')
    legacy=Cases(conn).register(state)
    sign_in(client)
    assert client.get('/api/cases/'+legacy).status_code==404
    assert client.get('/api/cases').json()==[]
    assert client.post('/api/jobs',json={'request_id':str(uuid4()),'case_id':legacy,'question':'adopt'}).status_code==404
    assert conn.execute('SELECT count(*) FROM investigation_web.jobs').fetchone()[0]==0
