"""Transactional snapshot import and read-only relationship inspection."""

from importlib.resources import files
from psycopg.types.json import Jsonb

from enterpriseiq.dataset import Bundle, SNAPSHOT_ID, SOURCE_URL, UPSTREAM_COMMIT, digest


def initialize(conn):
    conn.execute(files("enterpriseiq").joinpath("schema.sql").read_text(encoding="utf-8"))


def import_bundle(conn, bundle: Bundle, snapshot_id: str = SNAPSHOT_ID):
    """All-or-nothing import. Identical snapshots are idempotent, never overwritten."""
    with conn.transaction():
        initialize(conn)
        existing = conn.execute(
            "SELECT archive_sha256 FROM enterpriseiq.snapshots WHERE snapshot_id = %s", (snapshot_id,)
        ).fetchone()
        if existing:
            if existing[0] != bundle.archive_sha256:
                raise ValueError("Snapshot ID already belongs to different archive bytes")
            return {"snapshot_id": snapshot_id, "imported": False, **summary(conn, snapshot_id)}
        conn.execute(
            "INSERT INTO enterpriseiq.snapshots(snapshot_id,archive_sha256,upstream_commit,source_url) VALUES (%s,%s,%s,%s)",
            (snapshot_id, bundle.archive_sha256, UPSTREAM_COMMIT, SOURCE_URL),
        )
        with conn.cursor() as cur:
            with cur.copy("COPY enterpriseiq.source_files(snapshot_id,path,sha256,body) FROM STDIN") as copy:
                for path, raw in bundle.files.items():
                    copy.write_row((snapshot_id, path, digest(raw), raw.decode("utf-8")))
            with cur.copy("COPY enterpriseiq.records FROM STDIN") as copy:
                for r in bundle.records:
                    copy.write_row((snapshot_id, r["system"], r["kind"], r["source_id"], r["source_path"],
                                    r["pointer"], r["sha256"], Jsonb(r["data"])))
            with cur.copy("COPY enterpriseiq.relations FROM STDIN") as copy:
                for relation in bundle.relations:
                    copy.write_row((snapshot_id, *relation))
            with cur.copy("COPY enterpriseiq.document_links FROM STDIN") as copy:
                for link in bundle.document_links:
                    copy.write_row((snapshot_id, *link))
        # New bulk-loaded tables otherwise have no planner statistics until autovacuum.
        conn.execute("ANALYZE enterpriseiq.records")
        conn.execute("ANALYZE enterpriseiq.relations")
        return {"snapshot_id": snapshot_id, "imported": True, **summary(conn, snapshot_id)}


def summary(conn, snapshot_id=SNAPSHOT_ID):
    counts = conn.execute(
        "SELECT source_system,entity_type,count(*) FROM enterpriseiq.records WHERE snapshot_id=%s GROUP BY 1,2 ORDER BY 1,2",
        (snapshot_id,),
    ).fetchall()
    totals = {}
    for table in ("source_files", "relations", "document_links"):
        # table names are an internal constant, never user input.
        totals[table] = conn.execute(f"SELECT count(*) FROM enterpriseiq.{table} WHERE snapshot_id=%s", (snapshot_id,)).fetchone()[0]
    return {"record_counts": {f"{system}.{kind}": count for system, kind, count in counts}, **totals}


def account_links(conn, account_id: str, snapshot_id=SNAPSHOT_ID):
    """Inspect open-ticket component paths and opportunity text references."""
    account = conn.execute(
        "SELECT data FROM enterpriseiq.records WHERE snapshot_id=%s AND source_system='crm' AND entity_type='account' AND source_id=%s",
        (snapshot_id, account_id),
    ).fetchone()
    if not account:
        raise ValueError(f"Unknown account: {account_id}")
    candidates = conn.execute("""
        SELECT t.source_id, c.part_id, i.source_id, c.relation_type
        FROM enterpriseiq.records t
        JOIN enterpriseiq.component_candidates c ON c.snapshot_id=t.snapshot_id AND c.ticket_id=t.source_id
        JOIN enterpriseiq.records i ON i.snapshot_id=c.snapshot_id AND i.source_system='pm'
            AND i.entity_type='issue' AND i.source_id=c.issue_id
        WHERE t.snapshot_id=%s AND t.source_system='crm' AND t.entity_type='ticket'
          AND t.data->>'account_id'=%s AND t.data->>'status'='open'
          AND i.data->>'status'='in_progress' AND i.data->>'priority' IN ('high','highest')
        ORDER BY 1,2,3
    """, (snapshot_id, account_id)).fetchall()
    text_refs = conn.execute("""
        SELECT r.from_id, r.to_id, r.source_field, r.relation_type
        FROM enterpriseiq.relations r JOIN enterpriseiq.records o
          ON (o.snapshot_id,o.source_system,o.entity_type,o.source_id)=
             (r.snapshot_id,r.from_system,r.from_type,r.from_id)
        WHERE r.snapshot_id=%s AND r.from_system='crm' AND r.from_type='opportunity'
          AND r.relation_type='text_reference' AND o.data->>'account_id'=%s
        ORDER BY 1,2,3
    """, (snapshot_id, account_id)).fetchall()
    transcripts = conn.execute("""
        SELECT r.source_id,r.data->>'opportunity_id',d.content_path
        FROM enterpriseiq.records r JOIN enterpriseiq.document_links d
          USING(snapshot_id,source_system,entity_type,source_id)
        WHERE r.snapshot_id=%s AND r.entity_type='transcript' AND r.data->>'account_id'=%s
        ORDER BY 1
    """, (snapshot_id, account_id)).fetchall()
    return {"snapshot_id": snapshot_id, "account_id": account_id, "account_name": account[0]["account_name"],
            "component_candidates": [dict(zip(("ticket_id", "part_id", "issue_id", "relation_type"), row)) for row in candidates],
            "opportunity_text_references": [dict(zip(("opportunity_id", "issue_id", "source_field", "relation_type"), row)) for row in text_refs],
            "transcripts": [dict(zip(("transcript_id", "opportunity_id", "content_path"), row)) for row in transcripts]}
