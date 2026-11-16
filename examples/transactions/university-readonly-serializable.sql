-- The same synthetic source/reference as university-isolation-savepoints.sql.
-- DEFERRABLE has an effect with SERIALIZABLE READ ONLY only.
START TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ ONLY, DEFERRABLE;

SELECT
    s.student_id,
    s.full_name,
    COUNT(DISTINCT e.enrollment_id) AS enrollment_count,
    AVG(sub.score) AS average_score
FROM university.students AS s
LEFT JOIN university.enrollments AS e ON e.student_id = s.student_id
LEFT JOIN university.submissions AS sub ON sub.enrollment_id = e.enrollment_id
WHERE s.student_id = 17001 OR s.student_id = 17002
GROUP BY s.student_id, s.full_name
ORDER BY s.student_id;

ROLLBACK;
