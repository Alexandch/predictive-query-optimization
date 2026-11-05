SELECT s.student_id
FROM university.students AS s
WHERE s.student_id BETWEEN 1000 AND 1019
  AND (
      SELECT COUNT(*)
      FROM university.enrollments AS e
      WHERE e.student_id = s.student_id
  ) > 0
ORDER BY s.student_id;
