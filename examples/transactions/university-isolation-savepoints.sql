-- Source: pqo_university_unseen; reference: pqo_university_index_lab.
-- Run through the transaction tab, on disposable laboratory copies only.
-- Change the isolation level to READ COMMITTED, READ UNCOMMITTED or SERIALIZABLE
-- to inspect another supported context. This single-session trial does not
-- reproduce an application's concurrent sessions.
BEGIN ISOLATION LEVEL REPEATABLE READ, READ WRITE;
SET TRANSACTION NOT DEFERRABLE;

UPDATE university.enrollments
SET status = 'active'
WHERE enrollment_id = 17001
RETURNING enrollment_id, student_id, status;

SAVEPOINT before_trial;
UPDATE university.enrollments
SET status = 'reviewed'
WHERE enrollment_id = 17001;
ROLLBACK TO SAVEPOINT before_trial;
RELEASE SAVEPOINT before_trial;

-- SELECT 7 must see 'active', not the rolled-back 'reviewed'.
-- An OR-to-IN rewrite is available for this SELECT.
SELECT
    e.enrollment_id,
    e.student_id,
    s.full_name,
    c.title,
    e.status,
    e.registered_at
FROM university.enrollments AS e
JOIN university.students AS s ON s.student_id = e.student_id
JOIN university.offerings AS o ON o.offering_id = e.offering_id
JOIN university.courses AS c ON c.course_id = o.course_id
WHERE e.student_id = 17001 OR e.student_id = 17002
ORDER BY e.enrollment_id;

SELECT e.enrollment_id, e.status, sub.assignment_no, sub.score
FROM university.enrollments AS e
LEFT JOIN university.submissions AS sub ON sub.enrollment_id = e.enrollment_id
WHERE e.enrollment_id = 17001
ORDER BY e.enrollment_id, sub.submission_id;

COMMIT;
