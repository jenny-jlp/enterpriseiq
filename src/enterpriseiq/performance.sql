CREATE SCHEMA IF NOT EXISTS investigation_budget;
CREATE TABLE IF NOT EXISTS investigation_budget.executions (
    execution_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    entrypoint text NOT NULL,
    observation text NOT NULL CHECK (observation IN ('live','simulated','disabled')),
    submitted_at timestamptz NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ended_at timestamptz,
    queue_seconds double precision,
    execution_seconds double precision,
    end_to_end_seconds double precision,
    status text NOT NULL DEFAULT 'incomplete'
);
CREATE TABLE IF NOT EXISTS investigation_budget.timings (
    execution_id uuid NOT NULL REFERENCES investigation_budget.executions,
    event_number int NOT NULL,
    component text NOT NULL CHECK (component IN ('model_adapter','tool','validation')),
    seconds double precision NOT NULL,
    outcome text NOT NULL,
    PRIMARY KEY(execution_id,event_number)
);
