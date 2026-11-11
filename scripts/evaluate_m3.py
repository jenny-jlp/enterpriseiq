"""Offline report revalidation, frozen business scoring and provenance; never runs a model.

Evaluation-only module: not shipped as an Agent tool or ingested business data.
Original traces, results and reviews are read-only. Each invocation writes a new directory.
"""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg

import accept_m1 as m1
import accept_m2 as m2
from enterpriseiq.reliability import ReportVerifier

ROOT=Path(__file__).resolve().parents[1]
FROZEN_SCORERS={
    'scripts/accept_m1.py':'805e915782bab4dd42a11610b37411ee2afb1baef4ceeab726eae4c5329712f3',
    'scripts/accept_m2.py':'4c42fbd3c3e8490a6493f1e0f3a79d77741c169726be0b39f98afc86f7ef1bdc',
}
FROZEN_SCENARIO_IDS={s['id'] for s in m1.SCENARIOS+m2.NEW_SCENARIOS}
# Catalog for deterministic fixtures now; future paid repetitions need their own budget gate.
NEW_SCENARIOS=[
    {'id':'m3-account-scope','question':'逐一调查 ACC-004 的全部开放 P2 支持工单：列出主题、直接产品组件、客户名称和 ARR，以及未完成工程事项数量与代表记录；候选关联不等于根因，代表记录不冒充全部明细。',
     'coverage':['held_out_customer','priority_and_entity_scope','pagination','tool_failure','exact_amounts']},
    {'id':'m3-evidence-gap','question':'调查 ACC-004 是否已经完成修复、是否存在承诺 ETA；证据不足时列出待确认事项，不推定承诺日期或行动完成。',
     'coverage':['insufficient_evidence','unsupported_completion','untrusted_source_instruction']},
    {'id':'m3-source-conflict','question':'结合不同原始来源调查 ISS-032 的承诺 ETA；保留来源矛盾，不自行选取一个日期作为已确认结论。',
     'coverage':['source_conflict','issue_attribution','explicit_unknown']},
]
SCENARIO_IDS=FROZEN_SCENARIO_IDS|{s['id'] for s in NEW_SCENARIOS}


def grade_product_contract(scenario,state,checked,baseline):
    """Deterministic product expectations, including correct abstention; not semantic judging."""
    report=checked['report']
    gaps={g['code'] for g in report.get('gaps',[])}
    if scenario=='m3-account-scope':
        expected={t['ticket_id'] for t in baseline['tickets'] if t['account_id']=='ACC-004' and t['status']=='open' and t['priority']=='p2'}
        rows=[r for table in report.get('tables',[]) if table['view']=='ticket_contexts' for r in table['rows']]
        account=baseline['accounts']['ACC-004']
        checks={'verified_report':checked['report_validation']=='passed',
                'exact_customer_p2_set':len(rows)==len(expected) and {r['ticket_id'] for r in rows}==expected,
                'customer_and_arr':bool(rows) and all(r['account_id']=='ACC-004' and r['arr']==str(account['arr']) for r in rows)}
    elif scenario=='m3-evidence-gap':
        refs=state.get('evidence',{})
        checks={'no_formal_unsupported_conclusion':checked['report_validation']=='blocked' and report.get('formal_report') is None,
                'explicit_evidence_gap':bool(gaps&{'tool_failure','eta_not_explicitly_supported'}),
                'no_fabricated_claims':not report.get('rejected_claims'),
                'pending_questions_retained':bool(report.get('open_questions_unverified')),
                'customer_evidence_retained':any(refs.get(c['claim'].get('evidence_id'),{}).get('source_id')=='ACC-004' for c in report.get('verified_claims',[]))}
    elif scenario=='m3-source-conflict':
        checks={'no_formal_conflicting_eta':checked['report_validation']=='blocked' and report.get('formal_report') is None,
                'issue_conflict_retained':any(c.get('subject_id')=='ISS-032' for c in report.get('conflicts',[])),
                'source_conflict_explicit':'source_conflict' in gaps,
                'no_other_fabricated_claims':all(c['code']=='conflicting_eta_sources' for c in report.get('rejected_claims',[]))}
    else:
        raise ValueError('unsupported_scenario')
    return {'business_pass':all(checks.values()),'report_pass':checked['report_validation']=='passed',
            'checks':checks,'track':'m3_deterministic_contract_not_llm_semantic_quality'}


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_frozen_scoring(root=ROOT):
    for name,expected in FROZEN_SCORERS.items():
        if file_hash(root/name)!=expected:
            raise ValueError('frozen_scoring_changed')


