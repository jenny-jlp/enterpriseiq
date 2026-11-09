"""Explicit offline maintenance. Retain business history; prune verified duplicates.

No scheduled cleaner, external connector, recursive filesystem deletion or model calls.
"""
import argparse
from contextlib import ExitStack, nullcontext
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from enterpriseiq.agent import build_agent, run_lock, postgres_checkpointer
from enterpriseiq.cases import digest
from enterpriseiq.dataset import SNAPSHOT_ID
from enterpriseiq.jobs import WORKER_LOCK

POLICY = {'version':1,'trace_duplicate_days':30,'terminal_checkpoint_days':30,
          'unreferenced_snapshot_days':90,'case_reports_reviews':'retain_indefinitely',
          'referenced_snapshots':'retain_indefinitely','budget_and_audit':'retain_indefinitely'}
TERMINAL = ('completed','partial','failed')
MAINTENANCE_LOCK = 73931243


def authorize(conn):
    row=conn.execute("""SELECT rolsuper OR coalesce(pg_has_role(current_user,
        (SELECT oid FROM pg_roles WHERE rolname='enterpriseiq_maintenance'),'MEMBER'),false)
        FROM pg_roles WHERE rolname=current_user""").fetchone()
    if not row or not row[0]:
        raise PermissionError('Database maintenance privilege required')


def _file(path, root):
    # UUID leaves only. Refuse symlinks/junctions and paths escaping the fixed root.
    if path.is_symlink() or path.is_junction() or path.resolve().parent != root:
        raise ValueError('Unsafe trace path')
    return json.loads(path.read_text(encoding='utf-8'))


def _snapshot_refs(value):
    if isinstance(value,dict):
        result={value['snapshot_id']} if isinstance(value.get('snapshot_id'),str) else set()
        for item in value.values():
            result.update(_snapshot_refs(item))
        return result
    if isinstance(value,list):
        return set().union(*(_snapshot_refs(v) for v in value)) if value else set()
    return set()


def _history_pins(root):
    """Acceptance indexes are immutable evidence too; do not break their paths."""
    pins=set()
    def collect(value):
        if isinstance(value,dict):
            for item in value.values():
                collect(item)
        elif isinstance(value,list):
            for item in value:
                collect(item)
        elif isinstance(value,str):
            try:
                pins.add(str(UUID(value)))
            except ValueError:
                pass
    for directory in root.iterdir() if root.exists() else []:
        if directory.is_symlink() or directory.is_junction():
            raise ValueError('Unsafe history directory; cleanup stopped')
        if directory.is_dir():
            for name in ('manifest.json','results.json','reviewed_results.json'):
                path=directory/name
                if path.exists():
                    if path.is_symlink() or path.is_junction():
                        raise ValueError('Unsafe history index; cleanup stopped')
                    collect(json.loads(path.read_text(encoding='utf-8')))
    return pins


