CREATE SCHEMA IF NOT EXISTS investigation_maintenance;
CREATE TABLE IF NOT EXISTS investigation_maintenance.operations (
    operation_id uuid PRIMARY KEY,
    actor text NOT NULL,
    session_actor text NOT NULL,
    reason text NOT NULL,
    dry_run boolean NOT NULL,
    policy jsonb NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'started',
    result jsonb
);
CREATE TABLE IF NOT EXISTS investigation_maintenance.actions (
    operation_id uuid NOT NULL REFERENCES investigation_maintenance.operations,
    kind text NOT NULL,
    resource_id text NOT NULL,
    status text NOT NULL DEFAULT 'intent',
    PRIMARY KEY(operation_id,kind,resource_id)
);
CREATE TABLE IF NOT EXISTS investigation_maintenance.retired_checkpoints (
    run_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL REFERENCES investigation_maintenance.operations,
    checkpoint_id text NOT NULL,
    retired_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE OR REPLACE FUNCTION investigation_maintenance.keep_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR (TG_TABLE_NAME='operations' AND OLD.status<>'started')
        OR (TG_TABLE_NAME='actions' AND OLD.status<>'intent') THEN
        RAISE EXCEPTION 'Final maintenance audit is append-only';
    END IF;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE TRIGGER keep_operation_audit BEFORE UPDATE OR DELETE ON investigation_maintenance.operations
FOR EACH ROW EXECUTE FUNCTION investigation_maintenance.keep_audit();
CREATE OR REPLACE TRIGGER keep_action_audit BEFORE UPDATE OR DELETE ON investigation_maintenance.actions
FOR EACH ROW EXECUTE FUNCTION investigation_maintenance.keep_audit();
CREATE OR REPLACE TRIGGER keep_retirement BEFORE UPDATE OR DELETE ON investigation_maintenance.retired_checkpoints
FOR EACH ROW EXECUTE FUNCTION investigation_cases.immutable_history();
