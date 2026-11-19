-- Source: pqo_university_unseen; reference: a separate laboratory baseline.
-- Run through the transaction workspace on disposable copies only.
-- SELECT 7 has an OR-to-IN rewrite; try enrollments(student_id).
BEGIN ISOLATION LEVEL REPEATABLE READ;

UPDATE university.enrollments AS e
SET status = 'completed'
FROM university.students AS s
WHERE e.student_id = s.student_id
  AND e.enrollment_id BETWEEN 17001 AND 17020
RETURNING e.enrollment_id, e.status, s.full_name;

SAVEPOINT before_delete;

DELETE FROM university.submissions AS sub
USING university.enrollments AS e
WHERE sub.enrollment_id = e.enrollment_id
  AND e.enrollment_id BETWEEN 17001 AND 17020
RETURNING sub.submission_id, sub.enrollment_id;

SELECT COUNT(*) AS remaining_submissions
FROM university.submissions
WHERE enrollment_id BETWEEN 17001 AND 17020;

ROLLBACK TO before_delete;
RELEASE before_delete;

SELECT enrollment_id, student_id, status
FROM university.enrollments
WHERE student_id = 17001 OR student_id = 17002
ORDER BY enrollment_id;

SELECT COUNT(*) AS restored_submissions
FROM university.submissions
WHERE enrollment_id BETWEEN 17001 AND 17020;

COMMIT;
