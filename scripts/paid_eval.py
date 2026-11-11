"""Optional legacy live-suite gate; default only checks/initializes the budget.

Frozen scorers/runners stay byte-identical. This is NOT M3's future 21-run freeze.
"""
import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg

import accept_m1 as m1
import accept_m2 as m2
from evaluate_m3 import FROZEN_SCORERS, ROOT, file_hash
from enterpriseiq.budget import BudgetLedger, budget_dsn
from enterpriseiq.model import ModelError


class BudgetStop(BaseException):
    """Escape the immutable runners' catch-all Exception loops on a budget stop."""


def guarded_cli(runner, directory):
    def execute(arguments):
        result = runner(arguments)
        try:
            payload=json.loads(result.stdout)
        except (ValueError,TypeError):
            payload={}
        reason=payload.get('stop_reason','')
        budget_stop=reason.startswith('model_budget_') or 'model_budget_' in (result.stderr or '')
        # Do not retain stderr, credentials, question text or raw provider bodies.
        row={'command':arguments[0], 'exit_code':result.returncode,'run_id':payload.get('run_id'),
             'status':payload.get('status'),'budget_stop':budget_stop,
             'error_code':reason if reason.startswith('model_budget_') else 'budget_gate_rejected' if budget_stop else None}
        with (directory/'invocations.jsonl').open('a',encoding='utf-8') as handle:
            handle.write(json.dumps(row)+'\n')
        if budget_stop:
            raise BudgetStop()
        return result
    return execute


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite',choices=['m1','m2'],required=True)
    parser.add_argument('--live',action='store_true',help='Explicitly enable a legacy paid batch; not needed for offline verification')
    args=parser.parse_args()
    for relative,digest in FROZEN_SCORERS.items():
        if file_hash(ROOT/relative)!=digest:
            parser.error('Frozen evaluator changed')
    try:
        with psycopg.connect(budget_dsn()) as conn:
            ledger=BudgetLedger(conn)
            ledger.initialize()
            plan={'suite':args.suite,'tier':'legacy_paid_freeze' if args.live else 'budget_preflight_no_model_calls',
                  'planned_runs':9 if args.suite=='m1' else 15,'policy':ledger.policy,
                  'used':ledger.summary(),'remaining':ledger.remaining(),'new_provider_requests':0}
            if args.live and any(plan['remaining'][k]<=0 for k in ('runs','freeze_runs','calls','tokens','cny')):
                raise ModelError('model_budget_exhausted')
    except ModelError as exc:
        parser.exit(2,exc.code+'\n')
    except psycopg.Error:
        parser.exit(2,'Budget database unavailable\n')
    print(json.dumps(plan,default=str),flush=True)
    if not args.live:
        return
    directory=ROOT/'outputs'/('paid-gate-'+uuid4().hex)
    directory.mkdir(parents=True)
    (directory/'preflight.json').write_text(json.dumps(plan,default=str,indent=2),encoding='utf-8')
    original,phase=m1.cli,os.environ.get('ENTERPRISEIQ_BUDGET_PHASE')
    m1.cli=guarded_cli(original,directory)
    os.environ['ENTERPRISEIQ_BUDGET_PHASE']='freeze'
    try:
        (m1 if args.suite=='m1' else m2).main()
    except BudgetStop:
        parser.exit(2,'Budget stopped the suite; invocation retained at '+str(directory)+'\n')
    finally:
        m1.cli=original
        if phase is None:
            os.environ.pop('ENTERPRISEIQ_BUDGET_PHASE',None)
        else:
            os.environ['ENTERPRISEIQ_BUDGET_PHASE']=phase


if __name__=='__main__':
    main()
