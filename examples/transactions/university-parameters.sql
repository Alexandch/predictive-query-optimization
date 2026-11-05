-- Transaction tab only. Parameters are separate for each statement, not shared globally.
-- Fill types/values explicitly in the Parameters SQL tab after local inspection:
-- statement 1: $1 integer 17001; $2 integer 50
-- statement 2: $1 integer 350;   $2 integer 200
-- statement 3: $1 text completed; $2 integer 17001; $3 integer 350
-- DML is planned only, never replayed by the SELECT/index trial.
BEGIN;

SELECT enrollment_id, registered_at, status
FROM university.enrollments
WHERE student_id = $1
ORDER BY registered_at DESC
LIMIT $2;

SELECT enrollment_id, student_id, registered_at, status
FROM university.enrollments
WHERE offering_id = $1
ORDER BY registered_at DESC
LIMIT $2;

UPDATE university.enrollments
SET status = $1
WHERE student_id = $2 AND offering_id = $3;

ROLLBACK;
