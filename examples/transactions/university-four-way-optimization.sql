-- Use the transaction tab and a prepared university laboratory.
-- Mark enrollments(student_id), prepare the inner ORDER BY removal (SELECT 10),
-- then open "Итоговая оптимизация". Execute only on disposable test copies.
BEGIN ISOLATION LEVEL REPEATABLE READ;

UPDATE university.enrollments
SET status = 'active'
WHERE enrollment_id = 17001
RETURNING enrollment_id, status;

SELECT COUNT(*) AS n, SUM(enrollment_id) AS total FROM university.enrollments WHERE student_id = 17001;
SELECT COUNT(*) AS n, SUM(enrollment_id) AS total FROM university.enrollments WHERE student_id = 17002;
SELECT COUNT(*) AS n, SUM(enrollment_id) AS total FROM university.enrollments WHERE student_id = 17003;
SELECT COUNT(*) AS n, SUM(enrollment_id) AS total FROM university.enrollments WHERE student_id = 17004;
SELECT COUNT(*) AS n, SUM(enrollment_id) AS total FROM university.enrollments WHERE student_id = 17005;
SELECT COUNT(*) AS n, SUM(enrollment_id) AS total FROM university.enrollments WHERE student_id = 17006;
SELECT COUNT(*) AS n, SUM(enrollment_id) AS total FROM university.enrollments WHERE student_id = 17007;
SELECT COUNT(*) AS n, SUM(enrollment_id) AS total FROM university.enrollments WHERE student_id = 17008;

SELECT q.status, COUNT(*) AS n, SUM(q.enrollment_id) AS total
FROM (
    SELECT enrollment_id, student_id, status, registered_at
    FROM university.enrollments
    ORDER BY registered_at DESC, enrollment_id
) AS q
GROUP BY q.status
ORDER BY q.status;

SELECT enrollment_id, status
FROM university.enrollments
WHERE enrollment_id = 17001
ORDER BY enrollment_id;

COMMIT;
