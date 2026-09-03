CREATE SCHEMA IF NOT EXISTS pqo;

CREATE TABLE IF NOT EXISTS pqo.query_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sql_text text NOT NULL,
    sql_hash char(64) NOT NULL,
    source varchar(32) NOT NULL DEFAULT 'application',
    status varchar(16) NOT NULL DEFAULT 'started'
        CHECK (status IN ('started', 'completed', 'failed')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    execution_time_ms double precision,
    error_message text,
    created_by text NOT NULL DEFAULT current_user,
    CHECK (execution_time_ms IS NULL OR execution_time_ms >= 0),
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE INDEX IF NOT EXISTS ix_query_run_sql_hash
    ON pqo.query_run (sql_hash);

CREATE INDEX IF NOT EXISTS ix_query_run_started_at
    ON pqo.query_run (started_at DESC);

CREATE TABLE IF NOT EXISTS pqo.execution_plan (
    query_run_id bigint PRIMARY KEY
        REFERENCES pqo.query_run (id) ON DELETE CASCADE,
    plan_json jsonb NOT NULL,
    root_node_type text NOT NULL,
    node_count integer NOT NULL CHECK (node_count > 0),
    estimated_total_cost double precision NOT NULL CHECK (estimated_total_cost >= 0),
    estimated_plan_rows double precision NOT NULL CHECK (estimated_plan_rows >= 0),
    actual_total_time_ms double precision CHECK (actual_total_time_ms >= 0),
    shared_hit_blocks bigint NOT NULL DEFAULT 0 CHECK (shared_hit_blocks >= 0),
    shared_read_blocks bigint NOT NULL DEFAULT 0 CHECK (shared_read_blocks >= 0),
    temp_read_blocks bigint NOT NULL DEFAULT 0 CHECK (temp_read_blocks >= 0),
    temp_written_blocks bigint NOT NULL DEFAULT 0 CHECK (temp_written_blocks >= 0)
);

CREATE TABLE IF NOT EXISTS pqo.model_prediction (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    query_run_id bigint NOT NULL
        REFERENCES pqo.query_run (id) ON DELETE CASCADE,
    model_name text NOT NULL,
    model_version text NOT NULL,
    predicted_time_ms double precision NOT NULL CHECK (predicted_time_ms >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (query_run_id, model_name, model_version)
);

CREATE TABLE IF NOT EXISTS pqo.index_recommendation (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    query_run_id bigint NOT NULL
        REFERENCES pqo.query_run (id) ON DELETE CASCADE,
    table_name text NOT NULL,
    index_columns text[] NOT NULL CHECK (cardinality(index_columns) > 0),
    include_columns text[] NOT NULL DEFAULT '{}',
    action varchar(16) NOT NULL
        CHECK (action IN ('create', 'drop', 'keep', 'none')),
    proposed_ddl text,
    estimated_improvement double precision,
    status varchar(16) NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'rejected', 'applied', 'reverted')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS pqo.recommendation_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recommendation_id bigint NOT NULL
        REFERENCES pqo.index_recommendation (id) ON DELETE CASCADE,
    old_status varchar(16),
    new_status varchar(16) NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    changed_by text NOT NULL DEFAULT current_user
);

CREATE OR REPLACE FUNCTION pqo.audit_recommendation_status()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := clock_timestamp();

    IF OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO pqo.recommendation_audit (
            recommendation_id,
            old_status,
            new_status
        ) VALUES (
            NEW.id,
            OLD.status,
            NEW.status
        );
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_index_recommendation_status
    ON pqo.index_recommendation;

CREATE TRIGGER trg_index_recommendation_status
BEFORE UPDATE OF status ON pqo.index_recommendation
FOR EACH ROW
EXECUTE FUNCTION pqo.audit_recommendation_status();

CREATE OR REPLACE FUNCTION pqo.register_query_run(
    p_sql_text text,
    p_sql_hash char(64),
    p_source varchar(32) DEFAULT 'application'
)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    v_query_run_id bigint;
BEGIN
    INSERT INTO pqo.query_run (sql_text, sql_hash, source)
    VALUES (p_sql_text, p_sql_hash, p_source)
    RETURNING id INTO v_query_run_id;

    RETURN v_query_run_id;
END;
$$;
