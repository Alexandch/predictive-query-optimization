-- Rollback removes only the new API/guard, preserving all metadata and audit.
DROP PROCEDURE IF EXISTS pqo.set_recommendation_status(bigint, text, text);
DROP TRIGGER IF EXISTS trg_index_recommendation_transition
    ON pqo.index_recommendation;
DROP FUNCTION IF EXISTS pqo.guard_recommendation_transition();
DROP FUNCTION IF EXISTS pqo.is_recommendation_transition(text, text);
