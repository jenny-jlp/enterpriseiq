import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from paid_eval import BudgetStop, guarded_cli, main
from test_workbench_worker import database


@pytest.mark.parametrize('result',[
    subprocess.CompletedProcess([],0,json.dumps({'run_id':'fixture','status':'failed','stop_reason':'model_budget_exhausted'}),''),
    subprocess.CompletedProcess([],1,'','Model configuration error: model_budget_exhausted'),
])
def test_budget_stop_escapes_legacy_exception_handler_and_retains_failure(tmp_path,result):
    with pytest.raises(BudgetStop):
        try:
            guarded_cli(lambda _:result,tmp_path)(['investigate','private question not retained'])
        except Exception:
            pytest.fail('Legacy runner must not swallow budget stop')
    row=json.loads((tmp_path/'invocations.jsonl').read_text())
    assert row['budget_stop']
    assert 'private question' not in (tmp_path/'invocations.jsonl').read_text()


def test_default_paid_eval_entry_only_checks_budget_without_model_calls(database,monkeypatch,capsys):
    monkeypatch.setenv('BUDGET_DATABASE_URL',database)
    monkeypatch.setattr('sys.argv',['paid_eval.py','--suite','m1'])
    monkeypatch.setattr('enterpriseiq.budget.DeepSeekModel',lambda:pytest.fail('No provider during preflight'))
    main()
    plan=json.loads(capsys.readouterr().out)
    assert plan['tier']=='budget_preflight_no_model_calls' and plan['new_provider_requests']==0
    assert plan['used']['runs']==plan['used']['attempts']==0
