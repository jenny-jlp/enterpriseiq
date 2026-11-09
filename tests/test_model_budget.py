"""Budget gate exercises real PostgreSQL reservations; external calls are local fixtures."""

from uuid import uuid4

import pytest

from enterpriseiq.budget import BudgetLedger
from enterpriseiq.model import ModelError


def ledger(conn, **changes):
    value = BudgetLedger(conn, name="test-" + uuid4().hex, **changes)
    value.initialize()
    return value


def test_budget_reserves_before_call_and_refuses_next_attempt(conn):
    budget = ledger(conn, max_calls=1, max_tokens=100, max_cny="1")
    run = str(uuid4())
    attempt = budget.reserve(run, 40, 10)
    with pytest.raises(ModelError, match="budget"):
        budget.reserve(run, 40, 10)
    budget.settle(attempt, {"prompt_tokens": 4, "completion_tokens": 2})
    status = budget.summary()
    assert status['attempts'] == 1 and status['known_tokens'] == 6
    assert status['charged_tokens'] == 6 and status['runs'] == 1


def test_unknown_failed_attempt_keeps_reservation_across_recovery(conn):
    budget = ledger(conn, max_tokens=90)
    run = str(uuid4())
    budget.reserve(run, 50, 10)
    recovered = BudgetLedger(conn, name=budget.name, max_tokens=90)
    with pytest.raises(ModelError, match="budget"):
        recovered.reserve(run, 40, 10)
    summary = recovered.summary()
    assert summary['unknown_attempts'] == 1 and summary['charged_tokens'] == 60
    assert summary['known_tokens'] == 0


def test_money_run_and_smoke_caps_are_independent(conn):
    money = ledger(conn, max_cny="0.00001")
    with pytest.raises(ModelError, match="budget"):
        money.reserve(str(uuid4()), 1, 10)
    runs = ledger(conn, max_runs=1)
    runs.reserve(str(uuid4()), 1, 1)
    with pytest.raises(ModelError, match="budget"):
        runs.reserve(str(uuid4()), 1, 1)
    smoke = ledger(conn, max_smoke_runs=1)
    run = str(uuid4())
    smoke.reserve(run, 1, 1)
    smoke.reserve(run, 1, 1)
    with pytest.raises(ModelError, match="budget"):
        smoke.reserve(str(uuid4()), 1, 1)


def test_settlement_is_idempotent_and_missing_usage_remains_unknown(conn):
    budget = ledger(conn)
    attempt = budget.reserve(str(uuid4()), 30, 10)
    budget.settle(attempt, {})
    assert budget.summary()['unknown_attempts'] == 1
    budget.settle(attempt, {"prompt_tokens": 2, "completion_tokens": 1})
    budget.settle(attempt, {"prompt_tokens": 2, "completion_tokens": 1})
    assert budget.summary()['known_tokens'] == 3
