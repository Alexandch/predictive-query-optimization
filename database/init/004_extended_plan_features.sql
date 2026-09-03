ALTER TABLE pqo.execution_plan
    ADD COLUMN IF NOT EXISTS estimated_startup_cost double precision
        NOT NULL DEFAULT 0 CHECK (estimated_startup_cost >= 0),
    ADD COLUMN IF NOT EXISTS estimated_plan_width integer
        NOT NULL DEFAULT 0 CHECK (estimated_plan_width >= 0),
    ADD COLUMN IF NOT EXISTS estimated_rows_all_nodes double precision
        NOT NULL DEFAULT 0 CHECK (estimated_rows_all_nodes >= 0),
    ADD COLUMN IF NOT EXISTS max_plan_depth integer
        NOT NULL DEFAULT 1 CHECK (max_plan_depth > 0),
    ADD COLUMN IF NOT EXISTS relation_count integer
        NOT NULL DEFAULT 0 CHECK (relation_count >= 0),
    ADD COLUMN IF NOT EXISTS seq_scan_count integer
        NOT NULL DEFAULT 0 CHECK (seq_scan_count >= 0),
    ADD COLUMN IF NOT EXISTS index_scan_count integer
        NOT NULL DEFAULT 0 CHECK (index_scan_count >= 0),
    ADD COLUMN IF NOT EXISTS index_only_scan_count integer
        NOT NULL DEFAULT 0 CHECK (index_only_scan_count >= 0),
    ADD COLUMN IF NOT EXISTS bitmap_heap_scan_count integer
        NOT NULL DEFAULT 0 CHECK (bitmap_heap_scan_count >= 0),
    ADD COLUMN IF NOT EXISTS hash_join_count integer
        NOT NULL DEFAULT 0 CHECK (hash_join_count >= 0),
    ADD COLUMN IF NOT EXISTS merge_join_count integer
        NOT NULL DEFAULT 0 CHECK (merge_join_count >= 0),
    ADD COLUMN IF NOT EXISTS nested_loop_count integer
        NOT NULL DEFAULT 0 CHECK (nested_loop_count >= 0),
    ADD COLUMN IF NOT EXISTS sort_node_count integer
        NOT NULL DEFAULT 0 CHECK (sort_node_count >= 0),
    ADD COLUMN IF NOT EXISTS aggregate_node_count integer
        NOT NULL DEFAULT 0 CHECK (aggregate_node_count >= 0);

