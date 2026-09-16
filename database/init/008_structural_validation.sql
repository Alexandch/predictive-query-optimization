CREATE TABLE IF NOT EXISTS pqo.structural_validation (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recommendation_id bigint NOT NULL
        REFERENCES pqo.structural_recommendation (id) ON DELETE CASCADE,
    validation_method text NOT NULL,
    equivalent boolean NOT NULL,
    baseline_time_ms double precision NOT NULL CHECK (baseline_time_ms >= 0),
    candidate_time_ms double precision NOT NULL CHECK (candidate_time_ms >= 0),
    improvement_ratio double precision NOT NULL,
    artifact_creation_time_ms double precision
        CHECK (artifact_creation_time_ms IS NULL OR artifact_creation_time_ms >= 0),
    accepted boolean NOT NULL,
    rolled_back boolean NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS ix_structural_validation_recommendation
    ON pqo.structural_validation (recommendation_id, created_at DESC);
