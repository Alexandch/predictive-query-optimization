-- Source: pqo_university_unseen; separate reference: pqo_university_index_lab.
-- Open in the transaction tab. Execute only on disposable laboratory copies.
-- Suggested index candidate: university.enrollments(student_id).
-- The second command also has an OR-to-IN rewrite candidate.
BEGIN;

UPDATE university.enrollments
SET status = 'completed'
WHERE enrollment_id = 17001
RETURNING enrollment_id, student_id, status;

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

SELECT
    e.enrollment_id,
    e.status,
    sub.assignment_no,
    sub.score
FROM university.enrollments AS e
LEFT JOIN university.submissions AS sub ON sub.enrollment_id = e.enrollment_id
WHERE e.enrollment_id = 17001
ORDER BY e.enrollment_id, sub.submission_id;

COMMIT;
