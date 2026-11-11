CREATE SCHEMA IF NOT EXISTS investigation_budget;
CREATE TABLE IF NOT EXISTS investigation_budget.ledgers (
    name text PRIMARY KEY,
    policy jsonb NOT NULL,
    reservation_breach boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS investigation_budget.attempts (
    attempt_id uuid PRIMARY KEY,
    ledger text NOT NULL REFERENCES investigation_budget.ledgers,
    run_id uuid NOT NULL,
    phase text NOT NULL CHECK (phase IN ('smoke','freeze')),
    reserved_input bigint NOT NULL,
    reserved_output bigint NOT NULL,
    charged_tokens bigint NOT NULL,
    charged_cny numeric NOT NULL,
    prompt_tokens bigint,
    completion_tokens bigint,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS investigation_budget.runs (
    ledger text NOT NULL REFERENCES investigation_budget.ledgers,
    run_id uuid NOT NULL,
    phase text NOT NULL CHECK (phase IN ('smoke','freeze')),
    entrypoint text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ledger,run_id)
);
INSERT INTO investigation_budget.runs(ledger,run_id,phase,entrypoint,created_at)
SELECT ledger,run_id,min(phase),'legacy',min(created_at)
FROM investigation_budget.attempts GROUP BY ledger,run_id ON CONFLICT DO NOTHING;
ALTER TABLE investigation_budget.attempts ADD COLUMN IF NOT EXISTS request_key text;
ALTER TABLE investigation_budget.attempts ADD COLUMN IF NOT EXISTS request_digest text;
ALTER TABLE investigation_budget.attempts ADD COLUMN IF NOT EXISTS response jsonb;
ALTER TABLE investigation_budget.attempts ADD COLUMN IF NOT EXISTS outcome text NOT NULL DEFAULT 'unknown';
ALTER TABLE investigation_budget.attempts ADD COLUMN IF NOT EXISTS provider_seconds double precision;
ALTER TABLE investigation_budget.attempts ADD COLUMN IF NOT EXISTS error_code text;
CREATE UNIQUE INDEX IF NOT EXISTS budget_request_key ON investigation_budget.attempts(ledger,run_id,request_key);
CREATE TABLE IF NOT EXISTS investigation_budget.schema_version (singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), version int NOT NULL);
INSERT INTO investigation_budget.schema_version VALUES (true,4) ON CONFLICT(singleton) DO UPDATE SET version=excluded.version;
