SELECT q.flight_id
FROM (
    SELECT p.flight_id
    FROM aviation.flights AS p
    WHERE p.flight_id BETWEEN 1000 AND 1019
      AND (
          SELECT COUNT(*)
          FROM aviation.ticket_flights AS c
          WHERE c.flight_id = p.flight_id
      ) > 0
    ORDER BY p.flight_id DESC
) AS q
ORDER BY q.flight_id
LIMIT 20;
