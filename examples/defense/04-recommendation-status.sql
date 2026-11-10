-- Run manually with psql/pgAdmin on the laboratory service DB, not in the
-- application's restricted transaction executor. Metadata changes roll back;
-- identity sequences may still advance, as with any rolled-back INSERT.
BEGIN;
DO $$
DECLARE
    v_run bigint;
    v_recommendation bigint;
BEGIN
    INSERT INTO pqo.query_run(sql_text, sql_hash, source)
    VALUES ('SELECT 1', repeat('0', 64), 'status-demo') RETURNING id INTO v_run;
    INSERT INTO pqo.index_recommendation(query_run_id, table_name, index_columns, action)
    VALUES (v_run, 'demo.not_a_real_table', ARRAY['id'], 'create')
    RETURNING id INTO v_recommendation;

    CALL pqo.set_recommendation_status(v_recommendation, 'proposed', 'accepted');
    ASSERT (SELECT status = 'accepted' FROM pqo.index_recommendation WHERE id=v_recommendation);
    ASSERT (SELECT count(*) = 1 FROM pqo.recommendation_audit WHERE recommendation_id=v_recommendation);
    BEGIN
        CALL pqo.set_recommendation_status(v_recommendation, 'proposed', 'rejected');
        RAISE EXCEPTION 'Expected stale-status conflict was not raised';
    EXCEPTION WHEN object_not_in_prerequisite_state THEN
        RAISE NOTICE 'Stale expected status correctly rejected';
    END;
    BEGIN
        CALL pqo.set_recommendation_status(v_recommendation, 'accepted', 'proposed');
        RAISE EXCEPTION 'Expected invalid-transition failure was not raised';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'Invalid backward transition correctly rejected';
    END;
    ASSERT (SELECT count(*) = 1 FROM pqo.recommendation_audit WHERE recommendation_id=v_recommendation);
    RAISE NOTICE 'One accepted decision, one audit row; no index DDL executed';
END;
$$;
ROLLBACK;
