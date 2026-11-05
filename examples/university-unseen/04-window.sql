WITH scores AS (
    SELECT e.student_id, o.course_id, AVG(s.score) AS score
    FROM university.enrollments AS e
    JOIN university.offerings AS o ON o.offering_id = e.offering_id
    JOIN university.submissions AS s ON s.enrollment_id = e.enrollment_id
    WHERE e.status = 'completed'
    GROUP BY e.student_id, o.course_id
), ranked AS (
    SELECT student_id, course_id, score,
           DENSE_RANK() OVER (PARTITION BY course_id ORDER BY score DESC) AS place
    FROM scores
)
SELECT student_id, course_id, ROUND(score, 2) AS score, place
FROM ranked
WHERE place <= 10
ORDER BY course_id, place, student_id
LIMIT 500;
