"""Durable M3 gate. Frozen 2026-11-09 DeepSeek flash CNY rate estimates,
input 2/output 8 per million; not provider billing statements.
Source: https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
"""
from dataclasses import asdict
from decimal import Decimal
from importlib.resources import files
import json
import os
import time
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from enterpriseiq.model import Completion, DeepSeekModel, ModelError, budget_grant, request_digest


def budget_dsn():
    value = os.environ.get('BUDGET_DATABASE_URL')
    if not value:
        raise ModelError('model_budget_database_required')
    return value


class BudgetLedger:
    def __init__(self, conn, *, name='m3', max_calls=180, max_tokens=1_200_000, max_cny='30', max_runs=30,
                 max_smoke_runs=9, max_freeze_runs=21):
        self.conn, self.name = conn, name
        self.policy = dict(max_calls=max_calls, max_tokens=max_tokens, max_cny=str(max_cny), max_runs=max_runs,
                           max_smoke_runs=max_smoke_runs, max_freeze_runs=max_freeze_runs,
                           model='deepseek-flash', price_date='2026-11-09', input_per_million='2', output_per_million='8', currency='CNY')

    def initialize(self):
        with self.conn.transaction():
            if not self._schema_ready():
                self.conn.execute('SELECT pg_advisory_xact_lock(73931241)')
                if not self._schema_ready():
                    self.conn.execute(files('enterpriseiq').joinpath('budget.sql').read_text(encoding='utf-8'))
            self.conn.execute('INSERT INTO investigation_budget.ledgers(name,policy) VALUES (%s,%s) ON CONFLICT DO NOTHING', (self.name, Jsonb(self.policy)))
            self._lock(allow_blocked=True)

    def _schema_ready(self):
        if not self.conn.execute("SELECT to_regclass('investigation_budget.schema_version')").fetchone()[0]:
            return False
        row = self.conn.execute('SELECT version FROM investigation_budget.schema_version WHERE singleton').fetchone()
        if row and row[0] > 4:
            raise ModelError('model_budget_schema_incompatible')
        return bool(row and row[0] == 4)

    def _lock(self, *, allow_blocked=False):
        row = self.conn.execute('SELECT policy,reservation_breach FROM investigation_budget.ledgers WHERE name=%s FOR UPDATE', (self.name,)).fetchone()
        if not row or row[0] != self.policy or (row[1] and not allow_blocked):
            raise ModelError('model_budget_policy_or_reservation_blocked')

    def cost(self, prompt, completion):
        return (Decimal(prompt)*Decimal(self.policy['input_per_million']) + Decimal(completion)*Decimal(self.policy['output_per_million']))/Decimal(1_000_000)

    def summary(self):
        row = self.conn.execute('''SELECT count(*),coalesce(sum(charged_tokens),0),coalesce(sum(charged_cny),0),
            coalesce(sum(coalesce(prompt_tokens,0)+coalesce(completion_tokens,0)),0),count(*) FILTER (WHERE prompt_tokens IS NULL)
            FROM investigation_budget.attempts WHERE ledger=%s''', (self.name,)).fetchone()
        runs = self.conn.execute("SELECT count(*),count(*) FILTER (WHERE phase='smoke'),count(*) FILTER (WHERE phase='freeze') FROM investigation_budget.runs WHERE ledger=%s", (self.name,)).fetchone()
        return dict(attempts=row[0], runs=runs[0], smoke_runs=runs[1], freeze_runs=runs[2], charged_tokens=int(row[1]), charged_cny=str(row[2]), known_tokens=int(row[3]), unknown_attempts=row[4])

    def remaining(self):
        totals = self.summary()
        return {key:max(Decimal(0),Decimal(str(self.policy['max_'+key]))-Decimal(str(totals[total])))
                for key,total in [('runs','runs'),('smoke_runs','smoke_runs'),('freeze_runs','freeze_runs'),
                                  ('calls','attempts'),('tokens','charged_tokens'),('cny','charged_cny')]}

    def run_attempts(self, run_id):
        from psycopg.rows import dict_row
        with self.conn.cursor(row_factory=dict_row) as cur:
            return cur.execute('''SELECT attempt_id,request_key,phase,reserved_input,reserved_output,prompt_tokens,completion_tokens,
                charged_tokens,charged_cny,'conservative_rate_estimate' AS cost_kind,
                CASE WHEN outcome='not_sent' THEN 'known_not_sent' WHEN prompt_tokens IS NULL THEN 'reserved_unknown' ELSE 'provider_reported' END AS token_kind,
                coalesce(response->>'model',%s) AS model,provider_seconds,outcome,error_code,created_at
                FROM investigation_budget.attempts WHERE ledger=%s AND run_id=%s ORDER BY created_at''',
                (self.policy['model'],self.name,run_id)).fetchall()

    def register_run(self, run_id, *, entrypoint, phase='smoke'):
        if phase not in ('smoke','freeze'):
            raise ModelError('model_budget_invalid_phase')
        with self.conn.transaction():
            self._lock()
            run = self.conn.execute('SELECT phase FROM investigation_budget.runs WHERE ledger=%s AND run_id=%s', (self.name,run_id)).fetchone()
            if run:
                if run[0] != phase:
                    raise ModelError('model_budget_phase_conflict')
                return
            counts = self.conn.execute('SELECT count(*),count(*) FILTER (WHERE phase=%s) FROM investigation_budget.runs WHERE ledger=%s', (phase,self.name)).fetchone()
            if counts[0] >= self.policy['max_runs'] or counts[1] >= self.policy['max_'+phase+'_runs']:
                raise ModelError('model_budget_exhausted')
            self.conn.execute('INSERT INTO investigation_budget.runs(ledger,run_id,phase,entrypoint) VALUES (%s,%s,%s,%s)', (self.name,run_id,phase,entrypoint))

    def reserve(self, run_id, input_tokens, output_tokens, *, phase='smoke', request_key=None, digest=None):
        if type(input_tokens) is not int or type(output_tokens) is not int or input_tokens < 0 or output_tokens < 1:
            raise ModelError('model_budget_invalid_reservation')
        with self.conn.transaction():
            self._lock()
            self.register_run(run_id,entrypoint='adapter',phase=phase)
            if request_key is not None:
                existing = self.conn.execute('SELECT attempt_id,request_digest,response FROM investigation_budget.attempts WHERE ledger=%s AND run_id=%s AND request_key=%s', (self.name,run_id,request_key)).fetchone()
                if existing:
                    if existing[1] != digest:
                        raise ModelError('model_budget_content_conflict')
                    if existing[2] is not None:
                        return Completion(**existing[2])
                    raise ModelError('model_budget_outcome_unknown')
            totals = self.summary()
            price = self.cost(input_tokens,output_tokens)
            if (totals['attempts'] >= self.policy['max_calls']
                    or totals['charged_tokens']+input_tokens+output_tokens > self.policy['max_tokens']
                    or Decimal(totals['charged_cny'])+price > Decimal(self.policy['max_cny'])):
                raise ModelError('model_budget_exhausted')
            attempt = str(uuid4())
            self.conn.execute('''INSERT INTO investigation_budget.attempts(attempt_id,ledger,run_id,phase,reserved_input,reserved_output,charged_tokens,charged_cny,request_key,request_digest)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''', (attempt,self.name,run_id,phase,input_tokens,output_tokens,input_tokens+output_tokens,price,request_key,digest))
            return attempt

    def settle(self, attempt, usage):
        if not isinstance(usage,dict):
            return
        prompt, completion = usage.get('prompt_tokens'), usage.get('completion_tokens')
        if type(prompt) is not int or type(completion) is not int or prompt < 0 or completion < 0:
            return
        with self.conn.transaction():
            self._lock(allow_blocked=True)
            row = self.conn.execute('SELECT reserved_input,reserved_output,prompt_tokens,completion_tokens FROM investigation_budget.attempts WHERE attempt_id=%s AND ledger=%s FOR UPDATE', (attempt,self.name)).fetchone()
            if not row:
                raise ModelError('model_budget_attempt_missing')
            if row[2] is not None:
                if (prompt,completion) != (row[2],row[3]):
                    conservative_input = max(prompt,row[0],row[2])
                    conservative_output = max(completion,row[1],row[3])
                    self.conn.execute('''UPDATE investigation_budget.attempts SET charged_tokens=greatest(charged_tokens,%s),
                        charged_cny=greatest(charged_cny,%s),error_code='usage_conflict' WHERE attempt_id=%s''',
                        (conservative_input+conservative_output,self.cost(conservative_input,conservative_output),attempt))
                    self.conn.execute('UPDATE investigation_budget.ledgers SET reservation_breach=true WHERE name=%s', (self.name,))
                return
            self.conn.execute('UPDATE investigation_budget.attempts SET prompt_tokens=%s,completion_tokens=%s,charged_tokens=%s,charged_cny=%s WHERE attempt_id=%s', (prompt,completion,prompt+completion,self.cost(prompt,completion),attempt))
            if prompt > row[0] or completion > row[1]:
                self.conn.execute('UPDATE investigation_budget.ledgers SET reservation_breach=true WHERE name=%s', (self.name,))

    def record_outcome(self, attempt, *, outcome, seconds=None, error=None, reply=None):
        self.conn.execute('''UPDATE investigation_budget.attempts SET outcome=%s,provider_seconds=%s,error_code=%s,response=%s
            WHERE ledger=%s AND attempt_id=%s''', (outcome,seconds,error,Jsonb(asdict(reply)) if reply else None,self.name,attempt))


