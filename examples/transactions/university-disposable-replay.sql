-- Only the disposable-database execution tab. Never run on production.
-- The original and the laboratory reference must be DIFFERENT databases.
-- Explicit IDs/dates; no identity/sequence/defaults, functions or DDL.
BEGIN;
SELECT enrollment_id, registered_at, status
FROM university.enrollments
WHERE student_id = 17001
ORDER BY registered_at DESC, enrollment_id;

UPDATE university.enrollments
SET status = 'completed'
WHERE student_id = 17001
RETURNING enrollment_id, status;

SELECT enrollment_id, status
FROM university.enrollments
WHERE student_id = 17001
ORDER BY enrollment_id;

INSERT INTO university.enrollments
    (enrollment_id, student_id, offering_id, status, registered_at)
VALUES
    (900000001, 17001, 1, 'active', CAST('2026-11-01 10:00:00' AS timestamp))
RETURNING enrollment_id, status;

SELECT enrollment_id, status
FROM university.enrollments
WHERE enrollment_id = 900000001;

DELETE FROM university.enrollments
WHERE enrollment_id = 900000001
RETURNING enrollment_id;
COMMIT;
