SELECT c.department_id, o.term,
       COUNT(DISTINCT e.student_id) AS students,
       COUNT(*) AS submissions,
       ROUND(AVG(s.score), 2) AS average_score
FROM university.courses AS c
JOIN university.offerings AS o ON o.course_id = c.course_id
JOIN university.enrollments AS e ON e.offering_id = o.offering_id
JOIN university.submissions AS s ON s.enrollment_id = e.enrollment_id
WHERE e.status <> 'withdrawn'
GROUP BY c.department_id, o.term
ORDER BY average_score DESC, c.department_id, o.term
LIMIT 100;
