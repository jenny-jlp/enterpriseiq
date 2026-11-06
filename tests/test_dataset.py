import hashlib
import json
from zipfile import ZipFile

import pytest

from enterpriseiq.dataset import MANIFEST, read_bundle


def make_archive(tmp_path, rows=None, contents=None):
    path = tmp_path / "fixture.zip"
    with ZipFile(path, "w") as archive:
        for name in MANIFEST:
            archive.writestr(name, json.dumps((rows or {}).get(name, [])))
        for name, body in (contents or {}).items():
            archive.writestr(name, body)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_rejects_unverified_archive(tmp_path):
    path, _ = make_archive(tmp_path)
    with pytest.raises(ValueError, match="SHA-256"):
        read_bundle(path)


def test_duplicate_entity_is_rejected(tmp_path):
    path, sha = make_archive(tmp_path, {"crm_json_data/accounts.json": [{"account_id": "A"}] * 2})
    with pytest.raises(ValueError, match="Duplicate entity"):
        read_bundle(path, expected_sha256=sha)


def test_dangling_foreign_key_is_rejected(tmp_path):
    path, sha = make_archive(tmp_path, {"crm_json_data/tickets.json": [{"ticket_id": "T", "account_id": "missing"}]})
    with pytest.raises(ValueError, match="Dangling reference"):
        read_bundle(path, expected_sha256=sha)


@pytest.mark.parametrize("filename", ["../secret.txt", "C:\\secret.txt", "/secret.txt"])
def test_document_path_traversal_is_rejected(tmp_path, filename):
    path, sha = make_archive(tmp_path, {"transcripts/transcripts.json": [{"transcript_id": "TRN-1", "content_file": filename}]})
    with pytest.raises(ValueError, match="Unsafe content_file"):
        read_bundle(path, expected_sha256=sha)


def test_missing_document_is_rejected(tmp_path):
    path, sha = make_archive(tmp_path, {"transcripts/transcripts.json": [{"transcript_id": "TRN-1", "content_file": "missing.txt"}]})
    with pytest.raises(KeyError):
        read_bundle(path, expected_sha256=sha)


def test_part_hierarchy_cycle_is_rejected(tmp_path):
    path, sha = make_archive(tmp_path, {"pm_json_data/maple_parts.json": [
        {"part_id": "P1", "parent_id": "P2"}, {"part_id": "P2", "parent_id": "P1"}]})
    with pytest.raises(ValueError, match="hierarchy cycle"):
        read_bundle(path, expected_sha256=sha)


def test_multi_component_arrays_and_system_namespaces_are_preserved(tmp_path):
    path, sha = make_archive(tmp_path, {
        "crm_json_data/users.json": [{"user_id": "same-id"}],
        "pm_json_data/users.json": [{"user_id": "same-id"}],
        "crm_json_data/tickets.json": [{"ticket_id": "T", "components": ["P1", "P2"]}],
        "pm_json_data/maple_parts.json": [{"part_id": "P1"}, {"part_id": "P2"}],
    })
    bundle = read_bundle(path, expected_sha256=sha)
    assert len(bundle.records) == 5
    assert {(r[5], r[7]) for r in bundle.relations} == {("P1", "components/0"), ("P2", "components/1")}
