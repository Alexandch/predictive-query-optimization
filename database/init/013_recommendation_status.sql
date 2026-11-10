-- Metadata only: this procedure never executes proposed_ddl or commits.
CREATE OR REPLACE FUNCTION pqo.is_recommendation_transition(
    p_old_status text,
    p_new_status text
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, pqo
AS $$
    SELECT COALESCE(
        (p_old_status = 'proposed' AND p_new_status IN ('accepted', 'rejected'))
        OR (p_old_status = 'accepted' AND p_new_status = 'applied')
        OR (p_old_status = 'applied' AND p_new_status = 'reverted'),
        false
    );
$$;

CREATE OR REPLACE FUNCTION pqo.guard_recommendation_transition()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pqo
AS $$
BEGIN
    IF NOT pqo.is_recommendation_transition(OLD.status, NEW.status) THEN
        RAISE EXCEPTION 'Invalid index recommendation transition: % -> %',
            OLD.status, NEW.status
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

-- Also prevent an invalid transition through a direct UPDATE. The existing
-- audit trigger remains the sole writer of recommendation_audit.
DROP TRIGGER IF EXISTS trg_index_recommendation_transition
    ON pqo.index_recommendation;
CREATE TRIGGER trg_index_recommendation_transition
BEFORE UPDATE OF status ON pqo.index_recommendation
FOR EACH ROW
EXECUTE FUNCTION pqo.guard_recommendation_transition();

CREATE OR REPLACE PROCEDURE pqo.set_recommendation_status(
    p_recommendation_id bigint,
    p_expected_status text,
    p_new_status text
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pqo
AS $$
DECLARE
    v_status text;
BEGIN
    IF p_recommendation_id IS NULL OR p_recommendation_id <= 0
        OR p_expected_status IS NULL
        OR p_expected_status NOT IN ('proposed', 'accepted', 'rejected', 'applied', 'reverted')
        OR p_new_status IS NULL
        OR p_new_status NOT IN ('proposed', 'accepted', 'rejected', 'applied', 'reverted') THEN
        RAISE EXCEPTION 'Recommendation id and statuses must be valid'
            USING ERRCODE = '22023';
    END IF;

    SELECT status INTO v_status
    FROM pqo.index_recommendation
    WHERE id = p_recommendation_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Index recommendation % not found', p_recommendation_id
            USING ERRCODE = 'P0002';
    END IF;
    IF v_status IS DISTINCT FROM p_expected_status THEN
        RAISE EXCEPTION 'Recommendation status changed: expected %, found %',
            p_expected_status, v_status
            USING ERRCODE = '55000';
    END IF;
    IF NOT pqo.is_recommendation_transition(v_status, p_new_status) THEN
        RAISE EXCEPTION 'Invalid index recommendation transition: % -> %',
            v_status, p_new_status
            USING ERRCODE = '23514';
    END IF;

    UPDATE pqo.index_recommendation
    SET status = p_new_status
    WHERE id = p_recommendation_id;
END;
$$;
