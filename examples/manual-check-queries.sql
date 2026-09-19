-- Набор можно выполнять по одному вручную или целиком загрузить кнопкой
-- «Калибровать по файлу SQL». Все запросы предназначены для учебной aviation БД.

-- 1. Простой фильтр и сортировка.
SELECT flight_id, scheduled_departure
FROM aviation.flights
WHERE departure_airport = 'MSQ'
ORDER BY scheduled_departure;

-- 2. Другие параметры того же структурного шаблона.
SELECT flight_id, scheduled_departure
FROM aviation.flights
WHERE departure_airport = 'SVO'
ORDER BY scheduled_departure;

-- Третий вариант нужен для устойчивой калибровки этого шаблона.
SELECT flight_id, scheduled_departure
FROM aviation.flights
WHERE departure_airport = 'LED'
ORDER BY scheduled_departure;

-- Ещё семь параметров того же шаблона дают локальной калибровке достаточно
-- наблюдений именно для простого поиска рейсов.
SELECT flight_id, scheduled_departure FROM aviation.flights WHERE departure_airport = 'WAW' ORDER BY scheduled_departure;
SELECT flight_id, scheduled_departure FROM aviation.flights WHERE departure_airport = 'GME' ORDER BY scheduled_departure;
SELECT flight_id, scheduled_departure FROM aviation.flights WHERE departure_airport = 'GNA' ORDER BY scheduled_departure;
SELECT flight_id, scheduled_departure FROM aviation.flights WHERE departure_airport = 'BQT' ORDER BY scheduled_departure;
SELECT flight_id, scheduled_departure FROM aviation.flights WHERE departure_airport = 'VTB' ORDER BY scheduled_departure;
SELECT flight_id, scheduled_departure FROM aviation.flights WHERE departure_airport = 'IST' ORDER BY scheduled_departure;
SELECT flight_id, scheduled_departure FROM aviation.flights WHERE departure_airport = 'TBS' ORDER BY scheduled_departure;

-- 3. Диапазон дат.
SELECT flight_id, flight_no, status
FROM aviation.flights
WHERE scheduled_departure BETWEEN TIMESTAMP '2025-01-01' AND TIMESTAMP '2025-02-01'
ORDER BY scheduled_departure;

-- 4. Поиск бронирований по сумме.
SELECT book_ref, book_date, total_amount
FROM aviation.bookings
WHERE total_amount BETWEEN 25000 AND 75000
ORDER BY total_amount DESC
LIMIT 100;

-- 5. JOIN рейсов и аэропортов.
SELECT f.flight_id, departure.city AS departure_city, arrival.city AS arrival_city
FROM aviation.flights AS f
JOIN aviation.airports AS departure ON departure.airport_code = f.departure_airport
JOIN aviation.airports AS arrival ON arrival.airport_code = f.arrival_airport
WHERE f.status = 'Scheduled'
LIMIT 500;

-- 6. История сегментов пассажиров.
SELECT t.passenger_id, f.flight_no, f.scheduled_departure, tf.amount
FROM aviation.tickets AS t
JOIN aviation.ticket_flights AS tf ON tf.ticket_no = t.ticket_no
JOIN aviation.flights AS f ON f.flight_id = tf.flight_id
WHERE t.passenger_id LIKE '0001%'
ORDER BY f.scheduled_departure DESC;

-- 7. Агрегация по статусам.
SELECT status, COUNT(*) AS flight_count
FROM aviation.flights
GROUP BY status
ORDER BY flight_count DESC;

-- 8. Агрегация по маршрутам.
SELECT departure_airport, arrival_airport, COUNT(*) AS flight_count
FROM aviation.flights
GROUP BY departure_airport, arrival_airport
HAVING COUNT(*) >= 5
ORDER BY flight_count DESC;

-- 9. Доход по маршрутам с несколькими JOIN.
SELECT f.departure_airport, f.arrival_airport,
       COUNT(*) AS ticket_count, SUM(tf.amount) AS revenue
FROM aviation.flights AS f
JOIN aviation.ticket_flights AS tf ON tf.flight_id = f.flight_id
GROUP BY f.departure_airport, f.arrival_airport
ORDER BY revenue DESC;

