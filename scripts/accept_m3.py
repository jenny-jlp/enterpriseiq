"""M3 21 fresh trials. Default prepares only; --live requires a frozen manifest.

Evaluation glue only. No grader, Agent or business-tool implementation is changed.
"""
import argparse
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from uuid import UUID,uuid4
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

import accept_m1 as m1
import accept_m2 as m2
import evaluate_m3 as m3
from enterpriseiq.agent import build_agent,execute_run,initial_state,postgres_checkpointer,save_result,ENGINE_VERSION,_snapshot_result
from enterpriseiq.budget import BudgetLedger,BudgetedModel,budget_dsn
from enterpriseiq.cases import Cases
from enterpriseiq.dataset import SNAPSHOT_ID,ARCHIVE_SHA256,read_bundle,digest
from enterpriseiq.jobs import Jobs
from enterpriseiq.model import ModelError
from enterpriseiq.performance import ExecutionMetrics,Timed,execution_history
from enterpriseiq.reliability import ReportVerifier
from enterpriseiq.tools import BusinessTools

ROOT=Path(__file__).resolve().parents[1]
LOCK=73931245
FIXTURES={'baseline':{'version':1},'gap':{'version':1,'fail_tools':['list_documents','read_document'],
    'error':'source_unavailable','retryable':False},
    'conflict':{'version':1,'documents':{'M3-CONFLICT-0':'ISS-032 No ETA committed.',
                                      'M3-CONFLICT-1':'ISS-032 ETA: 2026-12-01.'}}}
CRITICAL_CODES={'wrong_amount','invalid_amount','wrong_account_set','calculation_source_mismatch',
    'citation_identity_mismatch','citation_not_from_successful_tool','record_source_mismatch','document_source_mismatch',
    'quote_not_in_original_source','unsupported_component_relation','eta_subject_not_supported','explicit_reference_not_found',
    'derived_collection_value_mismatch','inconsistent_collection_row','wrong_collection_source'}
ENTITY_FIELDS={'/account_id','/account_name','/ticket_id','/issue_id','/opportunity_id','/part_id','/components'}


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


def write(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    temporary.replace(path)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_manifest(directory):
    directory=Path(directory)
    path=directory/'manifest.json'
    if digest(path.read_bytes())!=(directory/'manifest.sha256').read_text(encoding='utf-8').strip():
        raise ValueError('frozen_manifest_changed')
    return read(path)


def seal_manifest(directory,manifest):
    write(Path(directory)/'manifest.json',manifest)
    (Path(directory)/'manifest.sha256').write_text(digest((Path(directory)/'manifest.json').read_bytes()),encoding='utf-8')


def hash_value(value):
    return digest(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str).encode())


def error_code(exc):
    if isinstance(exc,ModelError):
        return exc.code
    message=str(exc)
    return message if re.fullmatch('[a-z][a-z0-9_]{1,100}',message) else type(exc).__name__


def fingerprint():
    paths=set()
    for directory in ('src','scripts','tests','web/src'):
        paths.update(p for p in (ROOT/directory).rglob('*') if p.is_file() and p.suffix in ('.py','.sql','.ts','.tsx','.css','.html'))
    paths.update(ROOT/p for p in ('pyproject.toml','uv.lock','web/package.json','web/package-lock.json','AGENTS.md'))
    return {p.relative_to(ROOT).as_posix():digest(p.read_bytes()) for p in sorted(paths) if p.exists()}


def plan():
    scenarios=[]
    trials=[]
    for s in m1.SCENARIOS+m2.NEW_SCENARIOS+m3.NEW_SCENARIOS:
        sid=s['id']
        repetitions=2 if sid.startswith('m3-') else 3
        fixture='gap' if sid=='m3-evidence-gap' else 'conflict' if sid=='m3-source-conflict' else 'baseline'
        scenarios.append({**s,'repetitions':repetitions,'fixture':fixture,
            'scorer':'evaluate_m3.grade_product_contract' if sid.startswith('m3-') else 'accept_m2.grade_new' if sid.startswith('m2-') else 'accept_m1.grade'})
        for repetition in range(1,repetitions+1):
            trials.append({'trial_id':f'{sid}-r{repetition}','scenario':sid,'repetition':repetition,
                'fixture':fixture,'run_id':str(uuid4()),'status':'not_started',
                'checkpoint_test':sid=='sales-l2-b' and repetition==3})
    assert len(trials)==21
    return scenarios,trials


