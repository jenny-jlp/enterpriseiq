"""Durable M3 pre-call gate. Unknown/failed attempts retain their full reservation.

Rate source: https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
Verified 2026-11-09; deepseek-flash peak/cache-miss rates, CNY per million.
These conservative estimates are not provider billing statements.
"""

from decimal import Decimal
from importlib.resources import files
import json
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from enterpriseiq.model import DeepSeekModel, ModelError


class BudgetLedger:
    def __init__(self, conn, *, name="m3", max_calls=180, max_tokens=1_200_000, max_cny="30", max_runs=30,
                 max_smoke_runs=9, max_freeze_runs=21):
        self.conn, self.name = conn, name
        self.policy = dict(max_calls=max_calls, max_tokens=max_tokens, max_cny=str(max_cny), max_runs=max_runs,
                           max_smoke_runs=max_smoke_runs, max_freeze_runs=max_freeze_runs,
                           model="deepseek-flash", price_date="2026-11-09", input_per_million="2", output_per_million="8", currency="CNY")

    def initialize(self):
        with self.conn.transaction():
            self.conn.execute(files("enterpriseiq").joinpath("budget.sql").read_text(encoding="utf-8"))
            self.conn.execute("INSERT INTO investigation_budget.ledgers(name,policy) VALUES (%s,%s) ON CONFLICT DO NOTHING", (self.name, Jsonb(self.policy)))
            stored = self.conn.execute("SELECT policy FROM investigation_budget.ledgers WHERE name=%s", (self.name,)).fetchone()[0]
            if stored != self.policy:
                raise ModelError("model_budget_policy_mismatch")

    def cost(self, prompt, completion):
        return (Decimal(prompt) * Decimal(self.policy['input_per_million']) + Decimal(completion) * Decimal(self.policy['output_per_million'])) / Decimal(1_000_000)

    def summary(self):
        row = self.conn.execute("""SELECT count(*),count(DISTINCT run_id),coalesce(sum(charged_tokens),0),coalesce(sum(charged_cny),0),
            coalesce(sum(coalesce(prompt_tokens,0)+coalesce(completion_tokens,0)),0),count(*) FILTER (WHERE prompt_tokens IS NULL)
            FROM investigation_budget.attempts WHERE ledger=%s""", (self.name,)).fetchone()
        return dict(attempts=row[0], runs=row[1], charged_tokens=int(row[2]), charged_cny=str(row[3]), known_tokens=int(row[4]), unknown_attempts=row[5])

    def reserve(self, run_id, input_tokens, output_tokens, *, phase="smoke"):
        if phase not in ("smoke", "freeze") or input_tokens < 0 or output_tokens < 1:
            raise ModelError("model_budget_invalid_reservation")
        with self.conn.transaction():
            stored = self.conn.execute("SELECT policy,reservation_breach FROM investigation_budget.ledgers WHERE name=%s FOR UPDATE", (self.name,)).fetchone()
            if not stored or stored[0] != self.policy or stored[1]:
                raise ModelError("model_budget_policy_or_reservation_blocked")
            totals = self.summary()
            run = self.conn.execute("SELECT phase FROM investigation_budget.attempts WHERE ledger=%s AND run_id=%s LIMIT 1", (self.name, run_id)).fetchone()
            phase_runs = self.conn.execute("SELECT count(DISTINCT run_id) FROM investigation_budget.attempts WHERE ledger=%s AND phase=%s", (self.name, phase)).fetchone()[0]
            price = self.cost(input_tokens, output_tokens)
            if (totals['attempts'] >= self.policy['max_calls']
                    or totals['charged_tokens'] + input_tokens + output_tokens > self.policy['max_tokens']
                    or Decimal(totals['charged_cny']) + price > Decimal(self.policy['max_cny'])
                    or (not run and (totals['runs'] >= self.policy['max_runs'] or phase_runs >= self.policy['max_' + phase + '_runs']))
                    or (run and run[0] != phase)):
                raise ModelError("model_budget_exhausted")
            attempt = str(uuid4())
            self.conn.execute("""INSERT INTO investigation_budget.attempts(attempt_id,ledger,run_id,phase,reserved_input,reserved_output,charged_tokens,charged_cny)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (attempt, self.name, run_id, phase, input_tokens, output_tokens, input_tokens + output_tokens, price))
            return attempt

    def settle(self, attempt, usage):
        prompt, completion = usage.get('prompt_tokens'), usage.get('completion_tokens')
        if type(prompt) is not int or type(completion) is not int or prompt < 0 or completion < 0:
            return  # Unknown is charged at the reserved maximum, never assumed free.
        with self.conn.transaction():
            row = self.conn.execute("SELECT reserved_input,reserved_output,prompt_tokens FROM investigation_budget.attempts WHERE attempt_id=%s AND ledger=%s FOR UPDATE", (attempt, self.name)).fetchone()
            if not row or row[2] is not None:
                return
            self.conn.execute("""UPDATE investigation_budget.attempts SET prompt_tokens=%s,completion_tokens=%s,charged_tokens=%s,charged_cny=%s
                WHERE attempt_id=%s""", (prompt, completion, prompt + completion, self.cost(prompt, completion), attempt))
            if prompt > row[0] or completion > row[1]:
                self.conn.execute("UPDATE investigation_budget.ledgers SET reservation_breach=true WHERE name=%s", (self.name,))


class BudgetedModel:
    def __init__(self, dsn, run_id, *, phase="smoke"):
        self.dsn, self.run_id, self.phase = dsn, run_id, phase

    def complete(self, messages, tools, timeout):
        # Byte-count bound + framing allowance; no dependency on an approximate tokenizer.
        # Provider usage settles down to known counts; an underestimate blocks future calls.
        estimated_input = len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode('utf-8')) + 2048
        with psycopg.connect(self.dsn) as conn:
            budget = BudgetLedger(conn)
            attempt = budget.reserve(self.run_id, estimated_input, 4096, phase=self.phase)
        # Reservation is committed on a separate connection before any provider request.
        model = DeepSeekModel()
        if model.name != 'deepseek-flash':
            raise ModelError('model_budget_unpriced_model')
        reply = model.complete(messages, tools, timeout)
        with psycopg.connect(self.dsn) as conn:
            BudgetLedger(conn).settle(attempt, reply.usage)
        return reply
