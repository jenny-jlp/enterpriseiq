"""Read only the audited business-data archive; never ingest tasks or criteria."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from zipfile import ZipFile

UPSTREAM_COMMIT = "6220f818b0cf678471168d2c626a12fcce24ccf6"
ARCHIVE_SHA256 = "24d6d134067ffc763c953fab8ec28022c98bb4da11aa0e4456798d7f9bb656bc"
SOURCE_URL = f"https://github.com/devrev/enterprise-bench/raw/{UPSTREAM_COMMIT}/artifacts/data.zip"
SNAPSHOT_ID = f"enterprise-bench-{UPSTREAM_COMMIT[:12]}-{ARCHIVE_SHA256[:12]}"

# archive path -> source system, entity type, primary key
MANIFEST = {
    "crm_json_data/accounts.json": ("crm", "account", "account_id"),
    "crm_json_data/opportunities.json": ("crm", "opportunity", "opportunity_id"),
    "crm_json_data/tickets.json": ("crm", "ticket", "ticket_id"),
    "crm_json_data/users.json": ("crm", "user", "user_id"),
    "pm_json_data/issues.json": ("pm", "issue", "issue_id"),
    "pm_json_data/maple_parts.json": ("pm", "part", "part_id"),
    "pm_json_data/users.json": ("pm", "user", "user_id"),
    "pm_json_data/conversations.json": ("pm", "conversation", "conversation_id"),
    "pm_json_data/comments.json": ("pm", "comment", "comment_id"),
    "maple_kb/articles.json": ("kb", "article", "article_id"),
    "transcripts/transcripts.json": ("files", "transcript", "transcript_id"),
    "internal_docs/msa_and_compliance.json": ("files", "document", "document_id"),
}

FOREIGN_KEYS = {
    ("crm", "account"): {"owner_id": ("crm", "user")},
    ("crm", "opportunity"): {"account_id": ("crm", "account"), "owner_id": ("crm", "user")},
    ("crm", "ticket"): {"account_id": ("crm", "account"), "conversation_id": ("pm", "conversation")},
    ("crm", "user"): {"account_id": ("crm", "account"), "manager_id": ("crm", "user")},
    ("pm", "issue"): {"assignee_id": ("pm", "user"), "conversation_id": ("pm", "conversation")},
    ("pm", "part"): {"parent_id": ("pm", "part")},
    ("pm", "comment"): {"conversation_id": ("pm", "conversation")},
    ("files", "transcript"): {"account_id": ("crm", "account"), "opportunity_id": ("crm", "opportunity")},
}


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass
class Bundle:
    archive_sha256: str
    files: dict[str, bytes]
    records: list[dict]
    relations: list[tuple]
    document_links: list[tuple]


def read_bundle(path: Path, *, expected_sha256: str = ARCHIVE_SHA256) -> Bundle:
    """Verify bytes before parsing; reject missing content and dangling relations."""
    archive_hash = digest(path.read_bytes())
    if archive_hash != expected_sha256:
        raise ValueError("Archive SHA-256 does not match the expected snapshot")
    files, records, links = {}, [], []
    keys = set()
    with ZipFile(path) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("Duplicate archive member")
        for source_path, (system, kind, id_field) in MANIFEST.items():
            raw = archive.read(source_path)
            files[source_path] = raw
            rows = json.loads(raw)
            if not isinstance(rows, list):
                raise ValueError(f"Expected array in {source_path}")
            for index, row in enumerate(rows):
                source_id = row[id_field]
                if not isinstance(source_id, str) or not source_id:
                    raise ValueError(f"Invalid ID in {source_path}/{index}")
                key = (system, kind, source_id)
                if key in keys:
                    raise ValueError(f"Duplicate entity: {key}")
                keys.add(key)
                canonical = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                records.append(dict(system=system, kind=kind, source_id=source_id,
                                    source_path=source_path, pointer=f"/{index}",
                                    sha256=digest(canonical.encode()), data=row))
                if "content_file" in row:
                    filename = row["content_file"]
                    # Upstream manifests use a basename relative to their own folder.
                    if not isinstance(filename, str) or not filename or PurePosixPath(filename).name != filename or "\\" in filename:
                        raise ValueError("Unsafe content_file path")
                    content_path = str(PurePosixPath(source_path).parent / filename)
                    content = archive.read(content_path)  # Missing content is a failed import.
                    content.decode("utf-8")
                    files[content_path] = content
                    links.append((system, kind, source_id, content_path))

    relations = set()
    for record in records:
        system, kind, source_id, row = (record[k] for k in ("system", "kind", "source_id", "data"))

        def add(target_system, target_type, target_id, field, relation_type="explicit_foreign_key"):
            if (target_system, target_type, target_id) not in keys:
                raise ValueError(f"Dangling reference: {source_id}.{field} -> {target_id}")
            relations.add((system, kind, source_id, target_system, target_type, target_id, relation_type, field))

        for field, (target_system, target_type) in FOREIGN_KEYS.get((system, kind), {}).items():
            if row.get(field) is not None:
                add(target_system, target_type, row[field], field)
        if kind in ("ticket", "issue"):
            components = row.get("components", [])
            if not isinstance(components, list):
                raise ValueError(f"Invalid components on {source_id}")
            for index, part in enumerate(components):
                add("pm", "part", part, f"components/{index}")
        if kind == "conversation":
            parent_type = row["parent_type"]
            if parent_type not in ("ticket", "issue"):
                raise ValueError(f"Unknown conversation parent: {parent_type}")
            add("crm" if parent_type == "ticket" else "pm", parent_type, row["parent_id"], "parent_id")
        # A literal issue identifier in prose is a text reference, never a causal assertion.
        for field in ("description", "next_step", "body"):
            text = row.get(field, "")
            if isinstance(text, str):
                for issue_id in re.findall(r"\bISS-\d+\b", text):
                    if ("pm", "issue", issue_id) in keys:
                        add("pm", "issue", issue_id, field, "text_reference")

    # A self-referential FK alone cannot detect cycles in the part hierarchy.
    parents = {r["source_id"]: r["data"].get("parent_id") for r in records if r["kind"] == "part"}
    for start in parents:
        seen, current = set(), start
        while current is not None:
            if current in seen:
                raise ValueError(f"Part hierarchy cycle at {current}")
            seen.add(current)
            current = parents[current]
    return Bundle(archive_hash, files, records, sorted(relations), links)