def data_dsn(database):
    parameters=conninfo_to_dict(os.environ['DATABASE_URL'])
    parameters['dbname']=database
    return make_conninfo(**parameters)


def data_hash(conn):
    files=conn.execute('SELECT path,sha256,body FROM enterpriseiq.source_files WHERE snapshot_id=%s ORDER BY path',(SNAPSHOT_ID,)).fetchall()
    records=conn.execute('SELECT source_system,entity_type,source_id,source_path,json_pointer,record_sha256,data FROM enterpriseiq.records WHERE snapshot_id=%s ORDER BY 1,2,3',(SNAPSHOT_ID,)).fetchall()
    relations=conn.execute('SELECT from_system,from_type,from_id,to_system,to_type,to_id,relation_type,source_field FROM enterpriseiq.relations WHERE snapshot_id=%s ORDER BY 1,2,3,4,5,6,7,8',(SNAPSHOT_ID,)).fetchall()
    links=conn.execute('SELECT source_system,entity_type,source_id,content_path FROM enterpriseiq.document_links WHERE snapshot_id=%s ORDER BY 1,2,3,4',(SNAPSHOT_ID,)).fetchall()
    assert all(digest(body.encode())==sha for _,sha,body in files)
    return hash_value([files,records,relations,links])


def seed_fixture(conn,fixture):
    if fixture!='conflict':
        return
    for source_id,text in FIXTURES['conflict']['documents'].items():
        content_path=f'internal_docs/{source_id}.txt'
        metadata_path=f'internal_docs/{source_id}.json'
        record={'document_id':source_id,'content_file':source_id+'.txt','status':'published'}
        raw=json.dumps([record])
        canonical=json.dumps(record,sort_keys=True,ensure_ascii=False,separators=(',',':'))
        for path,body in ((content_path,text),(metadata_path,raw)):
            conn.execute('INSERT INTO enterpriseiq.source_files VALUES (%s,%s,%s,%s)',(SNAPSHOT_ID,path,digest(body.encode()),body))
        conn.execute("INSERT INTO enterpriseiq.records VALUES (%s,'files','document',%s,%s,'/0',%s,%s)",
                     (SNAPSHOT_ID,source_id,metadata_path,digest(canonical.encode()),Jsonb(record)))
        conn.execute("INSERT INTO enterpriseiq.document_links VALUES (%s,'files','document',%s,%s)",(SNAPSHOT_ID,source_id,content_path))


class FixtureTools(BusinessTools):
    def __init__(self,conn,fixture):
        super().__init__(conn)
        self.fixture=fixture

    def execute(self,name,arguments):
        if self.fixture=='gap' and name in FIXTURES['gap']['fail_tools']:
            return self._error('source_unavailable','Evaluation fixture: original document service is unavailable',retryable=False)
        return super().execute(name,arguments)


class FrozenModel:
    """Check the sealed freeze before every provider reservation, not just each trial."""
    def __init__(self,target,manifest,directory):
        self.target,self.manifest,self.directory=target,manifest,directory

    def for_attempt(self,number):
        return FrozenModel(self.target.for_attempt(number),self.manifest,self.directory)

    def complete(self,*args,**kwargs):
        load_manifest(self.directory)
        check_manifest(self.manifest)
        return self.target.complete(*args,**kwargs)


def budget_status():
    with psycopg.connect(budget_dsn()) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        ledger=BudgetLedger(conn)
        row=conn.execute('SELECT policy,reservation_breach FROM investigation_budget.ledgers WHERE name=%s',('m3',)).fetchone()
        if not row or row[0]!=ledger.policy or row[1]:
            raise ModelError('model_budget_policy_or_reservation_blocked')
        return {'policy':ledger.policy,'used':ledger.summary(),'remaining':ledger.remaining()}


