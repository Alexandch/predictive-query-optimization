-- Only the application's SELECT-rewrite tab, on a trusted laboratory server.
-- Use separate source/reference DBs; never execute this script on production.
-- The executor runs the whole transaction ONLY in fresh disposable copies.
BEGIN;

UPDATE university.enrollments
SET status = 'completed'
WHERE enrollment_id = 17001
RETURNING enrollment_id, status;

SELECT
    q.student_id,
    COUNT(*) AS enrollment_count,
    SUM(q.offering_id) AS offering_total
FROM (
    SELECT
        enrollment_id,
        student_id,
        offering_id,
        status,
        registered_at
    FROM university.enrollments
    ORDER BY registered_at DESC, enrollment_id
) AS q
WHERE q.status = 'completed'
GROUP BY q.student_id
HAVING COUNT(*) >= 2
ORDER BY q.student_id
LIMIT 500;

SELECT enrollment_id, status
FROM university.enrollments
WHERE enrollment_id = 17001
ORDER BY enrollment_id;

COMMIT;
