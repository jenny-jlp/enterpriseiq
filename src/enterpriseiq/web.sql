CREATE SCHEMA IF NOT EXISTS investigation_web;
CREATE TABLE IF NOT EXISTS investigation_web.cases (
    case_id uuid PRIMARY KEY REFERENCES investigation_cases.cases,
    workspace text NOT NULL
);
CREATE TABLE IF NOT EXISTS investigation_web.runs (
    run_id uuid PRIMARY KEY REFERENCES investigation_cases.runs,
    initiator text NOT NULL
);
CREATE TABLE IF NOT EXISTS investigation_web.jobs (
    job_id uuid PRIMARY KEY,
    job_order bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    workspace text NOT NULL,
    actor text NOT NULL,
    request_hash text NOT NULL,
    case_id uuid NOT NULL REFERENCES investigation_web.cases,
    run_id uuid NOT NULL REFERENCES investigation_web.runs,
    command text NOT NULL CHECK (command IN ('start', 'resume')),
    input jsonb NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE investigation_web.jobs ADD COLUMN IF NOT EXISTS job_order bigint GENERATED ALWAYS AS IDENTITY;
CREATE UNIQUE INDEX IF NOT EXISTS one_active_web_job ON investigation_web.jobs(run_id)
    WHERE status IN ('queued', 'running');
CREATE TABLE IF NOT EXISTS investigation_web.sessions (
    token_hash text PRIMARY KEY,
    credential_hash text NOT NULL,
    identity_binding text,
    expires_at timestamptz NOT NULL DEFAULT now() + interval '8 hours'
);
ALTER TABLE investigation_web.sessions ADD COLUMN IF NOT EXISTS identity_binding text;
