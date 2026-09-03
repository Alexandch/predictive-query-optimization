INSERT INTO aviation.airports (airport_code, airport_name, city, timezone)
VALUES
    ('MSQ', 'Minsk National Airport', 'Minsk', 'Europe/Minsk'),
    ('BQT', 'Brest Airport', 'Brest', 'Europe/Minsk'),
    ('GME', 'Gomel Airport', 'Gomel', 'Europe/Minsk'),
    ('VTB', 'Vitebsk Airport', 'Vitebsk', 'Europe/Minsk'),
    ('GNA', 'Grodno Airport', 'Grodno', 'Europe/Minsk'),
    ('SVO', 'Sheremetyevo', 'Moscow', 'Europe/Moscow'),
    ('LED', 'Pulkovo', 'Saint Petersburg', 'Europe/Moscow'),
    ('WAW', 'Warsaw Chopin Airport', 'Warsaw', 'Europe/Warsaw'),
    ('TBS', 'Tbilisi International Airport', 'Tbilisi', 'Asia/Tbilisi'),
    ('IST', 'Istanbul Airport', 'Istanbul', 'Europe/Istanbul')
ON CONFLICT DO NOTHING;

INSERT INTO aviation.aircrafts (aircraft_code, model, range_km)
VALUES
    ('SU9', 'Sukhoi Superjet 100', 3050),
    ('320', 'Airbus A320', 6100),
    ('321', 'Airbus A321neo', 7400),
    ('738', 'Boeing 737-800', 5436),
    ('E95', 'Embraer E195-E2', 4815)
ON CONFLICT DO NOTHING;

INSERT INTO aviation.seats (aircraft_code, seat_no, fare_conditions)
SELECT
    aircraft_code,
    concat((seat_index - 1) / 6 + 1, chr(65 + (seat_index - 1) % 6)),
    CASE
        WHEN seat_index <= 12 THEN 'Business'
        WHEN seat_index <= 36 THEN 'Comfort'
        ELSE 'Economy'
    END
FROM aviation.aircrafts
CROSS JOIN generate_series(1, 180) AS seat_index
ON CONFLICT DO NOTHING;

INSERT INTO aviation.flights (
    flight_no,
    scheduled_departure,
    scheduled_arrival,
    departure_airport,
    arrival_airport,
    status,
    aircraft_code
)
SELECT
    concat('B2', lpad((1000 + series_id % 8999)::text, 4, '0')),
    timestamptz '2025-01-01 00:00:00+03'
        + (series_id % 365) * interval '1 day'
        + (series_id % 24) * interval '1 hour',
    timestamptz '2025-01-01 00:00:00+03'
        + (series_id % 365) * interval '1 day'
        + (series_id % 24) * interval '1 hour'
        + (60 + series_id % 300) * interval '1 minute',
    airports[(series_id % 10) + 1],
    airports[((series_id + 3) % 10) + 1],
    statuses[(series_id % 4) + 1],
    aircrafts[(series_id % 5) + 1]
FROM generate_series(1, 10000) AS series_id
CROSS JOIN (
    SELECT
        ARRAY['MSQ','BQT','GME','VTB','GNA','SVO','LED','WAW','TBS','IST']::char(3)[] AS airports,
        ARRAY['Scheduled','On Time','Departed','Arrived']::varchar[] AS statuses,
        ARRAY['SU9','320','321','738','E95']::char(3)[] AS aircrafts
) AS constants
ON CONFLICT DO NOTHING;

INSERT INTO aviation.bookings (book_ref, book_date, total_amount)
SELECT
    lpad(series_id::text, 6, '0'),
    timestamptz '2024-10-01 00:00:00+03'
        + (series_id % 365) * interval '1 day',
    (5000 + (series_id * 7919) % 495000)::numeric(12, 2)
FROM generate_series(1, 50000) AS series_id
ON CONFLICT DO NOTHING;

INSERT INTO aviation.tickets (
    ticket_no,
    book_ref,
    passenger_id,
    passenger_name
)
SELECT
    lpad(series_id::text, 13, '0'),
    lpad((((series_id - 1) % 50000) + 1)::text, 6, '0'),
    concat(lpad((series_id % 9999)::text, 4, '0'), '-', lpad((series_id % 999999)::text, 6, '0')),
    concat('Passenger ', series_id)
FROM generate_series(1, 100000) AS series_id
ON CONFLICT DO NOTHING;

INSERT INTO aviation.ticket_flights (
    ticket_no,
    flight_id,
    fare_conditions,
    amount
)
SELECT
    lpad(series_id::text, 13, '0'),
    ((series_id * 37 - 1) % 10000) + 1,
    CASE
        WHEN series_id % 20 = 0 THEN 'Business'
        WHEN series_id % 7 = 0 THEN 'Comfort'
        ELSE 'Economy'
    END,
    (3000 + (series_id * 3571) % 120000)::numeric(12, 2)
FROM generate_series(1, 100000) AS series_id
ON CONFLICT DO NOTHING;

INSERT INTO aviation.boarding_passes (
    ticket_no,
    flight_id,
    boarding_no,
    seat_no
)
SELECT
    ticket_no,
    flight_id,
    row_number() OVER (PARTITION BY flight_id ORDER BY ticket_no),
    concat((row_number() OVER (PARTITION BY flight_id ORDER BY ticket_no) - 1) / 6 + 1,
           chr(65 + ((row_number() OVER (PARTITION BY flight_id ORDER BY ticket_no) - 1) % 6)::integer))
FROM aviation.ticket_flights
WHERE right(ticket_no, 1)::integer < 8
ON CONFLICT DO NOTHING;

ANALYZE aviation.airports;
ANALYZE aviation.aircrafts;
ANALYZE aviation.seats;
ANALYZE aviation.flights;
ANALYZE aviation.bookings;
ANALYZE aviation.tickets;
ANALYZE aviation.ticket_flights;
ANALYZE aviation.boarding_passes;

