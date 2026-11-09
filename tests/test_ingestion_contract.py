"""Simulated source changes, isolated DB, immutable snapshot contract. No connector."""
from copy import deepcopy
import json
from zipfile import ZipFile

import psycopg
import pytest

from enterpriseiq.dataset import SNAPSHOT_ID, digest, read_bundle
from enterpriseiq.store import import_bundle
from enterpriseiq.tools import BusinessTools
from test_workbench_worker import database


def changed_archive(bundle,tmp_path,change):
    files=dict(bundle.files)
    def rows(path):
        return json.loads(files[path])
    def save(path,value):
        files[path]=json.dumps(value,ensure_ascii=False).encode()
    accounts=rows('crm_json_data/accounts.json')
    articles=rows('maple_kb/articles.json')
    tickets=rows('crm_json_data/tickets.json')
    metadata={}
    if change=='combined':
        new=deepcopy(accounts[0]); new['account_id']='ACC-M3-FIXTURE'
        accounts.append(new)
        target=next(a for a in accounts if a['account_id']=='ACC-013')
        target['account_name']='M3 changed account fixture'
        metadata['deleted_article']=articles.pop()['article_id']
        parts=rows('pm_json_data/maple_parts.json')
        part=next(p['part_id'] for p in parts if p['part_id'] not in tickets[0].get('components',[]))
        tickets[0]['components']=[part]
        tickets[0]['account_id']='ACC-M3-FIXTURE'
        metadata.update(ticket=tickets[0]['ticket_id'],part=part)
    elif change=='dangling':
        tickets[0]['account_id']='ACC-DOES-NOT-EXIST'
    elif change=='unsafe_source':
        articles[0]['content_file']='../outside.md'
    elif change=='duplicate':
        accounts.append(deepcopy(accounts[0]))
    elif change=='missing_source':
        del files[bundle.document_links[0][-1]]
    save('crm_json_data/accounts.json',accounts)
    save('maple_kb/articles.json',articles)
    save('crm_json_data/tickets.json',tickets)
    path=tmp_path/'source-change.zip'
    with ZipFile(path,'w') as archive:
        for name,body in files.items():
            archive.writestr(name,body)
    return path,metadata


def test_add_update_delete_and_relationship_change_create_new_snapshot_without_rewriting_old(database,bundle,tmp_path):
    path,meta=changed_archive(bundle,tmp_path,'combined')
    changed=read_bundle(path,expected_sha256=digest(path.read_bytes()))
    snapshot='fixture-'+changed.archive_sha256[:16]
    with psycopg.connect(database) as conn:
        original=conn.execute('SELECT snapshot_id,source_system,entity_type,source_id,record_sha256 FROM enterpriseiq.records ORDER BY 1,2,3,4').fetchall()
        assert import_bundle(conn,changed,snapshot)['imported']
        assert not import_bundle(conn,changed,snapshot)['imported']
        assert conn.execute('SELECT snapshot_id,source_system,entity_type,source_id,record_sha256 FROM enterpriseiq.records WHERE snapshot_id=%s ORDER BY 1,2,3,4',(SNAPSHOT_ID,)).fetchall()==original
        records={(r['kind'],r['source_id']):r for r in changed.records}
        assert ('account','ACC-M3-FIXTURE') in records
        assert ('article',meta['deleted_article']) not in records
        assert records['account','ACC-013']['data']['account_name']=='M3 changed account fixture'
        links=conn.execute('SELECT to_id FROM enterpriseiq.relations WHERE snapshot_id=%s AND from_id=%s AND to_type=%s',(snapshot,meta['ticket'],'part')).fetchall()
        assert links==[(meta['part'],)]
        old=BusinessTools(conn).execute('get_record',{'entity_type':'account','source_id':'ACC-013'})
        new=BusinessTools(conn,snapshot_id=snapshot).execute('get_record',{'entity_type':'account','source_id':'ACC-013'})
        assert old['item']['data']['account_name']=='Vantara'
        assert new['item']['data']['account_name']=='M3 changed account fixture'
        assert old['item']['evidence']['evidence_id']!=new['item']['evidence']['evidence_id']
        # A changed archive must never be aliased to an existing snapshot ID.
        with pytest.raises(ValueError,match='different archive'):
            import_bundle(conn,changed,SNAPSHOT_ID)


@pytest.mark.parametrize('change,error',[
    ('dangling',ValueError),('unsafe_source',ValueError),('duplicate',ValueError),('missing_source',KeyError),
])
def test_invalid_source_change_is_rejected_before_any_snapshot_write(database,bundle,tmp_path,change,error):
    path,_=changed_archive(bundle,tmp_path,change)
    with pytest.raises(error):
        read_bundle(path,expected_sha256=digest(path.read_bytes()))
    with psycopg.connect(database) as conn:
        assert conn.execute('SELECT count(*) FROM enterpriseiq.snapshots').fetchone()[0]==1


def test_archive_change_without_explicit_new_hash_is_rejected(bundle,tmp_path):
    path,_=changed_archive(bundle,tmp_path,'combined')
    with pytest.raises(ValueError,match='SHA-256'):
        read_bundle(path)
