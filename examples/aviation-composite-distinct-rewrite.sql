SELECT
    p.ticket_no,
    p.flight_id,
    p.fare_conditions,
    (SELECT COUNT(DISTINCT t.ticket_no)
     FROM aviation.ticket_flights AS t
     WHERE t.flight_id = p.flight_id
       AND t.fare_conditions = p.fare_conditions) AS passengers_in_class,
    (SELECT SUM(prices.amount)
     FROM aviation.ticket_flights AS prices
     WHERE prices.fare_conditions = p.fare_conditions
       AND prices.flight_id = p.flight_id) AS class_revenue,
    (SELECT AVG(a.amount)
     FROM aviation.ticket_flights AS a
     WHERE a.flight_id = p.flight_id
       AND a.fare_conditions = p.fare_conditions) AS class_average_price
FROM aviation.ticket_flights AS p
WHERE p.flight_id = 1000
ORDER BY p.ticket_no;
