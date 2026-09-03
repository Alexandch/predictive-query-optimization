CREATE TABLE IF NOT EXISTS pqo.query_features (
    query_run_id bigint PRIMARY KEY
        REFERENCES pqo.query_run (id) ON DELETE CASCADE,
    query_length integer NOT NULL CHECK (query_length > 0),
    join_count integer NOT NULL CHECK (join_count >= 0),
    where_condition_count integer NOT NULL CHECK (where_condition_count >= 0),
    subquery_count integer NOT NULL CHECK (subquery_count >= 0),
    has_group_by boolean NOT NULL,
    has_order_by boolean NOT NULL,
    has_distinct boolean NOT NULL,
    table_reference_count integer NOT NULL CHECK (table_reference_count >= 0),
    unique_table_count integer NOT NULL CHECK (unique_table_count >= 0),
    select_expression_count integer NOT NULL CHECK (select_expression_count >= 0),
    aggregate_function_count integer NOT NULL CHECK (aggregate_function_count >= 0),
    inner_join_count integer NOT NULL CHECK (inner_join_count >= 0),
    left_join_count integer NOT NULL CHECK (left_join_count >= 0),
    right_join_count integer NOT NULL CHECK (right_join_count >= 0),
    full_join_count integer NOT NULL CHECK (full_join_count >= 0),
    cross_join_count integer NOT NULL CHECK (cross_join_count >= 0),
    CHECK (
        join_count = inner_join_count + left_join_count + right_join_count
            + full_join_count + cross_join_count
    )
);