def authority_hash():
    parameters=conninfo_to_dict(budget_dsn())
    return hash_value({k:v for k,v in parameters.items() if k not in ('password','sslpassword','passfile')})


def check_manifest(manifest):
    m3.verify_frozen_scoring()
    if fingerprint()!=manifest['fingerprint'] or hash_value(FIXTURES)!=manifest['fixture_definition_sha256']:
        raise ValueError('freeze_fingerprint_changed')
    scenarios,_=plan()
    if scenarios!=manifest['scenarios'] or authority_hash()!=manifest['budget_authority_sha256']:
        raise ValueError('freeze_plan_or_budget_authority_changed')
    _,expected=plan()
    fields=('trial_id','scenario','repetition','fixture','checkpoint_test')
    if [[r[k] for k in fields] for r in manifest['trials']]!=[[r[k] for k in fields] for r in expected]:
        raise ValueError('freeze_trial_sequence_changed')
    if len({str(UUID(r['run_id'])) for r in manifest['trials']})!=21:
        raise ValueError('freeze_run_identity_invalid')
    if any(not item['name'].startswith('enterpriseiq_m3_') for item in manifest['databases'].values()):
        raise ValueError('isolated_evaluation_database_required')
    if os.environ.get('DEEPSEEK_MODEL','deepseek-flash')!='deepseek-flash':
        raise ValueError('freeze_model_changed')


def prepare(directory):
    from enterpriseiq.store import import_bundle
    m3.verify_frozen_scoring()
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=False)
    scenarios,trials=plan()
    budget=budget_status()
    if budget['remaining']['freeze_runs']<21 or any(budget['remaining'][k]<=0 for k in ('runs','calls','tokens','cny')):
        raise ModelError('model_budget_exhausted')
    bundle=read_bundle(ROOT/'data/enterprise-bench-data.zip')
    manifest={'created_at':now(),'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'worktree':subprocess.check_output(['git','status','--short'],text=True),'engine_version':ENGINE_VERSION,
        'snapshot_id':SNAPSHOT_ID,'archive_sha256':ARCHIVE_SHA256,'model':'deepseek-flash',
        'scenarios':scenarios,'trials':trials,'fixtures':FIXTURES,'fixture_definition_sha256':hash_value(FIXTURES),
        'fingerprint':fingerprint(),'budget_before':budget,'budget_authority_sha256':authority_hash(),
        'critical_codes':sorted(CRITICAL_CODES),'critical_field_mismatch_fields':sorted(ENTITY_FIELDS|{'/arr','/acv'}),
        'criteria':{'business_min':19,'denominator':21,'critical_errors_max':0},
        'policy':'All 21 fresh trials count. No selective reruns. Existing graders unchanged. Expected safe partial is business success only under the frozen M3 contract. Codex review is not independent human review.',
        'databases':{},'phase':'prepared_not_live','new_provider_requests_at_preparation':0}
    write(directory/'manifest.json',manifest)
    write(directory/'results.json',trials)
    for fixture in FIXTURES:
        name='enterpriseiq_m3_final_'+uuid4().hex[:20]
        manifest['databases'][fixture]={'name':name,'status':'creating'}
        write(directory/'manifest.json',manifest)
        with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        with psycopg.connect(data_dsn(name)) as conn:
            import_bundle(conn,bundle)
            Jobs(conn).initialize()
            seed_fixture(conn,fixture)
            manifest['databases'][fixture].update(status='ready',data_sha256=data_hash(conn))
        write(directory/'manifest.json',manifest)
    manifest['phase']='frozen_ready'
    manifest['fingerprint_sha256']=hash_value(manifest['fingerprint'])
    seal_manifest(directory,manifest)
    return manifest


