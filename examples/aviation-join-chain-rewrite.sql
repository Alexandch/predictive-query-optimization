SELECT DISTINCT
    f.flight_id,
    f.departure_airport
FROM aviation.flights AS f
JOIN aviation.ticket_flights AS tf
    ON tf.flight_id = f.flight_id
JOIN aviation.tickets AS t
    ON t.ticket_no = tf.ticket_no
WHERE tf.amount > 1000
  AND t.book_ref > '200000'
ORDER BY f.flight_id;
