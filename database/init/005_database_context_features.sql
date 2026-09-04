ALTER TABLE pqo.execution_plan
    ADD COLUMN IF NOT EXISTS relation_row_estimate_sum double precision
        NOT NULL DEFAULT 0 CHECK (relation_row_estimate_sum >= 0),
    ADD COLUMN IF NOT EXISTS largest_relation_rows double precision
        NOT NULL DEFAULT 0 CHECK (largest_relation_rows >= 0),
    ADD COLUMN IF NOT EXISTS relation_size_bytes bigint
        NOT NULL DEFAULT 0 CHECK (relation_size_bytes >= 0),
    ADD COLUMN IF NOT EXISTS index_size_bytes bigint
        NOT NULL DEFAULT 0 CHECK (index_size_bytes >= 0),
    ADD COLUMN IF NOT EXISTS existing_index_count integer
        NOT NULL DEFAULT 0 CHECK (existing_index_count >= 0),
    ADD COLUMN IF NOT EXISTS estimated_selectivity double precision
        NOT NULL DEFAULT 0 CHECK (estimated_selectivity BETWEEN 0 AND 1);
