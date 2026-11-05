CREATE SCHEMA IF NOT EXISTS enterpriseiq;

CREATE TABLE IF NOT EXISTS enterpriseiq.snapshots (
    snapshot_id text PRIMARY KEY,
    archive_sha256 text NOT NULL UNIQUE,
    upstream_commit text NOT NULL,
    source_url text NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS enterpriseiq.source_files (
    snapshot_id text NOT NULL REFERENCES enterpriseiq.snapshots,
    path text NOT NULL,
    sha256 text NOT NULL,
    body text NOT NULL,
    PRIMARY KEY (snapshot_id, path)
);

CREATE TABLE IF NOT EXISTS enterpriseiq.records (
    snapshot_id text NOT NULL,
    source_system text NOT NULL,
    entity_type text NOT NULL,
    source_id text NOT NULL,
    source_path text NOT NULL,
    json_pointer text NOT NULL,
    record_sha256 text NOT NULL,
    data jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, source_system, entity_type, source_id),
    FOREIGN KEY (snapshot_id, source_path)
        REFERENCES enterpriseiq.source_files (snapshot_id, path)
);

CREATE TABLE IF NOT EXISTS enterpriseiq.relations (
    snapshot_id text NOT NULL,
    from_system text NOT NULL,
    from_type text NOT NULL,
    from_id text NOT NULL,
    to_system text NOT NULL,
    to_type text NOT NULL,
    to_id text NOT NULL,
    relation_type text NOT NULL CHECK (relation_type IN ('explicit_foreign_key', 'text_reference')),
    source_field text NOT NULL,
    PRIMARY KEY (snapshot_id, from_system, from_type, from_id,
                 to_system, to_type, to_id, relation_type, source_field),
    FOREIGN KEY (snapshot_id, from_system, from_type, from_id)
        REFERENCES enterpriseiq.records (snapshot_id, source_system, entity_type, source_id),
    FOREIGN KEY (snapshot_id, to_system, to_type, to_id)
        REFERENCES enterpriseiq.records (snapshot_id, source_system, entity_type, source_id)
);
CREATE INDEX IF NOT EXISTS relations_target ON enterpriseiq.relations
    (snapshot_id, to_system, to_type, to_id);

CREATE TABLE IF NOT EXISTS enterpriseiq.document_links (
    snapshot_id text NOT NULL,
    source_system text NOT NULL,
    entity_type text NOT NULL,
    source_id text NOT NULL,
    content_path text NOT NULL,
    PRIMARY KEY (snapshot_id, source_system, entity_type, source_id),
    FOREIGN KEY (snapshot_id, source_system, entity_type, source_id)
        REFERENCES enterpriseiq.records (snapshot_id, source_system, entity_type, source_id),
    FOREIGN KEY (snapshot_id, content_path)
        REFERENCES enterpriseiq.source_files (snapshot_id, path)
);

-- These rows express candidate impact only, never confirmed causality.
CREATE OR REPLACE VIEW enterpriseiq.component_candidates AS
SELECT t.snapshot_id, t.from_id AS ticket_id, i.from_id AS issue_id,
       t.to_id AS part_id, 'shared_component'::text AS relation_type
FROM enterpriseiq.relations t
JOIN enterpriseiq.relations i
  ON (t.snapshot_id, t.to_system, t.to_type, t.to_id) =
     (i.snapshot_id, i.to_system, i.to_type, i.to_id)
WHERE t.from_system = 'crm' AND t.from_type = 'ticket'
  AND i.from_system = 'pm' AND i.from_type = 'issue'
  AND t.to_type = 'part' AND t.relation_type = 'explicit_foreign_key'
  AND i.relation_type = 'explicit_foreign_key';
