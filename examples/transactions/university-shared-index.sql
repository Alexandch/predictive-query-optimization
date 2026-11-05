-- For the application's transaction tab only. Do not execute on production.
-- Inspect, obtain estimated plans, then collect index candidates. No DML replay.
BEGIN;

SELECT enrollment_id, registered_at, status
FROM university.enrollments
WHERE student_id = 17001
ORDER BY registered_at DESC;

SELECT enrollment_id, offering_id, registered_at
FROM university.enrollments
WHERE student_id = 17001 AND status = 'completed'
ORDER BY registered_at DESC;

UPDATE university.enrollments
SET status = 'completed'
WHERE student_id = 17001 AND offering_id = 350;

INSERT INTO university.enrollments
    (enrollment_id, student_id, offering_id, status, registered_at)
VALUES (900000001, 17001, 350, 'active', TIMESTAMP '2026-10-01 10:00:00');

DELETE FROM university.enrollments
WHERE student_id = 17001 AND status = 'withdrawn';

ROLLBACK;
