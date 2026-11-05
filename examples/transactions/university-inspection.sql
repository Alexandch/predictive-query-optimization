-- Inspection only: NOT a script to execute against production.
BEGIN;
SELECT student_id, campus_id
FROM university.students
WHERE student_id = 17001;
UPDATE university.students
SET campus_id = 7
WHERE student_id = 17001;
SELECT enrollment_id, registered_at
FROM university.enrollments
WHERE student_id = 17001
ORDER BY registered_at DESC;
ROLLBACK;