-- 10. Загрузка самолётов.
WITH boarded AS (
    SELECT f.aircraft_code, COUNT(bp.ticket_no) AS boarded_count
    FROM aviation.flights AS f
    LEFT JOIN aviation.boarding_passes AS bp ON bp.flight_id = f.flight_id
    GROUP BY f.aircraft_code
), capacity AS (
    SELECT aircraft_code, COUNT(*) AS seat_count
    FROM aviation.seats
    GROUP BY aircraft_code
)
SELECT b.aircraft_code, b.boarded_count, c.seat_count
FROM boarded AS b
JOIN capacity AS c ON c.aircraft_code = b.aircraft_code
ORDER BY b.boarded_count DESC;

-- 11. Коррелированный EXISTS.
SELECT b.book_ref, b.total_amount
FROM aviation.bookings AS b
WHERE EXISTS (
    SELECT 1
    FROM aviation.tickets AS t
    WHERE t.book_ref = b.book_ref
)
ORDER BY b.total_amount DESC
LIMIT 200;

-- 12. NOT EXISTS: рейсы без проданных билетов.
SELECT f.flight_id, f.flight_no
FROM aviation.flights AS f
WHERE NOT EXISTS (
    SELECT 1
    FROM aviation.ticket_flights AS tf
    WHERE tf.flight_id = f.flight_id
)
LIMIT 200;

-- 13. Оконная функция и ранжирование.
SELECT departure_airport, arrival_airport, flight_count,
       DENSE_RANK() OVER (PARTITION BY departure_airport ORDER BY flight_count DESC) AS route_rank
FROM (
    SELECT departure_airport, arrival_airport, COUNT(*) AS flight_count
    FROM aviation.flights
    GROUP BY departure_airport, arrival_airport
) AS route_counts
ORDER BY departure_airport, route_rank;

-- 14. CTE со сводкой.
WITH booking_totals AS (
    SELECT t.book_ref, SUM(tf.amount) AS segment_total
    FROM aviation.tickets AS t
    JOIN aviation.ticket_flights AS tf ON tf.ticket_no = t.ticket_no
    GROUP BY t.book_ref
)
SELECT b.book_ref, b.total_amount, bt.segment_total
FROM aviation.bookings AS b
JOIN booking_totals AS bt ON bt.book_ref = b.book_ref
WHERE bt.segment_total > 50000
ORDER BY bt.segment_total DESC
LIMIT 200;

-- 15. UNION ALL по направлениям.
SELECT departure_airport AS airport_code, COUNT(*) AS event_count
FROM aviation.flights
GROUP BY departure_airport
UNION ALL
SELECT arrival_airport AS airport_code, COUNT(*) AS event_count
FROM aviation.flights
GROUP BY arrival_airport;

-- 16. Проверка правила HAVING -> WHERE.
SELECT status, COUNT(*) AS flight_count
FROM aviation.flights
GROUP BY status
HAVING status <> 'Cancelled' AND COUNT(*) >= 1;

-- 17. Проверка лишней внутренней сортировки.
SELECT flight_id, departure_airport
FROM (
    SELECT flight_id, departure_airport
    FROM aviation.flights
    ORDER BY scheduled_departure
) AS ordered_flights
ORDER BY flight_id;

-- 18. LEFT JOIN с фильтром правой таблицы: анализатор предложит проверить INNER JOIN.
SELECT f.flight_id, a.city
FROM aviation.flights AS f
LEFT JOIN aviation.airports AS a ON a.airport_code = f.departure_airport
WHERE a.city = 'Minsk'
ORDER BY f.flight_id;

-- 19. OR по одному столбцу: кандидат на IN.
SELECT flight_id, status
FROM aviation.flights
WHERE departure_airport = 'MSQ'
   OR departure_airport = 'SVO'
   OR departure_airport = 'LED';

-- 20. Глубокая страница: кандидат на keyset pagination.
SELECT flight_id, scheduled_departure
FROM aviation.flights
ORDER BY scheduled_departure, flight_id
OFFSET 1000 LIMIT 50;
