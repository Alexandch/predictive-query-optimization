-- For the transaction tab, on a separate test copy only. Never replay on production.
-- Mark enrollments(student_id, registered_at) and enrollments(offering_id, registered_at), no INCLUDE.
-- The application measures only the SELECT slice, not this transaction or DML costs.
BEGIN;

SELECT enrollment_id, registered_at, status
FROM university.enrollments
WHERE student_id = 17001
ORDER BY registered_at DESC;

SELECT enrollment_id, student_id, status, registered_at
FROM university.enrollments
WHERE offering_id = 350
ORDER BY registered_at DESC
LIMIT 200;

UPDATE university.enrollments
SET status = 'completed'
WHERE student_id = 17001 AND offering_id = 350;

ROLLBACK;
