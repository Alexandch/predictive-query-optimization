CREATE TABLE IF NOT EXISTS pqo.sequential_analysis (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    query_run_id bigint NOT NULL
        REFERENCES pqo.query_run (id) ON DELETE CASCADE,
    model_name text NOT NULL DEFAULT 'sequential-dqn',
    model_version text NOT NULL DEFAULT 'unknown',
    baseline_time_ms double precision NOT NULL CHECK (baseline_time_ms >= 0),
    final_time_ms double precision NOT NULL CHECK (final_time_ms >= 0),
    measured_improvement_ratio double precision NOT NULL,
    storage_budget_bytes bigint NOT NULL CHECK (storage_budget_bytes >= 0),
    used_budget_bytes bigint NOT NULL CHECK (used_budget_bytes >= 0),
    candidate_count integer NOT NULL CHECK (candidate_count >= 0),
    decision_threshold double precision NOT NULL,
    minimum_baseline_time_ms double precision NOT NULL
        CHECK (minimum_baseline_time_ms >= 0),
    minimum_absolute_improvement_ms double precision NOT NULL
        CHECK (minimum_absolute_improvement_ms >= 0),
    terminal_reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS ix_sequential_analysis_query_run
    ON pqo.sequential_analysis (query_run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS pqo.sequential_analysis_step (
    sequential_analysis_id bigint NOT NULL
        REFERENCES pqo.sequential_analysis (id) ON DELETE CASCADE,
    step_number integer NOT NULL CHECK (step_number > 0),
    schema_name text NOT NULL,
    table_name text NOT NULL,
    key_columns text[] NOT NULL CHECK (cardinality(key_columns) > 0),
    include_columns text[] NOT NULL DEFAULT '{}',
    proposed_ddl text NOT NULL,
    predicted_q double precision NOT NULL,
    measured_reward double precision NOT NULL,
    before_time_ms double precision NOT NULL CHECK (before_time_ms >= 0),
    after_time_ms double precision NOT NULL CHECK (after_time_ms >= 0),
    index_size_bytes bigint NOT NULL CHECK (index_size_bytes >= 0),
    creation_time_ms double precision NOT NULL CHECK (creation_time_ms >= 0),
    used_by_postgresql boolean NOT NULL,
    PRIMARY KEY (sequential_analysis_id, step_number)
);

