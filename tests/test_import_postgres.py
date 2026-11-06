"""Integration tests require an explicitly separate TEST_DATABASE_URL and audited archive."""

from dataclasses import replace
import json

import psycopg
import pytest

from enterpriseiq.dataset import SNAPSHOT_ID, digest
from enterpriseiq.store import account_links, import_bundle


def test_full_import_is_exact_and_idempotent(conn, bundle):
    result = import_bundle(conn, bundle)
    assert result["record_counts"] == {
        "crm.account": 42, "crm.opportunity": 8704, "crm.ticket": 32768, "crm.user": 289,
        "pm.issue": 8448, "pm.part": 40, "pm.user": 5, "pm.conversation": 23, "pm.comment": 26,
        "kb.article": 55, "files.transcript": 3, "files.document": 8,
    }
    again = import_bundle(conn, bundle)
    assert again == {**result, "imported": False}
    assert result["document_links"] == 66
    # Every record resolves to its exact original JSON position; every file retains byte hash.
    stored_files = conn.execute("SELECT path,body,sha256 FROM enterpriseiq.source_files").fetchall()
    decoded = {}
    for path, body, sha in stored_files:
        assert digest(body.encode("utf-8")) == sha
        if path.endswith(".json"):
            decoded[path] = json.loads(body)
    for path, pointer, data in conn.execute("SELECT source_path,json_pointer,data FROM enterpriseiq.records"):
        assert decoded[path][int(pointer[1:])] == data


def test_vantara_chain_distinguishes_candidate_and_text_reference(conn, bundle):
    import_bundle(conn, bundle)
    result = account_links(conn, "ACC-013")
    assert {"ticket_id": "TKT-119", "part_id": "PART-010", "issue_id": "ISS-017", "relation_type": "shared_component"} in result["component_candidates"]
    assert {"opportunity_id": "OPP-006", "issue_id": "ISS-017", "source_field": "next_step", "relation_type": "text_reference"} in result["opportunity_text_references"]
    assert result["transcripts"][0]["transcript_id"] == "TRN-001"
    assert result["transcripts"][0]["opportunity_id"] == "OPP-006"


def test_database_fk_failure_rolls_back_entire_snapshot(conn, bundle):
    broken = replace(bundle, relations=bundle.relations + [("crm", "ticket", "TKT-119", "pm", "part", "missing", "explicit_foreign_key", "components/99")])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        import_bundle(conn, broken)
    # Schema creation was in the same transaction, too. A clean import must still succeed.
    assert import_bundle(conn, bundle)["imported"] is True
    assert conn.execute("SELECT count(*) FROM enterpriseiq.snapshots WHERE snapshot_id=%s", (SNAPSHOT_ID,)).fetchone()[0] == 1
