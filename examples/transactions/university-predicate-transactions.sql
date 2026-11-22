-- Source: pqo_university_unseen; schema: university. Use a separate laboratory baseline.
-- SELECT 7: filtering JOIN to EXISTS. SELECT 8: positive IN to EXISTS.
BEGIN ISOLATION LEVEL REPEATABLE READ;

UPDATE university.enrollments AS e SET status = 'completed'
WHERE e.enrollment_id BETWEEN 17001 AND 17020
  AND EXISTS (SELECT 1 FROM university.students AS s WHERE s.student_id = e.student_id)
  AND e.offering_id IN (
      SELECT o.offering_id FROM university.offerings AS o
      WHERE o.term = '2026-autumn'
  )
RETURNING e.enrollment_id,e.status;

SAVEPOINT discarded;
DELETE FROM university.submissions AS sub
WHERE sub.enrollment_id BETWEEN 17001 AND 17020
  AND NOT EXISTS (
      SELECT 1 FROM university.enrollments AS e
      WHERE e.enrollment_id = sub.enrollment_id AND e.status <> 'withdrawn'
  )
RETURNING sub.submission_id,sub.enrollment_id;

SELECT COUNT(*) AS remaining_submissions FROM university.submissions
WHERE enrollment_id BETWEEN 17001 AND 17020;
ROLLBACK TO discarded;
RELEASE discarded;

SELECT DISTINCT s.student_id,s.full_name,s.campus_id
FROM university.students AS s
JOIN university.enrollments AS e ON e.student_id = s.student_id
WHERE s.student_id BETWEEN 17001 AND 17020 AND e.status <> 'withdrawn'
ORDER BY s.student_id;

SELECT s.student_id,s.full_name FROM university.students AS s
WHERE s.student_id BETWEEN 17001 AND 17020 AND s.student_id IN (
    SELECT e.student_id FROM university.enrollments AS e
    WHERE e.status = 'completed'
)
ORDER BY s.student_id;
COMMIT;