def assess(conn,state,scenario):
    baseline=m1.raw_baseline()
    checked=ReportVerifier(conn).verify({**state,'answer':state.get('draft_answer',state.get('answer',''))})
    grade=(m3.grade_product_contract(scenario,state,checked,baseline) if scenario.startswith('m3-') else
           m2.grade_new(scenario,state,baseline) if scenario.startswith('m2-') else m1.grade(scenario,state,baseline))
    critical=[]
    schema_rejections=[]
    for number,attempt in enumerate(state.get('report_attempts',[]),1):
        report=attempt['validation']['report']
        try:
            claims=json.loads(attempt['draft']).get('claims',[])
        except (ValueError,AttributeError):
            claims=[]
        for item in report.get('rejected_claims',[]):
            index=item.get('index',-1)
            claim=claims[index] if isinstance(index,int) and 0<=index<len(claims) else {}
            code=item['code']
            target=critical if code in CRITICAL_CODES or (code=='field_value_not_supported' and claim.get('field') in ENTITY_FIELDS|{'/arr','/acv'}) else schema_rejections
            target.append({'attempt':number,'code':code,'claim':claim})
        for gap in report.get('gaps',[]):
            if gap['code'] in CRITICAL_CODES|{'source_file_integrity','invalid_source_reference'}:
                critical.append({'attempt':number,'code':gap['code'],'evidence_id':gap.get('evidence_id')})
    return {'automatic_business':grade,'source_validation':checked,'critical_errors':critical,
            'other_rejections':schema_rejections,'codex_review':{'status':'pending','independent_human':False}}


def execute_trial(directory,trial_id,*,resume=False):
    directory=Path(directory)
    manifest=load_manifest(directory)
    check_manifest(manifest)
    trial=next(t for t in manifest['trials'] if t['trial_id']==trial_id)
    scenario=next(s for s in manifest['scenarios'] if s['id']==trial['scenario'])
    target=directory/'trials'/trial_id
    target.mkdir(parents=True,exist_ok=True)
    dsn=data_dsn(manifest['databases'][trial['fixture']]['name'])
    with psycopg.connect(dsn) as conn:
        if data_hash(conn)!=manifest['databases'][trial['fixture']]['data_sha256']:
            raise ValueError('freeze_data_changed')
    model=BudgetedModel(budget_dsn(),trial['run_id'],phase='freeze')
    with psycopg.connect(dsn) as conn,psycopg.connect(dsn,autocommit=True) as case_conn,postgres_checkpointer(dsn) as saver:
        cases=Cases(case_conn)
        existing=case_conn.execute('SELECT case_id FROM investigation_cases.runs WHERE run_id=%s',(trial['run_id'],)).fetchone()
        if not resume and existing:
            raise ValueError('trial_already_started_no_replacement')
        if resume and (not existing or not trial['checkpoint_test']):
            raise ValueError('only_planned_same_run_checkpoint_resume_allowed')
        state=initial_state(scenario['question'],'deepseek-flash')
        state['run_id']=trial['run_id']
        case_id=str(existing[0]) if existing else cases.register(state)
        write(target/('resume-start.json' if resume else 'start.json'),{'run_id':trial['run_id'],'case_id':case_id,'at':now()})
        model.prepare(entrypoint='m3_final_acceptance')
        metrics=ExecutionMetrics(model.dsn,trial['run_id'],'m3_final','live')
        with metrics:
            graph=build_agent(Timed(FrozenModel(model,manifest,directory),metrics,'model_adapter'),Timed(FixtureTools(conn,trial['fixture']),metrics,'tool'),saver,
                pause_after_tool=trial['checkpoint_test'] and not resume,
                verifier=Timed(ReportVerifier(conn),metrics,'validation'))
            result=execute_run(graph,run_id=trial['run_id'],on_result=cases.reconcile) if resume else execute_run(graph,state=state,on_result=cases.reconcile)
            path=save_result(result)
            write(target/('trace-paused.json' if result['status']=='paused' else 'trace.json'),result)
            if result['status']!='paused':
                started=time.perf_counter()
                assessed=assess(conn,result,trial['scenario'])
                write(target/'assessment.json',assessed)
                write(target/'source-validation.json',assessed['source_validation'])
                write(target/'report.json',result['report'])
                source_seconds=time.perf_counter()-started
            else:
                assessed={}
                source_seconds=None
            metrics.finish(result['status'])
            with psycopg.connect(budget_dsn()) as budget_conn:
                attempts=BudgetLedger(budget_conn).run_attempts(trial['run_id'])
                timing=execution_history(budget_conn,trial['run_id'])
            answer={**trial,'case_id':case_id,'status':result['status'],'stop_reason':result.get('stop_reason'),
                'model_calls':result['model_calls'],'tool_calls':result['tool_calls'],'prompt_tokens':result['prompt_tokens'],
                'completion_tokens':result['completion_tokens'],'usage_incomplete':result['usage_incomplete'],
                'attempts':attempts,'timings':timing,'independent_source_check_seconds':source_seconds,
                'automatic_business_pass':assessed.get('automatic_business',{}).get('business_pass',False),
                'report_validation':result['report_validation'],'critical_error_count':len(assessed.get('critical_errors',[])),
                'trace_path':str(path.resolve().relative_to(ROOT)),'ended_at':now()}
            write(target/('paused-result.json' if result['status']=='paused' else 'result.json'),answer)
            return answer