def _plan(conn, graph, root, now, locks):
    candidates={'traces':[],'checkpoints':[],'snapshots':[]}
    protected=[]
    archived={}
    pins={SNAPSHOT_ID}
    history_pins=_history_pins(root)
    unknown_trace_dependency=False
    for path in root.glob('*.json'):
        try:
            UUID(path.stem)
        except ValueError:
            continue
        try:
            pins.update(_snapshot_refs(_file(path,root)))
        except (ValueError,OSError):
            unknown_trace_dependency=True
    rows=conn.execute('''SELECT r.run_id,r.snapshot_id,r.state,max(p.created_at)
        FROM investigation_cases.runs r LEFT JOIN investigation_cases.reports p USING(run_id)
        GROUP BY r.run_id ORDER BY r.run_id''').fetchall()
    for run_id,snapshot,state,reported in rows:
        run_id=str(run_id)
        pins.add(snapshot)
        pins.update(_snapshot_refs(state))
        if run_id in history_pins:
            protected.append({'kind':'run','id':run_id,'reason':'retained_acceptance_evidence'})
            continue
        if not state or state.get('status') not in TERMINAL or not reported or reported>now-timedelta(days=30):
            protected.append({'kind':'run','id':run_id,'reason':'recent_resumable_or_unarchived'})
            continue
        try:
            locks.enter_context(run_lock(graph,run_id))
        except ValueError:
            protected.append({'kind':'run','id':run_id,'reason':'executing'})
            continue
        job=conn.execute('SELECT status,updated_at FROM investigation_web.jobs WHERE run_id=%s ORDER BY job_order DESC LIMIT 1',(run_id,)).fetchone()
        if job and (job[0] not in TERMINAL or job[1]>now-timedelta(days=30)):
            protected.append({'kind':'run','id':run_id,'reason':'recent_or_pending_job'})
            continue
        archived[run_id]=state
        path=root/(run_id+'.json')
        if path.exists():
            try:
                if datetime.fromtimestamp(path.stat().st_mtime,timezone.utc)<=now-timedelta(days=30) and digest(_file(path,root))==digest(state):
                    candidates['traces'].append(run_id)
                else:
                    protected.append({'kind':'trace','id':run_id,'reason':'recent_or_content_mismatch'})
            except (ValueError,OSError):
                protected.append({'kind':'trace','id':run_id,'reason':'unsafe_or_invalid_file'})
        saved=graph.get_state({'configurable':{'thread_id':run_id}})
        if saved.values:
            checkpoint_time=datetime.fromisoformat(saved.created_at)
            expected={k:v for k,v in state.items() if k not in ('next_nodes','checkpoint_id')}
            if not saved.next and checkpoint_time<=now-timedelta(days=30) and digest(saved.values)==digest(expected):
                candidates['checkpoints'].append(run_id)
            else:
                protected.append({'kind':'checkpoint','id':run_id,'reason':'recent_pending_or_not_projected'})
    for (payload,) in conn.execute('SELECT payload FROM investigation_cases.reports'):
        pins.update(_snapshot_refs(payload))
    orphan_checkpoint=conn.execute('''SELECT EXISTS(SELECT 1 FROM investigation_checkpoint.checkpoints c
        WHERE NOT EXISTS(SELECT 1 FROM investigation_cases.runs r WHERE r.run_id::text=c.thread_id))''').fetchone()[0]
    for snapshot,imported in conn.execute('SELECT snapshot_id,imported_at FROM enterpriseiq.snapshots ORDER BY snapshot_id'):
        if snapshot not in pins and not orphan_checkpoint and not unknown_trace_dependency and imported<=now-timedelta(days=90):
            candidates['snapshots'].append(snapshot)
        else:
            protected.append({'kind':'snapshot','id':snapshot,'reason':'pinned_recent_or_unknown_checkpoint_dependency'})
    return candidates,protected,archived


