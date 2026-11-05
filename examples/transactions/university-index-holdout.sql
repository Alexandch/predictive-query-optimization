-- Control only: load AFTER freezing. New values, not for candidate selection.
SELECT enrollment_id, registered_at, status
FROM university.enrollments
WHERE student_id = 19007
ORDER BY registered_at DESC;

SELECT enrollment_id, student_id, status, registered_at
FROM university.enrollments
WHERE offering_id = 777
ORDER BY registered_at DESC
LIMIT 150;

SELECT enrollment_id, registered_at, status
FROM university.enrollments
WHERE student_id IN (19008, 19009)
ORDER BY registered_at DESC
LIMIT 80;