def invoke_child(directory,trial,resume=False):
    arguments=[sys.executable,str(Path(__file__).resolve()),'--execute-trial',trial['trial_id'],'--directory',str(directory)]
    if resume:
        arguments.append('--resume')
    try:
        process=subprocess.run(arguments,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=260)
    except subprocess.TimeoutExpired:
        process=subprocess.CompletedProcess(arguments,-1,'{"error_code":"trial_process_timeout"}','')
    result_file=Path(directory)/'trials'/trial['trial_id']/('result.json' if resume or not trial['checkpoint_test'] else 'paused-result.json')
    if not result_file.exists():
        # A planned pause may terminate before its first tool. That is still a result.
        result_file=Path(directory)/'trials'/trial['trial_id']/'result.json'
    if process.returncode or not result_file.exists():
        error='trial_process_failed'
        for line in process.stdout.splitlines():
            try:
                value=json.loads(line)
                error=value.get('error_code',error)
            except ValueError:
                pass
        failure={**trial,'status':'interrupted','error_code':error,'automatic_business_pass':False,'exit_code':process.returncode}
        target=Path(directory)/'trials'/trial['trial_id']
        # Preserve the last durable checkpoint even if a child died before projection.
        manifest=load_manifest(directory)
        dsn=data_dsn(manifest['databases'][trial['fixture']]['name'])
        with postgres_checkpointer(dsn) as saver:
            snapshot=build_agent(None,None,saver).get_state({'configurable':{'thread_id':trial['run_id']}})
            if snapshot.values:
                write(target/'trace-interrupted.json',_snapshot_result(snapshot))
                failure['checkpoint_preserved']=True
        with psycopg.connect(budget_dsn()) as conn:
            failure['attempts']=BudgetLedger(conn).run_attempts(trial['run_id'])
            failure['timings']=execution_history(conn,trial['run_id'])
        write(target/'failure.json',failure)
        return failure
    return read(result_file)


def drive(trials,executor,persist,preflight):
    rows=[dict(t) for t in trials]
    for i,trial in enumerate(trials):
        try:
            preflight()
            rows[i]={**trial,'status':'starting','started_at':now()}
            persist(rows)
            start=time.perf_counter()
            result=executor(trial,False)
            if result['status']=='paused':
                result=executor(trial,True)
                result['checkpoint_restored_same_run']=result['run_id']==trial['run_id']
            rows[i]={**result,'end_to_end_seconds':time.perf_counter()-start}
            persist(rows)
            reason=result.get('stop_reason') or result.get('error_code') or ''
            if reason.startswith(('model_budget_','model_http_')) or reason in ('freeze_fingerprint_changed','freeze_data_changed','missing_deepseek_key','model_timeout','model_connection_error') or result['status']=='interrupted':
                raise RuntimeError(reason or 'trial_interrupted')
        except BaseException as exc:
            # The active trial remains an actual failure/partial, never replaced.
            if rows[i]['status'] in ('starting','not_started'):
                rows[i]={**rows[i],'status':'interrupted' if rows[i]['status']=='starting' else 'not_started',
                         'error_code':error_code(exc),'automatic_business_pass':False}
            for j in range(i+1,len(rows)):
                rows[j]={**rows[j],'status':'not_started','not_executed_reason':error_code(exc),'automatic_business_pass':False}
            persist(rows)
            return rows
    return rows


