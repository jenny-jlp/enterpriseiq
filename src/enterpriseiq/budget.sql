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
