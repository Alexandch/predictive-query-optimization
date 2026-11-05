SELECT enrollment_id, registered_at, status
FROM university.enrollments
WHERE student_id = 17001
ORDER BY registered_at DESC;
