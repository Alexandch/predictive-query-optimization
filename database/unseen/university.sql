-- Independent synthetic domain. Install only in a NEW dedicated database.
CREATE SCHEMA university;
CREATE TABLE university.students (
    student_id bigint PRIMARY KEY, full_name text NOT NULL,
    campus_id integer NOT NULL, enrolled_at date NOT NULL
);
CREATE TABLE university.courses (
    course_id integer PRIMARY KEY, title text NOT NULL,
    department_id integer NOT NULL, credits integer NOT NULL
);
CREATE TABLE university.offerings (
    offering_id bigint PRIMARY KEY,
    course_id integer NOT NULL REFERENCES university.courses,
    term text NOT NULL, instructor_id integer NOT NULL
);
CREATE TABLE university.enrollments (
    enrollment_id bigint PRIMARY KEY,
    student_id bigint NOT NULL REFERENCES university.students,
    offering_id bigint NOT NULL REFERENCES university.offerings,
    status text NOT NULL, registered_at timestamp NOT NULL
);
CREATE TABLE university.submissions (
    submission_id bigint PRIMARY KEY,
    enrollment_id bigint NOT NULL REFERENCES university.enrollments,
    assignment_no integer NOT NULL, score numeric(5,2) NOT NULL,
    submitted_at timestamp NOT NULL
);
INSERT INTO university.students
SELECT i, 'Student ' || i, 1 + i % 12, DATE '2023-09-01' + (i % 730)::integer
FROM generate_series(1,40000) i;
INSERT INTO university.courses
SELECT i, 'Course ' || i, 1 + i % 20, 2 + i % 5 FROM generate_series(1,400) i;
INSERT INTO university.offerings
SELECT i, 1 + i % 400,
       (ARRAY['2024-autumn','2025-spring','2025-autumn','2026-spring','2026-autumn'])[1 + (i / 400)::integer % 5],
       1 + i % 1000 FROM generate_series(1,2000) i;
INSERT INTO university.enrollments
SELECT i, 1 + (i-1) % 40000,
       1 + ((i * 37 + ((i-1)/40000) * 347) % 2000),
       (ARRAY['active','completed','completed','completed','withdrawn'])[1 + i % 5],
       TIMESTAMP '2025-01-01' + ((i*13) % 365) * INTERVAL '1 day'
FROM generate_series(1::bigint,240000::bigint) i;
INSERT INTO university.submissions
SELECT (e-1)*3+a, e, a, ((e*17+a*23)%10001)/100.0,
       TIMESTAMP '2025-02-01' + ((e*7+a*19)%365)*INTERVAL '1 day'
FROM generate_series(1::bigint,240000::bigint) e CROSS JOIN generate_series(1,3) a;
ANALYZE university.students;
ANALYZE university.courses;
ANALYZE university.offerings;
ANALYZE university.enrollments;
ANALYZE university.submissions;
