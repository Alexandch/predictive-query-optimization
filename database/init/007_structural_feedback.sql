CREATE TABLE IF NOT EXISTS pqo.structural_recommendation (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    query_run_id bigint NOT NULL
        REFERENCES pqo.query_run (id) ON DELETE CASCADE,
    category varchar(32) NOT NULL
        CHECK (category IN ('aggregation', 'join', 'sort', 'materialized_view')),
    rule_id text NOT NULL,
    priority varchar(16) NOT NULL
        CHECK (priority IN ('high', 'medium', 'low')),
    title text NOT NULL,
    evidence text NOT NULL,
    proposed_action text NOT NULL,
    verification text NOT NULL,
    suggested_sql text,
    status varchar(16) NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'rejected')),
    decision_note text,
    decided_at timestamptz,
    baseline_time_ms double precision CHECK (baseline_time_ms >= 0),
    optimized_time_ms double precision CHECK (optimized_time_ms >= 0),
    measured_improvement_ratio double precision,
    measurement_outcome varchar(16)
        CHECK (measurement_outcome IN ('improved', 'unchanged', 'regressed')),
    measurement_note text,
    measured_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (query_run_id, rule_id),
    CHECK (
        (baseline_time_ms IS NULL AND optimized_time_ms IS NULL
            AND measured_improvement_ratio IS NULL
            AND measurement_outcome IS NULL AND measured_at IS NULL)
        OR
        (baseline_time_ms IS NOT NULL AND baseline_time_ms > 0
            AND optimized_time_ms IS NOT NULL
            AND measured_improvement_ratio IS NOT NULL
            AND measurement_outcome IS NOT NULL AND measured_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_structural_recommendation_status
    ON pqo.structural_recommendation (status, category, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_structural_recommendation_rule
    ON pqo.structural_recommendation (rule_id, measurement_outcome);