def evaluate_batch(conn,directory,*,root=ROOT):
    """Revalidate saved drafts, never resume a checkpoint or replace a historical score."""
    directory=Path(directory)
    manifest_path,results_path=directory/'manifest.json',directory/'results.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    originals=json.loads(results_path.read_text(encoding='utf-8'))
    if not isinstance(originals,list):
        raise ValueError('invalid_results_structure')
    baseline=m1.raw_baseline()
    inputs={str(p.relative_to(root)):file_hash(p) for p in (manifest_path,results_path)}
    input_issues=[]
    expected=len(manifest.get('scenarios',[]))*manifest.get('repetitions',0)
    if expected and expected!=len(originals):
        input_issues.append('result_count_mismatch')
    for field,name in (('frozen_m1_scoring_sha256','scripts/accept_m1.py'),('m2_evaluator_sha256','scripts/accept_m2.py')):
        if manifest.get(field) is not None and manifest[field]!=FROZEN_SCORERS[name]:
            input_issues.append('historical_scorer_mismatch:'+field)
    reviews=[]
    for name in ('source-review.json','manual-review.json','grading-review.json','reviewed_results.json'):
        path=directory/name
        if path.exists():
            inputs[str(path.relative_to(root))]=file_hash(path)
            reviews.append({'path':str(path.relative_to(root)),'sha256':file_hash(path),
                            'classification':'historical_record_not_a_new_review'})
    rows=[]
    seen=set()
    for original in originals:
        if not isinstance(original,dict):
            rows.append({'original':original,'source_validation':None,'automatic_business':None,
                         'review':{'performed_this_run':False},'error_code':'invalid_result_row'})
            continue
        row={'run_id':original.get('run_id'),'scenario':original.get('scenario'),'repetition':original.get('repetition'),
             'original':original,'source_validation':None,'automatic_business':None,
             'review':{'performed_this_run':False,'historical_records':reviews}}
        try:
            run_id=str(UUID(original['run_id']))
            if run_id in seen:
                raise ValueError('duplicate_run_id')
            seen.add(run_id)
            if original.get('scenario') not in SCENARIO_IDS:
                raise ValueError('unsupported_scenario')
            path=root/'outputs'/(run_id+'.json')
            if not path.is_file():
                raise ValueError('missing_trace')
            inputs[str(path.relative_to(root))]=file_hash(path)
            try:
                state=json.loads(path.read_text(encoding='utf-8'))
            except (ValueError,UnicodeError):
                raise ValueError('invalid_trace_json') from None
            if not isinstance(state,dict) or state.get('run_id')!=run_id:
                raise ValueError('trace_identity_mismatch')
            row['recorded_engine_version']=state.get('engine_version')
            state['answer']=state.get('draft_answer',state.get('answer',''))
            # This is current source verification, not historical engine execution.
            checked=ReportVerifier(conn).verify(state)
            row['source_validation']={'passed':checked['report_validation']=='passed',
                'status':checked['status'],'gap_codes':[g['code'] for g in checked['report'].get('gaps',[])],
                'rejected_claim_codes':[g['code'] for g in checked['report'].get('rejected_claims',[])],
                'verified_claim_count':len(checked['report'].get('verified_claims',[]))}
            rescored={**state,**checked}
            if original['scenario'].startswith('m3-'):
                grade=grade_product_contract(original['scenario'],state,checked,baseline)
            else:
                grade=m2.grade_new(original['scenario'],rescored,baseline) if original['scenario'].startswith('m2-') else m1.grade(original['scenario'],rescored,baseline)
                grade={**grade,'track':'unchanged_m1_m2_frozen'}
            row['automatic_business']={**grade,'scorers_sha256':FROZEN_SCORERS}
        except (ValueError,KeyError,TypeError,IndexError) as exc:
            known={'duplicate_run_id','unsupported_scenario','missing_trace','invalid_trace_json','trace_identity_mismatch'}
            row['error_code']=str(exc) if str(exc) in known else 'replay_input_or_verification_error'
            row['error_type']=type(exc).__name__
        rows.append(row)
    changed=[name for name,sha in inputs.items() if not (root/name).is_file() or file_hash(root/name)!=sha]
    if changed:
        input_issues.append('input_changed_during_replay')
    return {'batch':directory.name,'rows':rows,'input_issues':input_issues,'input_sha256':inputs,
            'inputs_unchanged':not changed,'errors':sum('error_code' in row for row in rows),
            'counts':{'recorded_runs':len(rows),
                'original_report_pass':sum(bool(row.get('report_pass')) for row in originals if isinstance(row,dict)),
                'original_automatic_business_pass':sum(bool(row.get('business_pass')) for row in originals if isinstance(row,dict)),
                'current_source_pass':sum(bool((r['source_validation'] or {}).get('passed')) for r in rows),
                'current_frozen_grader_business_pass':sum(bool((r['automatic_business'] or {}).get('business_pass')) for r in rows)}}