def run_batch(directory,executor=None):
    directory=Path(directory)
    manifest=load_manifest(directory)
    check_manifest(manifest)
    if manifest['phase']!='frozen_ready' or (directory/'execution.json').exists():
        raise ValueError('batch_may_start_only_once')
    with psycopg.connect(budget_dsn(),autocommit=True) as control:
        if not control.execute('SELECT pg_try_advisory_lock(%s)',(LOCK,)).fetchone()[0]:
            raise ValueError('another_acceptance_batch_is_running')
        try:
            if budget_status()['used']!=manifest['budget_before']['used']:
                raise ValueError('budget_changed_since_freeze')
            write(directory/'execution.json',{'status':'running','started_at':now(),'authorized_live':True})
            def preflight():
                load_manifest(directory)
                check_manifest(manifest)
                remaining=budget_status()['remaining']
                if any(remaining[k]<=0 for k in ('freeze_runs','runs','calls','tokens','cny')):
                    raise ModelError('model_budget_exhausted')
            rows=drive(manifest['trials'],executor or (lambda t,r:invoke_child(directory,t,r)),
                       lambda values:write(directory/'results.json',values),preflight)
            with psycopg.connect(budget_dsn()) as conn:
                attempts=[a for row in rows for a in BudgetLedger(conn).run_attempts(row['run_id'])]
            summary={'ended_at':now(),
                'started_trials':sum(r['status']!='not_started' for r in rows),
                'business_pass':sum(bool(r.get('automatic_business_pass')) for r in rows),'denominator':21,
                'statuses':{s:sum(r['status']==s for r in rows) for s in sorted({r['status'] for r in rows})},
                'critical_errors':sum(r.get('critical_error_count',0) for r in rows),
                'model_attempts':len(attempts),'charged_tokens':sum(a['charged_tokens'] for a in attempts),
                'estimated_cny':str(sum((Decimal(str(a['charged_cny'])) for a in attempts),Decimal(0))),
                'budget_after':budget_status(),'codex_review':'pending','independent_human_review':'not_performed',
                'final_acceptance':'pending_review_not_closed'}
            with psycopg.connect(budget_dsn()) as conn:
                summary['actual_admitted_runs']=conn.execute('SELECT count(*) FROM investigation_budget.runs WHERE ledger=%s AND run_id=ANY(%s::uuid[])',('m3',[r['run_id'] for r in rows])).fetchone()[0]
            write(directory/'summary.json',summary)
            write(directory/'execution.json',{'status':'stopped' if any(r['status']=='not_started' for r in rows) else 'finished','ended_at':now()})
            return summary
        finally:
            control.execute('SELECT pg_advisory_unlock(%s)',(LOCK,))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    choice=parser.add_mutually_exclusive_group()
    choice.add_argument('--prepare',action='store_true')
    choice.add_argument('--live',action='store_true')
    choice.add_argument('--execute-trial')
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    try:
        if args.prepare:
            result=prepare(args.directory)
            print(json.dumps({'status':result['phase'],'trials':21,'new_provider_requests':0,'fingerprint':result['fingerprint_sha256']}))
        elif args.execute_trial:
            execute_trial(args.directory,args.execute_trial,resume=args.resume)
        elif args.live:
            print(json.dumps(run_batch(args.directory),default=str),flush=True)
        else:
            check_manifest(load_manifest(args.directory))
            print(json.dumps({'status':'preflight_ok_no_model_calls','budget':budget_status()},default=str))
    except Exception as exc:
        code=error_code(exc)
        # Never print exception bodies, SQL, HTTP content or connection strings.
        print(json.dumps({'error_code':code}),flush=True)
        sys.exit(1)


if __name__=='__main__':
    main()
