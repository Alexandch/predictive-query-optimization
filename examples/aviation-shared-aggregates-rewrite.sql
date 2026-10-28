SELECT
    f.flight_id,
    f.departure_airport,
    (SELECT SUM(tf.amount)
     FROM aviation.ticket_flights AS tf
     WHERE tf.flight_id = f.flight_id) AS revenue,
    (SELECT COUNT(*)
     FROM aviation.ticket_flights AS tickets
     WHERE tickets.flight_id = f.flight_id) AS ticket_count,
    (SELECT AVG(prices.amount)
     FROM aviation.ticket_flights AS prices
     WHERE prices.flight_id = f.flight_id) AS average_price
FROM aviation.flights AS f
WHERE f.flight_id BETWEEN 1000 AND 1019
ORDER BY f.flight_id;
