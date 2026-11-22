-- Source: pqo_university_unseen; schema: university. Use a separate lab baseline.
-- SELECT 7 offers NOT MATERIALIZED. Candidate: enrollments(student_id).
BEGIN ISOLATION LEVEL REPEATABLE READ;

WITH selected_students AS (
    SELECT student_id,campus_id FROM university.students
    WHERE student_id BETWEEN 17001 AND 17020
)
UPDATE university.enrollments AS e SET status = 'completed'
FROM selected_students AS s
WHERE e.student_id = s.student_id
RETURNING e.enrollment_id,e.student_id,e.status,s.campus_id;

SAVEPOINT before_delete;

WITH selected_enrollments AS (
    SELECT enrollment_id FROM university.enrollments WHERE student_id = 17001
)
DELETE FROM university.submissions AS sub USING selected_enrollments AS e
WHERE sub.enrollment_id = e.enrollment_id
RETURNING sub.submission_id,sub.enrollment_id;

SELECT COUNT(*) AS remaining_submissions
FROM university.submissions AS sub
JOIN university.enrollments AS e ON e.enrollment_id = sub.enrollment_id
WHERE e.student_id = 17001;

ROLLBACK TO before_delete;
RELEASE before_delete;

WITH chosen AS MATERIALIZED (
    SELECT enrollment_id,student_id,status FROM university.enrollments
    WHERE student_id BETWEEN 17001 AND 18000
)
SELECT a.enrollment_id,a.student_id,a.status,b.status AS same_status
FROM chosen AS a JOIN chosen AS b ON a.enrollment_id = b.enrollment_id
WHERE a.student_id = 17001
ORDER BY a.enrollment_id;

SELECT COUNT(*) AS restored_submissions
FROM university.submissions AS sub
JOIN university.enrollments AS e ON e.enrollment_id = sub.enrollment_id
WHERE e.student_id = 17001;
COMMIT;