def maintain(dsn, *, outputs, reason, apply=False, now=None, connection=None):
    if not isinstance(reason,str) or not reason.strip() or len(reason)>500:
        raise ValueError('Maintenance reason required (1..500 characters)')
    now=now or datetime.now(timezone.utc)
    root=Path(outputs)
    if root.is_symlink() or root.is_junction():
        raise ValueError('Unsafe output directory')
    root=root.resolve()
    with (nullcontext(connection) if connection else psycopg.connect(dsn,autocommit=True)) as conn:
        if not conn.autocommit:
            raise ValueError('Dedicated autocommit maintenance connection required')
        authorize(conn)
        if not conn.execute("SELECT to_regclass('investigation_maintenance.retired_checkpoints')").fetchone()[0]:
            with conn.transaction():
                conn.execute('SELECT pg_advisory_xact_lock(73931244)')
                if not conn.execute("SELECT to_regclass('investigation_maintenance.retired_checkpoints')").fetchone()[0]:
                    conn.execute(files('enterpriseiq').joinpath('lifecycle.sql').read_text(encoding='utf-8'))
        operation=str(uuid4())
        conn.execute('''INSERT INTO investigation_maintenance.operations(operation_id,actor,session_actor,reason,dry_run,policy)
            VALUES (%s,current_user,session_user,%s,%s,%s)''',(operation,reason.strip(),not apply,Jsonb(POLICY)))
        acquired=[]
        try:
            for key in (MAINTENANCE_LOCK,WORKER_LOCK):
                if not conn.execute('SELECT pg_try_advisory_lock(%s)',(key,)).fetchone()[0]:
                    raise ValueError('Maintenance or worker is active; stop it before retrying')
                acquired.append(key)
            with postgres_checkpointer(dsn) as saver,ExitStack() as locks:
                graph=build_agent(None,None,saver)
                candidates,protected,archived=_plan(conn,graph,root,now,locks)
                result={'operation_id':operation,'dry_run':not apply,'as_of':now.isoformat(),
                        'policy':POLICY,'candidates':candidates,'protected':protected,'status':'completed'}
                if apply:
                    for kind,ids in candidates.items():
                        for resource in ids:
                            conn.execute('INSERT INTO investigation_maintenance.actions(operation_id,kind,resource_id) VALUES (%s,%s,%s)',(operation,kind,resource))
                            if kind=='traces':
                                path=root/(resource+'.json')
                                if digest(_file(path,root))!=digest(archived[resource]):
                                    raise ValueError('Trace changed after planning; cleanup stopped')
                                path.unlink()  # Exact verified leaf; never recursive.
                                conn.execute("UPDATE investigation_maintenance.actions SET status='completed' WHERE operation_id=%s AND kind=%s AND resource_id=%s",(operation,kind,resource))
                            else:
                                with conn.transaction():
                                    if kind=='checkpoints':
                                        saved=graph.get_state({'configurable':{'thread_id':resource}})
                                        conn.execute('INSERT INTO investigation_maintenance.retired_checkpoints(run_id,operation_id,checkpoint_id) VALUES (%s,%s,%s)',(resource,operation,saved.config['configurable']['checkpoint_id']))
                                        for table in ('checkpoint_writes','checkpoint_blobs','checkpoints'):
                                            conn.execute(f'DELETE FROM investigation_checkpoint.{table} WHERE thread_id=%s',(resource,))
                                    else:
                                        # Serialize against new run registration/import. No cascade.
                                        conn.execute('LOCK TABLE investigation_cases.runs,investigation_cases.reports,enterpriseiq.snapshots IN SHARE ROW EXCLUSIVE MODE')
                                        current_pins={row[0] for row in conn.execute('SELECT snapshot_id FROM investigation_cases.runs')}
                                        for (value,) in conn.execute('SELECT state FROM investigation_cases.runs UNION ALL SELECT payload FROM investigation_cases.reports'):
                                            current_pins.update(_snapshot_refs(value))
                                        orphan=conn.execute('''SELECT EXISTS(SELECT 1 FROM investigation_checkpoint.checkpoints c
                                            WHERE NOT EXISTS(SELECT 1 FROM investigation_cases.runs r WHERE r.run_id::text=c.thread_id))''').fetchone()[0]
                                        if resource in current_pins or orphan:
                                            raise ValueError('Snapshot became referenced; cleanup stopped')
                                        for table in ('document_links','relations','records','source_files','snapshots'):
                                            conn.execute(f'DELETE FROM enterpriseiq.{table} WHERE snapshot_id=%s',(resource,))
                                    conn.execute("UPDATE investigation_maintenance.actions SET status='completed' WHERE operation_id=%s AND kind=%s AND resource_id=%s",(operation,kind,resource))
                conn.execute("UPDATE investigation_maintenance.operations SET status='completed',finished_at=clock_timestamp(),result=%s WHERE operation_id=%s",(Jsonb(result),operation))
                return result
        except BaseException as exc:
            conn.execute("UPDATE investigation_maintenance.operations SET status='failed',finished_at=clock_timestamp(),result=%s WHERE operation_id=%s",(Jsonb({'error_type':type(exc).__name__,'detail':'No automatic retry; inspect per-resource intents'}),operation))
            raise
        finally:
            for key in reversed(acquired):
                conn.execute('SELECT pg_advisory_unlock(%s)',(key,))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Delete eligible duplicates; default dry-run still writes audit')
    parser.add_argument('--reason',required=True)
    args=parser.parse_args()
    dsn=os.environ.get('MAINTENANCE_DATABASE_URL')
    if not dsn:
        parser.exit(1,'Set MAINTENANCE_DATABASE_URL; no fallback to application credentials\n')
    try:
        print(json.dumps(maintain(dsn,outputs=Path.cwd()/'outputs',reason=args.reason,apply=args.apply),ensure_ascii=False,indent=2))
    except PermissionError:
        parser.exit(1,'Database maintenance privilege required\n')
    except psycopg.Error:
        parser.exit(1,'Maintenance database operation failed; inspect audit before retrying\n')
    except (ValueError,OSError) as exc:
        parser.exit(1,'Maintenance stopped: '+str(exc)+'\n')


if __name__=='__main__':
    main()