class BudgetedModel:
    def __init__(self, dsn, run_id, *, phase='smoke', number=None):
        self.dsn, self.run_id, self.phase, self.number = dsn,str(run_id),phase,number
        self.name = os.environ.get('DEEPSEEK_MODEL','deepseek-flash')
        self.prepared = False

    def prepare(self, *, entrypoint):
        with psycopg.connect(self.dsn) as conn:
            ledger = BudgetLedger(conn)
            ledger.initialize()
            ledger.register_run(self.run_id,entrypoint=entrypoint,phase=self.phase)
        self.prepared = True

    def for_attempt(self, number):
        if type(number) is not int or number < 1:
            raise ModelError('model_budget_invalid_request_key')
        bound = BudgetedModel(self.dsn,self.run_id,phase=self.phase,number=number)
        bound.prepared = self.prepared
        return bound

    def complete(self, messages, tools, timeout):
        if self.number is None:
            raise ModelError('model_budget_request_key_required')
        if not self.prepared:
            self.prepare(entrypoint='adapter')
        estimated_input = len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False).encode())+2048
        digest = request_digest(messages,tools,self.name)
        with psycopg.connect(self.dsn) as conn:
            attempt = BudgetLedger(conn).reserve(self.run_id,estimated_input,4096,phase=self.phase,
                                                 request_key=f'model-{self.number}',digest=digest)
        if isinstance(attempt,Completion):
            return attempt
        try:
            model = DeepSeekModel()
            if self.name != 'deepseek-flash' or model.name != self.name:
                raise ModelError('model_budget_unpriced_model')
        except Exception as exc:
            with psycopg.connect(self.dsn) as conn:
                ledger = BudgetLedger(conn)
                ledger.settle(attempt,dict(prompt_tokens=0,completion_tokens=0))
                ledger.record_outcome(attempt,outcome='not_sent',error=exc.code if isinstance(exc,ModelError) else 'configuration_error')
            raise
        start = time.perf_counter()
        try:
            with budget_grant(digest):
                reply = model.complete(messages,tools,timeout)
        except BaseException as exc:
            with psycopg.connect(self.dsn) as conn:
                BudgetLedger(conn).record_outcome(attempt,outcome='unknown',seconds=time.perf_counter()-start,
                    error=exc.code if isinstance(exc,ModelError) else 'provider_interrupted')
            raise
        seconds = time.perf_counter()-start
        with psycopg.connect(self.dsn) as conn:
            ledger = BudgetLedger(conn)
            ledger.settle(attempt,reply.usage)
            ledger.record_outcome(attempt,outcome='completed',seconds=seconds,reply=reply)
        return reply
