ALTER TABLE pqo.query_run
    ADD COLUMN IF NOT EXISTS template_id text NOT NULL DEFAULT 'external';

CREATE INDEX IF NOT EXISTS ix_query_run_template_id
    ON pqo.query_run (template_id);

DROP FUNCTION IF EXISTS pqo.register_query_run(text, character, character varying);

CREATE OR REPLACE FUNCTION pqo.register_query_run(
    p_sql_text text,
    p_sql_hash char(64),
    p_source varchar(32) DEFAULT 'application',
    p_template_id text DEFAULT 'external'
)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    v_query_run_id bigint;
BEGIN
    INSERT INTO pqo.query_run (sql_text, sql_hash, source, template_id)
    VALUES (p_sql_text, p_sql_hash, p_source, p_template_id)
    RETURNING id INTO v_query_run_id;

    RETURN v_query_run_id;
END;
$$;
