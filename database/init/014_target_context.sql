-- Metadata belongs only to the service database. No external database keys.
ALTER TABLE pqo.query_run
    ADD COLUMN IF NOT EXISTS target_context jsonb NOT NULL DEFAULT '{}'::jsonb;
