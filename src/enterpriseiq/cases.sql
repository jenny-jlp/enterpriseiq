CREATE SCHEMA IF NOT EXISTS investigation_cases;
CREATE TABLE IF NOT EXISTS investigation_cases.cases (
    case_id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS investigation_cases.runs (
    run_id uuid PRIMARY KEY,
    run_order bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    case_id uuid NOT NULL REFERENCES investigation_cases.cases,
    snapshot_id text NOT NULL,
    question text NOT NULL,
    state jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS investigation_cases.reports (
    report_id uuid PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES investigation_cases.cases,
    run_id uuid NOT NULL REFERENCES investigation_cases.runs,
    version integer NOT NULL,
    content_key text NOT NULL,
    validation text NOT NULL CHECK (validation IN ('passed', 'blocked')),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, version), UNIQUE (run_id, content_key)
);
CREATE TABLE IF NOT EXISTS investigation_cases.reviews (
    request_id uuid PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES investigation_cases.cases,
    report_id uuid NOT NULL REFERENCES investigation_cases.reports,
    action text NOT NULL CHECK (action IN ('accept', 'reject', 'modify')),
    actor text NOT NULL,
    reason text NOT NULL,
    opinion text NOT NULL,
    request_hash text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (report_id)
);
CREATE OR REPLACE FUNCTION investigation_cases.immutable_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Report and review history is append-only';
END;
$$;
CREATE OR REPLACE TRIGGER immutable_reports BEFORE UPDATE OR DELETE ON investigation_cases.reports
FOR EACH ROW EXECUTE FUNCTION investigation_cases.immutable_history();
CREATE OR REPLACE TRIGGER immutable_reviews BEFORE UPDATE OR DELETE ON investigation_cases.reviews
FOR EACH ROW EXECUTE FUNCTION investigation_cases.immutable_history();
