"""Per-invocation observed wall times; no estimated latency or hidden human wait."""
from importlib.resources import files
import time
from uuid import uuid4

import psycopg


class ExecutionMetrics:
    def __init__(self, dsn, run_id, entrypoint, observation, submitted_at=None):
        self.dsn, self.id, self.number = dsn,str(uuid4()),0
        self.finished = False
        with psycopg.connect(dsn) as conn:
            started = conn.execute('SELECT clock_timestamp()').fetchone()[0]
            self.start = time.perf_counter()
            if not conn.execute("SELECT to_regclass('investigation_budget.timings')").fetchone()[0]:
                # Share the schema creation lock with budget migrations.
                conn.execute('SELECT pg_advisory_xact_lock(73931241)')
                conn.execute(files('enterpriseiq').joinpath('performance.sql').read_text(encoding='utf-8'))
            conn.execute('''INSERT INTO investigation_budget.executions(execution_id,run_id,entrypoint,observation,submitted_at,started_at,queue_seconds)
                VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                (self.id,run_id,entrypoint,observation,submitted_at or started,started,
                 max(0,(started-submitted_at).total_seconds()) if submitted_at and entrypoint=='web_worker' else 0))

    def call(self, component, function, *args, **kwargs):
        start, outcome = time.perf_counter(),'error'
        try:
            result = function(*args,**kwargs)
            outcome = 'returned'
            return result
        finally:
            seconds = time.perf_counter()-start
            self.number += 1
            with psycopg.connect(self.dsn) as conn:
                conn.execute('INSERT INTO investigation_budget.timings VALUES (%s,%s,%s,%s,%s)',
                             (self.id,self.number,component,seconds,outcome))

    def finish(self, status):
        if self.finished:
            return
        with psycopg.connect(self.dsn) as conn:
            conn.execute('''UPDATE investigation_budget.executions SET ended_at=clock_timestamp(),status=%s,
                execution_seconds=%s,end_to_end_seconds=extract(epoch FROM clock_timestamp()-submitted_at)
                WHERE execution_id=%s''', (status,time.perf_counter()-self.start,self.id))
        self.finished = True

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is not None:
            self.finish('interrupted')


class Timed:
    def __init__(self, target, metrics, component):
        object.__setattr__(self,'target',target)
        object.__setattr__(self,'metrics',metrics)
        object.__setattr__(self,'component',component)

    def __getattr__(self, name):
        value = getattr(self.target,name)
        if name == 'for_attempt':
            return lambda number: Timed(value(number),self.metrics,self.component)
        if name in ('complete','execute','verify'):
            return lambda *args,**kwargs: self.metrics.call(self.component,value,*args,**kwargs)
        return value

    def __setattr__(self,name,value):
        setattr(self.target,name,value)


def execution_history(conn, run_id):
    from psycopg.rows import dict_row
    if not conn.execute("SELECT to_regclass('investigation_budget.timings')").fetchone()[0]:
        return []  # Historical runs have no measurements; never synthesize zeros.
    with conn.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute('''SELECT e.*,coalesce((SELECT jsonb_agg(jsonb_build_object('component',t.component,
            'seconds',t.seconds,'outcome',t.outcome) ORDER BY event_number) FROM investigation_budget.timings t
            WHERE t.execution_id=e.execution_id),'[]') AS timings FROM investigation_budget.executions e
            WHERE run_id=%s ORDER BY started_at''', (run_id,)).fetchall()
    return rows
