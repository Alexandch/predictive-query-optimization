"""Independent production-like stress workload for the aviation schema."""

from __future__ import annotations

from datetime import date, timedelta
import random
from typing import Callable

from .query_case import QueryCase


class ProductionQueryGenerator:
    """Generate structures deliberately absent from the training generator."""

    AIRPORTS = ("MSQ", "BQT", "GME", "VTB", "GNA", "SVO", "LED", "WAW", "TBS", "IST")
    FARES = ("Economy", "Comfort", "Business")
    STATUSES = ("Scheduled", "On Time", "Departed", "Arrived")
    TEMPLATE_IDS = (
        "prod_customer_value_deciles", "prod_booking_reconciliation",
        "prod_passenger_itinerary_gaps", "prod_airport_hourly_pressure",
        "prod_booking_funnel", "prod_fare_mix_change", "prod_route_concentration",
        "prod_aircraft_turnaround", "prod_rolling_booking_revenue",
        "prod_route_top_passengers", "prod_integrity_audit",
        "prod_duplicate_passenger_summary", "prod_revenue_iqr_outliers",
        "prod_load_factor_percentile", "prod_network_two_hop",
        "prod_route_status_cube", "prod_passenger_no_show_streak",
        "prod_seat_heatmap", "prod_aircraft_duration_fit",
        "prod_latest_booking_per_passenger", "prod_unsold_business_inventory",
        "prod_multi_segment_bookings", "prod_boarded_frequent_flyers",
        "prod_tickets_without_boarding", "prod_route_peer_comparison",
        "prod_lateral_top_fares", "prod_materialized_route_comparison",
        "prod_recursive_calendar_gaps", "prod_booking_cohort_retention",
        "prod_route_top_n_per_month",
    )

    def __init__(self, seed: int = 7301) -> None:
        self.random = random.Random(seed)
        self._templates: tuple[Callable[[], QueryCase], ...] = (
            self._customer_value_deciles,
            self._booking_reconciliation,
            self._passenger_itinerary_gaps,
            self._airport_hourly_pressure,
            self._booking_funnel,
            self._fare_mix_change,
            self._route_concentration,
            self._aircraft_turnaround,
            self._rolling_booking_revenue,
            self._route_top_passengers,
            self._integrity_audit,
            self._duplicate_passenger_summary,
            self._revenue_iqr_outliers,
            self._load_factor_percentile,
            self._network_two_hop,
            self._route_status_cube,
            self._passenger_no_show_streak,
            self._seat_heatmap,
            self._aircraft_duration_fit,
            self._latest_booking_per_passenger,
            self._unsold_business_inventory,
            self._multi_segment_bookings,
            self._boarded_frequent_flyers,
            self._tickets_without_boarding,
            self._route_peer_comparison,
            self._lateral_top_fares,
            self._materialized_route_comparison,
            self._recursive_calendar_gaps,
            self._booking_cohort_retention,
            self._route_top_n_per_month,
        )

    @property
    def template_ids(self) -> tuple[str, ...]:
        return self.TEMPLATE_IDS

    def generate(self, count: int) -> list[QueryCase]:
        if count <= 0:
            raise ValueError("count must be greater than zero")
        cases: list[QueryCase] = []
        while len(cases) < count:
            cycle = list(self._templates)
            self.random.shuffle(cycle)
            cases.extend(template() for template in cycle[: count - len(cases)])
        return cases

    def generate_one_per_template(self) -> list[QueryCase]:
        return [template() for template in self._templates]

    def _airport(self) -> str:
        return self.random.choice(self.AIRPORTS)

    def _flight_day(self) -> date:
        return date(2025, 1, 1) + timedelta(days=self.random.randrange(330))

    def _booking_day(self) -> date:
        return date(2024, 10, 1) + timedelta(days=self.random.randrange(300))

    @staticmethod
    def _case(template_id: str, sql_text: str) -> QueryCase:
        return QueryCase(f"prod_{template_id}", " ".join(sql_text.split()))

    def _customer_value_deciles(self) -> QueryCase:
        minimum = self.random.choice((2, 3, 4, 5))
        return self._case("customer_value_deciles", f"""
            WITH passenger_value AS (
                SELECT t.passenger_id, COUNT(DISTINCT t.ticket_no) AS tickets,
                       SUM(tf.amount) AS revenue
                FROM aviation.tickets t
                JOIN aviation.ticket_flights tf ON tf.ticket_no = t.ticket_no
                GROUP BY t.passenger_id HAVING COUNT(DISTINCT t.ticket_no) >= {minimum}
            ), scored AS (
                SELECT *, NTILE(10) OVER (ORDER BY revenue DESC) AS value_decile
                FROM passenger_value
            )
            SELECT value_decile, COUNT(*) AS passengers, AVG(revenue) AS avg_revenue
            FROM scored GROUP BY value_decile ORDER BY value_decile
        """)

    def _booking_reconciliation(self) -> QueryCase:
        tolerance = self.random.choice((500, 1000, 2500, 5000))
        return self._case("booking_reconciliation", f"""
            SELECT b.book_ref, b.total_amount, COALESCE(SUM(tf.amount), 0) AS sold_amount,
                   b.total_amount - COALESCE(SUM(tf.amount), 0) AS difference
            FROM aviation.bookings b
            LEFT JOIN aviation.tickets t ON t.book_ref = b.book_ref
            LEFT JOIN aviation.ticket_flights tf ON tf.ticket_no = t.ticket_no
            GROUP BY b.book_ref, b.total_amount
            HAVING ABS(b.total_amount - COALESCE(SUM(tf.amount), 0)) > {tolerance}
            ORDER BY ABS(b.total_amount - COALESCE(SUM(tf.amount), 0)) DESC LIMIT 200
        """)

    def _passenger_itinerary_gaps(self) -> QueryCase:
        hours = self.random.choice((2, 4, 8, 12))
        return self._case("passenger_itinerary_gaps", f"""
            WITH itinerary AS (
                SELECT t.passenger_id, f.flight_id, f.departure_airport, f.arrival_airport,
                       f.scheduled_departure, f.scheduled_arrival,
                       LAG(f.scheduled_arrival) OVER (
                           PARTITION BY t.passenger_id ORDER BY f.scheduled_departure
                       ) AS previous_arrival
                FROM aviation.tickets t
                JOIN aviation.ticket_flights tf ON tf.ticket_no = t.ticket_no
                JOIN aviation.flights f ON f.flight_id = tf.flight_id
            )
            SELECT * FROM itinerary
            WHERE scheduled_departure - previous_arrival < INTERVAL '{hours} hours'
            ORDER BY scheduled_departure - previous_arrival LIMIT 200
        """)

    def _airport_hourly_pressure(self) -> QueryCase:
        airport, day = self._airport(), self._flight_day()
        return self._case("airport_hourly_pressure", f"""
            WITH hourly AS (
                SELECT date_trunc('hour', scheduled_departure) AS hour_start,
                       COUNT(*) AS departures
                FROM aviation.flights
                WHERE departure_airport = '{airport}'
                  AND scheduled_departure >= DATE '{day}' - INTERVAL '14 days'
                  AND scheduled_departure < DATE '{day}' + INTERVAL '14 days'
                GROUP BY date_trunc('hour', scheduled_departure)
            )
            SELECT hour_start, departures,
                   SUM(departures) OVER (ORDER BY hour_start ROWS BETWEEN 3 PRECEDING AND 3 FOLLOWING) AS seven_hour_pressure,
                   CUME_DIST() OVER (ORDER BY departures) AS pressure_percentile
            FROM hourly ORDER BY seven_hour_pressure DESC LIMIT 100
        """)

    def _booking_funnel(self) -> QueryCase:
        day = self._booking_day()
        return self._case("booking_funnel", f"""
            WITH cohort AS (
                SELECT book_ref FROM aviation.bookings
                WHERE book_date >= DATE '{day}' AND book_date < DATE '{day}' + INTERVAL '30 days'
            ), ticketed AS (
                SELECT c.book_ref, COUNT(DISTINCT t.ticket_no) AS tickets
                FROM cohort c LEFT JOIN aviation.tickets t ON t.book_ref = c.book_ref GROUP BY c.book_ref
            ), segmented AS (
                SELECT x.book_ref, x.tickets, COUNT(tf.flight_id) AS segments,
                       COUNT(bp.ticket_no) AS boarded
                FROM ticketed x LEFT JOIN aviation.tickets t ON t.book_ref = x.book_ref
                LEFT JOIN aviation.ticket_flights tf ON tf.ticket_no = t.ticket_no
                LEFT JOIN aviation.boarding_passes bp ON bp.ticket_no = tf.ticket_no AND bp.flight_id = tf.flight_id
                GROUP BY x.book_ref, x.tickets
            )
            SELECT COUNT(*) AS bookings, COUNT(*) FILTER (WHERE tickets > 0) AS with_tickets,
                   COUNT(*) FILTER (WHERE segments > 0) AS with_segments,
                   COUNT(*) FILTER (WHERE boarded > 0) AS with_boarding
            FROM segmented
        """)

    def _fare_mix_change(self) -> QueryCase:
        airport = self._airport()
        return self._case("fare_mix_change", f"""
            WITH monthly AS (
                SELECT date_trunc('month', f.scheduled_departure) AS month_start,
                       tf.fare_conditions, COUNT(*) AS sold
                FROM aviation.flights f JOIN aviation.ticket_flights tf ON tf.flight_id = f.flight_id
                WHERE f.departure_airport = '{airport}'
                GROUP BY date_trunc('month', f.scheduled_departure), tf.fare_conditions
            ), shares AS (
                SELECT *, sold::numeric / SUM(sold) OVER (PARTITION BY month_start) AS share
                FROM monthly
            )
            SELECT *, share - LAG(share) OVER (PARTITION BY fare_conditions ORDER BY month_start) AS share_change
            FROM shares ORDER BY ABS(share - LAG(share) OVER (PARTITION BY fare_conditions ORDER BY month_start)) DESC NULLS LAST
        """)

    def _route_concentration(self) -> QueryCase:
        status = self.random.choice(self.STATUSES)
        return self._case("route_concentration", f"""
            WITH route_counts AS (
                SELECT departure_airport, arrival_airport, COUNT(*)::numeric AS flights
                FROM aviation.flights WHERE status = '{status}'
                GROUP BY departure_airport, arrival_airport
            ), shares AS (
                SELECT *, flights / SUM(flights) OVER (PARTITION BY departure_airport) AS share
                FROM route_counts
            )
            SELECT departure_airport, SUM(share * share) AS hhi,
                   COUNT(*) AS served_destinations
            FROM shares GROUP BY departure_airport ORDER BY hhi DESC
        """)

    def _aircraft_turnaround(self) -> QueryCase:
        aircraft = self.random.choice(("SU9", "320", "321", "738", "E95"))
        return self._case("aircraft_turnaround", f"""
            WITH rotations AS (
                SELECT flight_id, departure_airport, arrival_airport, scheduled_departure,
                       LAG(scheduled_arrival) OVER (
                           PARTITION BY aircraft_code ORDER BY scheduled_departure
                       ) AS prior_arrival
                FROM aviation.flights WHERE aircraft_code = '{aircraft}'
            )
            SELECT departure_airport, COUNT(*) AS rotations,
                   AVG(scheduled_departure - prior_arrival) AS avg_turnaround,
                   MIN(scheduled_departure - prior_arrival) AS min_turnaround
            FROM rotations WHERE prior_arrival IS NOT NULL
            GROUP BY departure_airport ORDER BY min_turnaround
        """)

    def _rolling_booking_revenue(self) -> QueryCase:
        days = self.random.choice((6, 13, 27, 55))
        return self._case("rolling_booking_revenue", f"""
            WITH daily AS (
                SELECT book_date::date AS day, SUM(total_amount) AS revenue,
                       COUNT(*) AS bookings FROM aviation.bookings GROUP BY book_date::date
            )
            SELECT day, revenue, bookings,
                   SUM(revenue) OVER (ORDER BY day ROWS BETWEEN {days} PRECEDING AND CURRENT ROW) AS rolling_revenue,
                   AVG(bookings) OVER (ORDER BY day ROWS BETWEEN {days} PRECEDING AND CURRENT ROW) AS rolling_bookings
            FROM daily ORDER BY day
        """)

    def _route_top_passengers(self) -> QueryCase:
        airport = self._airport()
        return self._case("route_top_passengers", f"""
            SELECT r.arrival_airport, p.passenger_id, p.route_revenue
            FROM (SELECT DISTINCT arrival_airport FROM aviation.flights WHERE departure_airport = '{airport}') r
            CROSS JOIN LATERAL (
                SELECT t.passenger_id, SUM(tf.amount) AS route_revenue
                FROM aviation.flights f JOIN aviation.ticket_flights tf ON tf.flight_id = f.flight_id
                JOIN aviation.tickets t ON t.ticket_no = tf.ticket_no
                WHERE f.departure_airport = '{airport}' AND f.arrival_airport = r.arrival_airport
                GROUP BY t.passenger_id ORDER BY route_revenue DESC LIMIT 5
            ) p ORDER BY r.arrival_airport, p.route_revenue DESC
        """)

    def _integrity_audit(self) -> QueryCase:
        minimum_flight = self.random.randrange(1, 40_001, 1000)
        return self._case("integrity_audit", f"""
            SELECT 'tickets_without_booking' AS check_name, COUNT(*) AS failures
            FROM aviation.tickets t LEFT JOIN aviation.bookings b ON b.book_ref = t.book_ref WHERE b.book_ref IS NULL
            UNION ALL
            SELECT 'segments_without_ticket', COUNT(*)
            FROM aviation.ticket_flights tf LEFT JOIN aviation.tickets t ON t.ticket_no = tf.ticket_no WHERE t.ticket_no IS NULL
            UNION ALL
            SELECT 'boarding_without_segment', COUNT(*)
            FROM aviation.boarding_passes bp LEFT JOIN aviation.ticket_flights tf
              ON tf.ticket_no = bp.ticket_no AND tf.flight_id = bp.flight_id WHERE tf.ticket_no IS NULL
            UNION ALL
            SELECT 'flight_without_aircraft', COUNT(*)
            FROM aviation.flights f LEFT JOIN aviation.aircrafts a ON a.aircraft_code = f.aircraft_code
            WHERE a.aircraft_code IS NULL AND f.flight_id >= {minimum_flight}
        """)

    def _duplicate_passenger_summary(self) -> QueryCase:
        minimum = self.random.choice((2, 3, 4, 5))
        return self._case("duplicate_passenger_summary", f"""
            SELECT passenger_id, MIN(passenger_name) AS canonical_name,
                   COUNT(DISTINCT passenger_name) AS name_versions,
                   COUNT(DISTINCT book_ref) AS bookings
            FROM aviation.tickets GROUP BY passenger_id
            HAVING COUNT(DISTINCT book_ref) >= {minimum}
            ORDER BY name_versions DESC, bookings DESC LIMIT 200
        """)

    def _revenue_iqr_outliers(self) -> QueryCase:
        fare = self.random.choice(self.FARES)
        return self._case("revenue_iqr_outliers", f"""
            WITH route_stats AS (
                SELECT f.departure_airport, f.arrival_airport,
                       percentile_cont(0.25) WITHIN GROUP (ORDER BY tf.amount) AS q1,
                       percentile_cont(0.75) WITHIN GROUP (ORDER BY tf.amount) AS q3
                FROM aviation.flights f JOIN aviation.ticket_flights tf ON tf.flight_id = f.flight_id
                WHERE tf.fare_conditions = '{fare}' GROUP BY f.departure_airport, f.arrival_airport
            )
            SELECT f.flight_id, tf.ticket_no, tf.amount, s.q1, s.q3
            FROM aviation.ticket_flights tf JOIN aviation.flights f ON f.flight_id = tf.flight_id
            JOIN route_stats s USING (departure_airport, arrival_airport)
            WHERE tf.fare_conditions = '{fare}' AND (tf.amount < s.q1 - 1.5 * (s.q3 - s.q1)
               OR tf.amount > s.q3 + 1.5 * (s.q3 - s.q1))
            ORDER BY tf.amount DESC LIMIT 200
        """)

    def _load_factor_percentile(self) -> QueryCase:
        airport = self._airport()
        return self._case("load_factor_percentile", f"""
            WITH capacity AS (
                SELECT aircraft_code, COUNT(*) AS seats FROM aviation.seats GROUP BY aircraft_code
            ), loads AS (
                SELECT f.flight_id, f.arrival_airport, COUNT(tf.ticket_no)::numeric / c.seats AS load_factor
                FROM aviation.flights f JOIN capacity c ON c.aircraft_code = f.aircraft_code
                LEFT JOIN aviation.ticket_flights tf ON tf.flight_id = f.flight_id
                WHERE f.departure_airport = '{airport}' GROUP BY f.flight_id, f.arrival_airport, c.seats
            )
            SELECT *, PERCENT_RANK() OVER (PARTITION BY arrival_airport ORDER BY load_factor) AS route_percentile
            FROM loads ORDER BY route_percentile DESC, load_factor DESC LIMIT 200
        """)

    def _network_two_hop(self) -> QueryCase:
        airport = self._airport()
        return self._case("network_two_hop", f"""
            WITH RECURSIVE network(origin, destination, depth, path) AS (
                SELECT departure_airport, arrival_airport, 1,
                       ARRAY[departure_airport::text, arrival_airport::text]
                FROM aviation.flights WHERE departure_airport = '{airport}'
                UNION
                SELECT n.origin, f.arrival_airport, n.depth + 1, n.path || f.arrival_airport
                FROM network n JOIN aviation.flights f ON f.departure_airport = n.destination
                WHERE n.depth < 2 AND NOT f.arrival_airport = ANY(n.path)
            )
            SELECT destination, MIN(depth) AS hops, COUNT(*) AS alternatives
            FROM network GROUP BY destination ORDER BY hops, alternatives DESC
        """)

    def _route_status_cube(self) -> QueryCase:
        day = self._flight_day()
        return self._case("route_status_cube", f"""
            SELECT departure_airport, arrival_airport, status, aircraft_code,
                   COUNT(*) AS flights,
                   AVG(EXTRACT(EPOCH FROM (scheduled_arrival - scheduled_departure)) / 60) AS avg_minutes
            FROM aviation.flights
            WHERE scheduled_departure >= DATE '{day}' - INTERVAL '60 days'
              AND scheduled_departure < DATE '{day}' + INTERVAL '60 days'
            GROUP BY CUBE (departure_airport, arrival_airport, status, aircraft_code)
            HAVING COUNT(*) >= 2 ORDER BY flights DESC NULLS LAST LIMIT 500
        """)

    def _passenger_no_show_streak(self) -> QueryCase:
        minimum = self.random.choice((2, 3, 4, 5))
        return self._case("passenger_no_show_streak", f"""
            WITH attendance AS (
                SELECT t.passenger_id, f.scheduled_departure,
                       CASE WHEN bp.ticket_no IS NULL THEN 1 ELSE 0 END AS no_show
                FROM aviation.tickets t JOIN aviation.ticket_flights tf ON tf.ticket_no = t.ticket_no
                JOIN aviation.flights f ON f.flight_id = tf.flight_id
                LEFT JOIN aviation.boarding_passes bp ON bp.ticket_no = tf.ticket_no AND bp.flight_id = tf.flight_id
            ), runs AS (
                SELECT *, SUM(CASE WHEN no_show = 0 THEN 1 ELSE 0 END)
                    OVER (PARTITION BY passenger_id ORDER BY scheduled_departure) AS run_id
                FROM attendance
            )
            SELECT passenger_id, run_id, COUNT(*) AS consecutive_no_shows
            FROM runs WHERE no_show = 1 GROUP BY passenger_id, run_id
            HAVING COUNT(*) >= {minimum} ORDER BY consecutive_no_shows DESC LIMIT 200
        """)

    def _seat_heatmap(self) -> QueryCase:
        aircraft = self.random.choice(("SU9", "320", "321", "738", "E95"))
        return self._case("seat_heatmap", f"""
            SELECT s.seat_no, s.fare_conditions, COUNT(DISTINCT f.flight_id) AS operated_flights,
                   COUNT(bp.ticket_no) AS occupied,
                   COUNT(bp.ticket_no)::numeric / NULLIF(COUNT(DISTINCT f.flight_id), 0) AS utilization
            FROM aviation.seats s JOIN aviation.flights f ON f.aircraft_code = s.aircraft_code
            LEFT JOIN aviation.boarding_passes bp ON bp.flight_id = f.flight_id AND bp.seat_no = s.seat_no
            WHERE s.aircraft_code = '{aircraft}'
            GROUP BY s.seat_no, s.fare_conditions ORDER BY utilization DESC, s.seat_no
        """)

    def _aircraft_duration_fit(self) -> QueryCase:
        factor = self.random.choice((0.7, 0.8, 0.9, 1.0))
        return self._case("aircraft_duration_fit", f"""
            SELECT a.aircraft_code, a.model, f.departure_airport, f.arrival_airport,
                   COUNT(*) AS flights,
                   AVG(EXTRACT(EPOCH FROM (f.scheduled_arrival - f.scheduled_departure)) / 3600) AS avg_hours
            FROM aviation.aircrafts a JOIN aviation.flights f ON f.aircraft_code = a.aircraft_code
            GROUP BY a.aircraft_code, a.model, f.departure_airport, f.arrival_airport, a.range_km
            HAVING AVG(EXTRACT(EPOCH FROM (f.scheduled_arrival - f.scheduled_departure)) / 3600) * 800 > a.range_km * {factor}
            ORDER BY avg_hours DESC
        """)

    def _latest_booking_per_passenger(self) -> QueryCase:
        prefix = self.random.randrange(100)
        return self._case("latest_booking_per_passenger", f"""
            SELECT DISTINCT ON (t.passenger_id) t.passenger_id, t.passenger_name,
                   b.book_ref, b.book_date, b.total_amount
            FROM aviation.tickets t JOIN aviation.bookings b ON b.book_ref = t.book_ref
            WHERE t.passenger_id LIKE '{prefix:02d}%'
            ORDER BY t.passenger_id, b.book_date DESC, b.book_ref DESC
        """)

    def _unsold_business_inventory(self) -> QueryCase:
        day = self._flight_day()
        return self._case("unsold_business_inventory", f"""
            SELECT f.flight_id, f.departure_airport, f.arrival_airport,
                   COUNT(DISTINCT s.seat_no) AS business_capacity,
                   COUNT(DISTINCT tf.ticket_no) AS business_sold
            FROM aviation.flights f JOIN aviation.seats s ON s.aircraft_code = f.aircraft_code
              AND s.fare_conditions = 'Business'
            LEFT JOIN aviation.ticket_flights tf ON tf.flight_id = f.flight_id
              AND tf.fare_conditions = 'Business'
            WHERE f.scheduled_departure >= DATE '{day}'
              AND f.scheduled_departure < DATE '{day}' + INTERVAL '7 days'
            GROUP BY f.flight_id HAVING COUNT(DISTINCT tf.ticket_no) < COUNT(DISTINCT s.seat_no)
            ORDER BY COUNT(DISTINCT s.seat_no) - COUNT(DISTINCT tf.ticket_no) DESC LIMIT 200
        """)

    def _multi_segment_bookings(self) -> QueryCase:
        segments = self.random.choice((2, 3, 4))
        return self._case("multi_segment_bookings", f"""
            SELECT b.book_ref, b.book_date, COUNT(DISTINCT t.ticket_no) AS passengers,
                   COUNT(tf.flight_id) AS segments, SUM(tf.amount) AS revenue
            FROM aviation.bookings b JOIN aviation.tickets t ON t.book_ref = b.book_ref
            JOIN aviation.ticket_flights tf ON tf.ticket_no = t.ticket_no
            WHERE EXISTS (
                SELECT 1 FROM aviation.ticket_flights x WHERE x.ticket_no = t.ticket_no
                GROUP BY x.ticket_no HAVING COUNT(*) >= {segments}
            )
            GROUP BY b.book_ref, b.book_date ORDER BY segments DESC, revenue DESC LIMIT 200
        """)

    def _boarded_frequent_flyers(self) -> QueryCase:
        minimum = self.random.choice((2, 3, 4, 5))
        return self._case("boarded_frequent_flyers", f"""
            SELECT passenger_id FROM aviation.tickets GROUP BY passenger_id HAVING COUNT(*) >= {minimum}
            INTERSECT
            SELECT t.passenger_id FROM aviation.tickets t
            JOIN aviation.boarding_passes bp ON bp.ticket_no = t.ticket_no
            GROUP BY t.passenger_id HAVING COUNT(*) >= {minimum}
            ORDER BY passenger_id LIMIT 500
        """)

    def _tickets_without_boarding(self) -> QueryCase:
        fare = self.random.choice(self.FARES)
        return self._case("tickets_without_boarding", f"""
            SELECT tf.ticket_no FROM aviation.ticket_flights tf WHERE tf.fare_conditions = '{fare}'
            EXCEPT
            SELECT bp.ticket_no FROM aviation.boarding_passes bp
            ORDER BY ticket_no LIMIT 500
        """)

    def _route_peer_comparison(self) -> QueryCase:
        factor = self.random.choice((1.05, 1.10, 1.20, 1.30))
        return self._case("route_peer_comparison", f"""
            SELECT f.departure_airport, f.arrival_airport, AVG(tf.amount) AS avg_fare
            FROM aviation.flights f JOIN aviation.ticket_flights tf ON tf.flight_id = f.flight_id
            GROUP BY f.departure_airport, f.arrival_airport
            HAVING AVG(tf.amount) > {factor} * (
                SELECT AVG(tf2.amount) FROM aviation.flights f2
                JOIN aviation.ticket_flights tf2 ON tf2.flight_id = f2.flight_id
                WHERE f2.departure_airport = f.departure_airport
            ) ORDER BY avg_fare DESC
        """)

    def _lateral_top_fares(self) -> QueryCase:
        fare = self.random.choice(self.FARES)
        return self._case("lateral_top_fares", f"""
            SELECT route.departure_airport, route.arrival_airport,
                   expensive.ticket_no, expensive.amount
            FROM (SELECT DISTINCT departure_airport, arrival_airport FROM aviation.flights) route
            CROSS JOIN LATERAL (
                SELECT tf.ticket_no, tf.amount FROM aviation.flights f
                JOIN aviation.ticket_flights tf ON tf.flight_id = f.flight_id
                WHERE f.departure_airport = route.departure_airport
                  AND f.arrival_airport = route.arrival_airport
                  AND tf.fare_conditions = '{fare}'
                ORDER BY tf.amount DESC LIMIT 3
            ) expensive ORDER BY route.departure_airport, route.arrival_airport, expensive.amount DESC
        """)

    def _materialized_route_comparison(self) -> QueryCase:
        airport = self._airport()
        return self._case("materialized_route_comparison", f"""
            WITH route_sales AS MATERIALIZED (
                SELECT f.departure_airport, f.arrival_airport, COUNT(*) AS tickets,
                       SUM(tf.amount) AS revenue
                FROM aviation.flights f JOIN aviation.ticket_flights tf ON tf.flight_id = f.flight_id
                GROUP BY f.departure_airport, f.arrival_airport
            )
            SELECT current.arrival_airport, current.tickets, current.revenue,
                   AVG(peer.revenue) AS peer_average
            FROM route_sales current JOIN route_sales peer
              ON peer.departure_airport <> current.departure_airport
             AND peer.arrival_airport = current.arrival_airport
            WHERE current.departure_airport = '{airport}'
            GROUP BY current.arrival_airport, current.tickets, current.revenue
            ORDER BY current.revenue - AVG(peer.revenue) DESC
        """)

    def _recursive_calendar_gaps(self) -> QueryCase:
        start = self._flight_day()
        days = self.random.choice((14, 30, 60, 90))
        return self._case("recursive_calendar_gaps", f"""
            WITH RECURSIVE calendar(day) AS (
                SELECT DATE '{start}'
                UNION ALL SELECT day + 1 FROM calendar WHERE day < DATE '{start}' + {days}
            ), traffic AS (
                SELECT scheduled_departure::date AS day, COUNT(*) AS flights
                FROM aviation.flights GROUP BY scheduled_departure::date
            )
            SELECT c.day, COALESCE(t.flights, 0) AS flights,
                   AVG(COALESCE(t.flights, 0)) OVER (ORDER BY c.day ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS weekly_average
            FROM calendar c LEFT JOIN traffic t USING (day) ORDER BY c.day
        """)

    def _booking_cohort_retention(self) -> QueryCase:
        fare = self.random.choice(self.FARES)
        return self._case("booking_cohort_retention", f"""
            WITH first_booking AS (
                SELECT t.passenger_id, MIN(date_trunc('month', b.book_date)) AS cohort_month
                FROM aviation.tickets t JOIN aviation.bookings b ON b.book_ref = t.book_ref
                GROUP BY t.passenger_id
            ), activity AS (
                SELECT DISTINCT t.passenger_id, date_trunc('month', b.book_date) AS activity_month
                FROM aviation.tickets t JOIN aviation.bookings b ON b.book_ref = t.book_ref
                JOIN aviation.ticket_flights tf ON tf.ticket_no = t.ticket_no
                WHERE tf.fare_conditions = '{fare}'
            )
            SELECT f.cohort_month,
                   EXTRACT(YEAR FROM age(a.activity_month, f.cohort_month)) * 12
                     + EXTRACT(MONTH FROM age(a.activity_month, f.cohort_month)) AS month_number,
                   COUNT(DISTINCT a.passenger_id) AS retained_passengers
            FROM first_booking f JOIN activity a ON a.passenger_id = f.passenger_id
            GROUP BY f.cohort_month, month_number ORDER BY f.cohort_month, month_number
        """)

    def _route_top_n_per_month(self) -> QueryCase:
        top = self.random.choice((3, 5, 7, 10))
        return self._case("route_top_n_per_month", f"""
            WITH monthly_routes AS (
                SELECT date_trunc('month', f.scheduled_departure) AS month_start,
                       f.departure_airport, f.arrival_airport,
                       COUNT(*) AS tickets, SUM(tf.amount) AS revenue
                FROM aviation.flights f JOIN aviation.ticket_flights tf ON tf.flight_id = f.flight_id
                GROUP BY date_trunc('month', f.scheduled_departure), f.departure_airport, f.arrival_airport
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY month_start, departure_airport ORDER BY revenue DESC) AS position
                FROM monthly_routes
            )
            SELECT * FROM ranked WHERE position <= {top}
            ORDER BY month_start, departure_airport, position
        """)