def main():
    argparse.ArgumentParser(description='Offline-only M1/M2 trace replay; no model execution or checkpoint resume.').parse_args()
    verify_frozen_scoring()
    now=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    output=ROOT/'outputs'/f'm3-offline-{now}-{uuid4().hex[:8]}'
    output.mkdir(parents=True,exist_ok=False)
    batches=[]
    directories=sorted((ROOT/'outputs').glob('m[12]-final-*'))
    execution_error=None
    try:
        with psycopg.connect(os.environ['DATABASE_URL'],connect_timeout=5) as conn:
            with conn.transaction(force_rollback=True):
                conn.execute('SET TRANSACTION READ ONLY')
                for directory in directories:
                    try:
                        batch=evaluate_batch(conn,directory)
                    except (ValueError,KeyError,OSError,TypeError) as exc:
                        batch={'batch':directory.name,'error_type':type(exc).__name__,'error_code':'batch_input_unreadable','rows':[],'errors':1,'input_issues':['batch_input_unreadable']}
                    batches.append(batch)
                    print(json.dumps({'batch':batch['batch'],'counts':batch.get('counts'),'errors':batch['errors']},ensure_ascii=False),flush=True)
    except (psycopg.Error,KeyError) as exc:
        execution_error={'code':'offline_database_unavailable','error_type':type(exc).__name__}
    result={'tier':'offline_revalidation','new_provider_requests':0,'batches':batches,'frozen_scorers':FROZEN_SCORERS,'execution_error':execution_error,
            'unprocessed_batches':[d.name for d in directories if d.name not in {b['batch'] for b in batches}],
            'policy':'All recorded rows retained. No model, checkpoint resume, reference-answer retrieval or new human review. Current source verification and frozen automatic scoring are distinct. Historical scores/reviews are unchanged; mixed engine batches are not a production success-rate estimate.'}
    (output/'replay.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    verify_frozen_scoring()
    print('Offline artifacts: '+str(output),flush=True)
    return 1 if execution_error or not directories or any(b['errors'] or b['input_issues'] for b in batches) else 0


if __name__=='__main__':
    raise SystemExit(main())
