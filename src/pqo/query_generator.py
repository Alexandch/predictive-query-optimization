"""Deterministic parameterized queries for the aviation benchmark schema."""

from __future__ import annotations

from datetime import date, timedelta
import random
from typing import Callable

from .query_case import QueryCase


class AviationQueryGenerator:
    AIRPORTS = ("MSQ", "BQT", "GME", "VTB", "GNA", "SVO", "LED", "WAW", "TBS", "IST")
    FARES = ("Economy", "Comfort", "Business")
    STATUSES = ("Scheduled", "On Time", "Departed", "Arrived")
    FLIGHT_COUNT = 50_000
    BOOKING_COUNT = 250_000
    TICKET_COUNT = 500_000
    TEMPLATE_IDS = (
        "booking_amount_range",
        "route_by_date",
        "passengers_for_booking",
        "flight_revenue",
        "bookings_with_expensive_ticket",
        "recent_flights_sorted",
        "fare_statistics",
        "passenger_flight_history",
        "airport_departure_counts",
        "aircraft_flight_counts",
        "aircraft_seat_capacity",
        "ticket_name_prefix",
        "boarding_pass_lookup",
        "route_revenue",
        "unboarded_tickets",
        "high_value_passengers",
        "flight_occupancy",
        "airport_connections",
        "booking_ticket_count",
        "flights_without_tickets",
        "top_routes",
        "daily_flight_count",
        "aircraft_fare_capacity",
        "ticket_fare_lookup",
        "monthly_route_rank",
        "passenger_spending_rank",
        "booking_fare_breakdown",
        "aircraft_route_utilization",
        "fare_price_percentiles",
        "connecting_itineraries",
        "unoccupied_seats",
        "booking_month_outliers",
        "airport_status_pivot",
        "latest_route_flights",
        "boarding_efficiency",
        "routes_above_average_revenue",
        "airport_revenue_rollup",
        "passenger_fare_transitions",
        "route_traffic_share",
        "aircraft_capacity_load",
        "route_moving_average",
        "flight_gap_analysis",
        "booking_segment_summary",
        "fare_deviation",
        "airport_pair_union",
        "no_show_revenue",
        "aircraft_revenue_rank",
        "route_revenue_grouping_sets",
        "passenger_latest_segment",
        "flight_capacity_shortfall",
        "booking_value_buckets",
        "route_peak_comparison",
    )

    def __init__(self, seed: int = 42):
        self.random = random.Random(seed)
        self._templates: tuple[Callable[[], QueryCase], ...] = (
            self._booking_amount_range,
            self._route_by_date,
            self._passengers_for_booking,
            self._flight_revenue,
            self._bookings_with_expensive_ticket,
            self._recent_flights_sorted,
            self._fare_statistics,
            self._passenger_flight_history,
            self._airport_departure_counts,
            self._aircraft_flight_counts,
            self._aircraft_seat_capacity,
            self._ticket_name_prefix,
            self._boarding_pass_lookup,
            self._route_revenue,
            self._unboarded_tickets,
            self._high_value_passengers,
            self._flight_occupancy,
            self._airport_connections,
            self._booking_ticket_count,
            self._flights_without_tickets,
            self._top_routes,
            self._daily_flight_count,
            self._aircraft_fare_capacity,
            self._ticket_fare_lookup,
            self._monthly_route_rank,
            self._passenger_spending_rank,
            self._booking_fare_breakdown,
            self._aircraft_route_utilization,
            self._fare_price_percentiles,
            self._connecting_itineraries,
            self._unoccupied_seats,
            self._booking_month_outliers,
            self._airport_status_pivot,
            self._latest_route_flights,
            self._boarding_efficiency,
            self._routes_above_average_revenue,
            self._airport_revenue_rollup,
            self._passenger_fare_transitions,
            self._route_traffic_share,
            self._aircraft_capacity_load,
            self._route_moving_average,
            self._flight_gap_analysis,
            self._booking_segment_summary,
            self._fare_deviation,
            self._airport_pair_union,
            self._no_show_revenue,
            self._aircraft_revenue_rank,
            self._route_revenue_grouping_sets,
            self._passenger_latest_segment,
            self._flight_capacity_shortfall,
            self._booking_value_buckets,
            self._route_peak_comparison,
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
            remaining = count - len(cases)
            cases.extend(template() for template in cycle[:remaining])
        return cases

    def generate_one_per_template(self) -> list[QueryCase]:
        return [template() for template in self._templates]

    def _random_date(self) -> date:
        return date(2025, 1, 1) + timedelta(days=self.random.randrange(365))

    def _booking_amount_range(self) -> QueryCase:
        lower = self.random.randrange(5_000, 350_000, 5_000)
        upper = lower + self.random.randrange(25_000, 150_001, 5_000)
        return QueryCase(
            "booking_amount_range",
            "SELECT book_ref, book_date, total_amount "
            "FROM aviation.bookings "
            f"WHERE total_amount BETWEEN {lower} AND {upper}",
        )

    def _route_by_date(self) -> QueryCase:
        departure_index = self.random.randrange(len(self.AIRPORTS))
        departure = self.AIRPORTS[departure_index]
        arrival = self.AIRPORTS[(departure_index + 3) % len(self.AIRPORTS)]
        day = self._random_date().isoformat()
        return QueryCase(
            "route_by_date",
            "SELECT flight_id, flight_no, scheduled_departure, status "
            "FROM aviation.flights "
            f"WHERE departure_airport = '{departure}' "
            f"AND arrival_airport = '{arrival}' "
            f"AND scheduled_departure >= DATE '{day}' "
            f"AND scheduled_departure < DATE '{day}' + INTERVAL '1 day'",
        )

    def _passengers_for_booking(self) -> QueryCase:
        book_ref = f"{self.random.randint(1, self.BOOKING_COUNT):06d}"
        return QueryCase(
            "passengers_for_booking",
            "SELECT b.book_ref, b.book_date, t.ticket_no, t.passenger_name "
            "FROM aviation.bookings AS b "
            "JOIN aviation.tickets AS t ON t.book_ref = b.book_ref "
            f"WHERE b.book_ref = '{book_ref}'",
        )

    def _flight_revenue(self) -> QueryCase:
        flight_id = self.random.randint(1, self.FLIGHT_COUNT)
        return QueryCase(
            "flight_revenue",
            "SELECT f.flight_id, f.flight_no, COUNT(*) AS ticket_count, "
            "SUM(tf.amount) AS revenue "
            "FROM aviation.flights AS f "
            "JOIN aviation.ticket_flights AS tf ON tf.flight_id = f.flight_id "
            f"WHERE f.flight_id = {flight_id} "
            "GROUP BY f.flight_id, f.flight_no",
        )

    def _bookings_with_expensive_ticket(self) -> QueryCase:
        amount = self.random.randrange(40_000, 120_001, 250)
        return QueryCase(
            "bookings_with_expensive_ticket",
            "SELECT b.book_ref, b.total_amount "
            "FROM aviation.bookings AS b "
            "WHERE EXISTS ("
            "SELECT 1 FROM aviation.tickets AS t "
            "JOIN aviation.ticket_flights AS tf ON tf.ticket_no = t.ticket_no "
            "WHERE t.book_ref = b.book_ref "
            f"AND tf.amount >= {amount})",
        )

    def _recent_flights_sorted(self) -> QueryCase:
        airport = self.random.choice(self.AIRPORTS)
        limit = self.random.choice((10, 25, 50, 100))
        return QueryCase(
            "recent_flights_sorted",
            "SELECT flight_no, departure_airport, arrival_airport, scheduled_departure "
            "FROM aviation.flights "
            f"WHERE departure_airport = '{airport}' "
            f"ORDER BY scheduled_departure DESC LIMIT {limit}",
        )

    def _fare_statistics(self) -> QueryCase:
        fare = self.random.choice(self.FARES)
        minimum_amount = self.random.randrange(3_000, 80_001, 250)
        return QueryCase(
            "fare_statistics",
            "SELECT fare_conditions, COUNT(*) AS ticket_count, AVG(amount) AS average_amount "
            "FROM aviation.ticket_flights "
            f"WHERE fare_conditions = '{fare}' AND amount >= {minimum_amount} "
            "GROUP BY fare_conditions HAVING COUNT(*) > 10",
        )

    def _passenger_flight_history(self) -> QueryCase:
        passenger_number = self.random.randint(1, self.TICKET_COUNT)
        passenger_id = (
            f"{passenger_number % 9999:04d}-{passenger_number % 999999:06d}"
        )
        status = self.random.choice(self.STATUSES)
        return QueryCase(
            "passenger_flight_history",
            "SELECT t.passenger_id, t.passenger_name, f.flight_no, "
            "f.scheduled_departure, tf.fare_conditions "
            "FROM aviation.tickets AS t "
            "JOIN aviation.ticket_flights AS tf ON tf.ticket_no = t.ticket_no "
            "JOIN aviation.flights AS f ON f.flight_id = tf.flight_id "
            f"WHERE t.passenger_id = '{passenger_id}' AND f.status = '{status}' "
            "ORDER BY f.scheduled_departure DESC",
        )

    def _airport_departure_counts(self) -> QueryCase:
        day = self._random_date()
        end_day = day + timedelta(days=self.random.choice((7, 14, 30, 60)))
        return QueryCase(
            "airport_departure_counts",
            "SELECT departure_airport, COUNT(*) AS flight_count "
            "FROM aviation.flights "
            f"WHERE scheduled_departure >= DATE '{day.isoformat()}' "
            f"AND scheduled_departure < DATE '{end_day.isoformat()}' "
            "GROUP BY departure_airport ORDER BY flight_count DESC",
        )

    def _aircraft_flight_counts(self) -> QueryCase:
        minimum_range = self.random.randrange(3_000, 7_001, 250)
        return QueryCase(
            "aircraft_flight_counts",
            "SELECT a.aircraft_code, a.model, COUNT(f.flight_id) AS flight_count "
            "FROM aviation.aircrafts AS a "
            "LEFT JOIN aviation.flights AS f ON f.aircraft_code = a.aircraft_code "
            f"WHERE a.range_km >= {minimum_range} "
            "GROUP BY a.aircraft_code, a.model",
        )

    def _aircraft_seat_capacity(self) -> QueryCase:
        fare = self.random.choice(self.FARES)
        return QueryCase(
            "aircraft_seat_capacity",
            "SELECT a.aircraft_code, a.model, COUNT(s.seat_no) AS seat_count "
            "FROM aviation.aircrafts AS a "
            "JOIN aviation.seats AS s ON s.aircraft_code = a.aircraft_code "
            f"WHERE s.fare_conditions = '{fare}' "
            "GROUP BY a.aircraft_code, a.model ORDER BY seat_count DESC",
        )

    def _ticket_name_prefix(self) -> QueryCase:
        prefix = self.random.randint(1, 99_999)
        return QueryCase(
            "ticket_name_prefix",
            "SELECT ticket_no, passenger_id, passenger_name "
            "FROM aviation.tickets "
            f"WHERE passenger_name LIKE 'Passenger {prefix}%' "
            "ORDER BY ticket_no LIMIT 50",
        )

    def _boarding_pass_lookup(self) -> QueryCase:
        flight_id = self.random.randint(1, self.FLIGHT_COUNT)
        lower = self.random.randint(1, 5)
        upper = lower + self.random.randint(2, 5)
        return QueryCase(
            "boarding_pass_lookup",
            "SELECT bp.boarding_no, bp.seat_no, t.passenger_name "
            "FROM aviation.boarding_passes AS bp "
            "JOIN aviation.tickets AS t ON t.ticket_no = bp.ticket_no "
            f"WHERE bp.flight_id = {flight_id} "
            f"AND bp.boarding_no BETWEEN {lower} AND {upper}",
        )

    def _route_revenue(self) -> QueryCase:
        airport = self.random.choice(self.AIRPORTS)
        return QueryCase(
            "route_revenue",
            "SELECT f.departure_airport, f.arrival_airport, SUM(tf.amount) AS revenue "
            "FROM aviation.flights AS f "
            "JOIN aviation.ticket_flights AS tf ON tf.flight_id = f.flight_id "
            f"WHERE f.departure_airport = '{airport}' "
            "GROUP BY f.departure_airport, f.arrival_airport "
            "ORDER BY revenue DESC",
        )

    def _unboarded_tickets(self) -> QueryCase:
        flight_id = self.random.randint(1, self.FLIGHT_COUNT)
        return QueryCase(
            "unboarded_tickets",
            "SELECT tf.ticket_no, tf.fare_conditions "
            "FROM aviation.ticket_flights AS tf "
            "LEFT JOIN aviation.boarding_passes AS bp "
            "ON bp.ticket_no = tf.ticket_no AND bp.flight_id = tf.flight_id "
            f"WHERE tf.flight_id = {flight_id} AND bp.ticket_no IS NULL",
        )

    def _high_value_passengers(self) -> QueryCase:
        amount = self.random.randrange(100_000, 450_001, 2_500)
        return QueryCase(
            "high_value_passengers",
            "SELECT t.passenger_id, t.passenger_name, b.total_amount "
            "FROM aviation.tickets AS t "
            "JOIN aviation.bookings AS b ON b.book_ref = t.book_ref "
            f"WHERE b.total_amount >= {amount} "
            "ORDER BY b.total_amount DESC LIMIT 100",
        )

    def _flight_occupancy(self) -> QueryCase:
        flight_id = self.random.randint(1, self.FLIGHT_COUNT)
        return QueryCase(
            "flight_occupancy",
            "SELECT f.flight_id, COUNT(DISTINCT tf.ticket_no) AS tickets, "
            "COUNT(DISTINCT bp.ticket_no) AS boarded "
            "FROM aviation.flights AS f "
            "LEFT JOIN aviation.ticket_flights AS tf ON tf.flight_id = f.flight_id "
            "LEFT JOIN aviation.boarding_passes AS bp "
            "ON bp.flight_id = tf.flight_id AND bp.ticket_no = tf.ticket_no "
            f"WHERE f.flight_id = {flight_id} GROUP BY f.flight_id",
        )

    def _airport_connections(self) -> QueryCase:
        airport = self.random.choice(self.AIRPORTS)
        status = self.random.choice(self.STATUSES)
        return QueryCase(
            "airport_connections",
            "SELECT dep.city AS departure_city, arr.city AS arrival_city, COUNT(*) AS flights "
            "FROM aviation.flights AS f "
            "JOIN aviation.airports AS dep ON dep.airport_code = f.departure_airport "
            "JOIN aviation.airports AS arr ON arr.airport_code = f.arrival_airport "
            f"WHERE f.departure_airport = '{airport}' AND f.status = '{status}' "
            "GROUP BY dep.city, arr.city",
        )

    def _booking_ticket_count(self) -> QueryCase:
        day = date(2024, 10, 1) + timedelta(days=self.random.randrange(300))
        end_day = day + timedelta(days=self.random.choice((7, 14, 30)))
        return QueryCase(
            "booking_ticket_count",
            "SELECT b.book_ref, COUNT(t.ticket_no) AS ticket_count "
            "FROM aviation.bookings AS b "
            "JOIN aviation.tickets AS t ON t.book_ref = b.book_ref "
            f"WHERE b.book_date >= DATE '{day.isoformat()}' "
            f"AND b.book_date < DATE '{end_day.isoformat()}' "
            "GROUP BY b.book_ref HAVING COUNT(t.ticket_no) >= 2",
        )

    def _flights_without_tickets(self) -> QueryCase:
        day = self._random_date()
        return QueryCase(
            "flights_without_tickets",
            "SELECT f.flight_id, f.flight_no "
            "FROM aviation.flights AS f "
            f"WHERE f.scheduled_departure >= DATE '{day.isoformat()}' "
            "AND NOT EXISTS (SELECT 1 FROM aviation.ticket_flights AS tf "
            "WHERE tf.flight_id = f.flight_id)",
        )

    def _top_routes(self) -> QueryCase:
        day = self._random_date()
        end_day = day + timedelta(days=self.random.choice((30, 60, 90)))
        limit = self.random.choice((3, 5, 10))
        return QueryCase(
            "top_routes",
            "SELECT departure_airport, arrival_airport, COUNT(*) AS flight_count "
            "FROM aviation.flights "
            f"WHERE scheduled_departure >= DATE '{day.isoformat()}' "
            f"AND scheduled_departure < DATE '{end_day.isoformat()}' "
            "GROUP BY departure_airport, arrival_airport "
            f"ORDER BY flight_count DESC LIMIT {limit}",
        )

    def _daily_flight_count(self) -> QueryCase:
        airport = self.random.choice(self.AIRPORTS)
        return QueryCase(
            "daily_flight_count",
            "SELECT scheduled_departure::date AS flight_date, COUNT(*) AS flight_count "
            "FROM aviation.flights "
            f"WHERE departure_airport = '{airport}' "
            "GROUP BY scheduled_departure::date ORDER BY flight_date",
        )

    def _aircraft_fare_capacity(self) -> QueryCase:
        aircraft = self.random.choice(("SU9", "320", "321", "738", "E95"))
        return QueryCase(
            "aircraft_fare_capacity",
            "SELECT fare_conditions, COUNT(*) AS seat_count "
            "FROM aviation.seats "
            f"WHERE aircraft_code = '{aircraft}' "
            "GROUP BY fare_conditions ORDER BY seat_count DESC",
        )

    def _ticket_fare_lookup(self) -> QueryCase:
        passenger_number = self.random.randint(1, self.TICKET_COUNT)
        passenger_id = (
            f"{passenger_number % 9999:04d}-{passenger_number % 999999:06d}"
        )
        fare = self.random.choice(self.FARES)
        return QueryCase(
            "ticket_fare_lookup",
            "SELECT t.ticket_no, tf.flight_id, tf.amount "
            "FROM aviation.tickets AS t "
            "JOIN aviation.ticket_flights AS tf ON tf.ticket_no = t.ticket_no "
            f"WHERE t.passenger_id = '{passenger_id}' "
            f"AND tf.fare_conditions = '{fare}'",
        )

    def _monthly_route_rank(self) -> QueryCase:
        month = self.random.randint(1, 12)
        return QueryCase(
            "monthly_route_rank",
            "WITH route_month AS ("
            "SELECT date_trunc('month', f.scheduled_departure) AS month_start, "
            "f.departure_airport, f.arrival_airport, COUNT(*) AS sold, "
            "SUM(tf.amount) AS revenue "
            "FROM aviation.flights AS f "
            "JOIN aviation.ticket_flights AS tf ON tf.flight_id = f.flight_id "
            f"WHERE EXTRACT(MONTH FROM f.scheduled_departure) = {month} "
            "GROUP BY month_start, f.departure_airport, f.arrival_airport) "
            "SELECT month_start, departure_airport, arrival_airport, sold, revenue, "
            "DENSE_RANK() OVER (PARTITION BY month_start ORDER BY revenue DESC) AS route_rank "
            "FROM route_month ORDER BY month_start, route_rank LIMIT 100",
        )

    def _passenger_spending_rank(self) -> QueryCase:
        minimum = self.random.randrange(10_000, 150_001, 5_000)
        first_ticket = self.random.randint(1, self.TICKET_COUNT - 50_000)
        last_ticket = first_ticket + self.random.choice((10_000, 25_000, 50_000))
        return QueryCase(
            "passenger_spending_rank",
            "WITH passenger_totals AS ("
            "SELECT t.passenger_id, t.passenger_name, COUNT(*) AS segments, "
            "SUM(tf.amount) AS total_spent "
            "FROM aviation.tickets AS t "
            "JOIN aviation.ticket_flights AS tf ON tf.ticket_no = t.ticket_no "
            f"WHERE t.ticket_no BETWEEN '{first_ticket:013d}' AND '{last_ticket:013d}' "
            "GROUP BY t.passenger_id, t.passenger_name) "
            "SELECT passenger_id, passenger_name, segments, total_spent, "
            "ROW_NUMBER() OVER (ORDER BY total_spent DESC) AS spending_rank "
            f"FROM passenger_totals WHERE total_spent >= {minimum} "
            "ORDER BY spending_rank LIMIT 100",
        )

    def _booking_fare_breakdown(self) -> QueryCase:
        day = date(2024, 10, 1) + timedelta(days=self.random.randrange(300))
        return QueryCase(
            "booking_fare_breakdown",
            "SELECT b.book_ref, b.total_amount, COUNT(tf.flight_id) AS segments, "
            "SUM(tf.amount) FILTER (WHERE tf.fare_conditions = 'Economy') AS economy, "
            "SUM(tf.amount) FILTER (WHERE tf.fare_conditions = 'Business') AS business "
            "FROM aviation.bookings AS b "
            "JOIN aviation.tickets AS t ON t.book_ref = b.book_ref "
            "JOIN aviation.ticket_flights AS tf ON tf.ticket_no = t.ticket_no "
            f"WHERE b.book_date >= DATE '{day.isoformat()}' "
            f"AND b.book_date < DATE '{(day + timedelta(days=30)).isoformat()}' "
            "GROUP BY b.book_ref, b.total_amount HAVING COUNT(tf.flight_id) >= 2",
        )

    def _aircraft_route_utilization(self) -> QueryCase:
        status = self.random.choice(self.STATUSES)
        day = self._random_date()
        end_day = day + timedelta(days=self.random.choice((14, 30, 60, 90)))
        return QueryCase(
            "aircraft_route_utilization",
            "SELECT f.aircraft_code, f.departure_airport, f.arrival_airport, "
            "COUNT(DISTINCT f.flight_id) AS flights, COUNT(tf.ticket_no) AS tickets, "
            "AVG(tf.amount) AS average_fare "
            "FROM aviation.flights AS f "
            "LEFT JOIN aviation.ticket_flights AS tf ON tf.flight_id = f.flight_id "
            f"WHERE f.status = '{status}' "
            f"AND f.scheduled_departure >= DATE '{day.isoformat()}' "
            f"AND f.scheduled_departure < DATE '{end_day.isoformat()}' "
            "GROUP BY f.aircraft_code, f.departure_airport, f.arrival_airport "
            "ORDER BY tickets DESC LIMIT 100",
        )

    def _fare_price_percentiles(self) -> QueryCase:
        fare = self.random.choice(self.FARES)
        return QueryCase(
            "fare_price_percentiles",
            "SELECT f.departure_airport, f.arrival_airport, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY tf.amount) AS median_fare, "
            "percentile_cont(0.9) WITHIN GROUP (ORDER BY tf.amount) AS p90_fare "
            "FROM aviation.ticket_flights AS tf "
            "JOIN aviation.flights AS f ON f.flight_id = tf.flight_id "
            f"WHERE tf.fare_conditions = '{fare}' "
            "GROUP BY f.departure_airport, f.arrival_airport",
        )

    def _connecting_itineraries(self) -> QueryCase:
        origin = self.random.choice(self.AIRPORTS)
        day = self._random_date()
        end_day = day + timedelta(days=self.random.choice((3, 7, 14)))
        return QueryCase(
            "connecting_itineraries",
            "SELECT f1.flight_id AS first_flight, f2.flight_id AS second_flight, "
            "f1.departure_airport, f1.arrival_airport AS connection_airport, "
            "f2.arrival_airport AS destination "
            "FROM aviation.flights AS f1 "
            "JOIN aviation.flights AS f2 ON f2.departure_airport = f1.arrival_airport "
            "AND f2.scheduled_departure BETWEEN f1.scheduled_arrival "
            "AND f1.scheduled_arrival + INTERVAL '12 hours' "
            f"WHERE f1.departure_airport = '{origin}' "
            f"AND f1.scheduled_departure >= DATE '{day.isoformat()}' "
            f"AND f1.scheduled_departure < DATE '{end_day.isoformat()}' "
            "AND f2.arrival_airport <> f1.departure_airport "
            "ORDER BY f1.scheduled_departure LIMIT 100",
        )

    def _unoccupied_seats(self) -> QueryCase:
        flight_id = self.random.randint(1, self.FLIGHT_COUNT)
        return QueryCase(
            "unoccupied_seats",
            "SELECT s.fare_conditions, COUNT(*) AS free_seats "
            "FROM aviation.flights AS f "
            "JOIN aviation.seats AS s ON s.aircraft_code = f.aircraft_code "
            "LEFT JOIN aviation.boarding_passes AS bp "
            "ON bp.flight_id = f.flight_id AND bp.seat_no = s.seat_no "
            f"WHERE f.flight_id = {flight_id} AND bp.ticket_no IS NULL "
            "GROUP BY s.fare_conditions",
        )

    def _booking_month_outliers(self) -> QueryCase:
        multiplier = self.random.choice((1.2, 1.5, 2.0))
        return QueryCase(
            "booking_month_outliers",
            "WITH monthly_average AS ("
            "SELECT date_trunc('month', book_date) AS month_start, "
            "AVG(total_amount) AS average_amount FROM aviation.bookings "
            "GROUP BY date_trunc('month', book_date)) "
            "SELECT b.book_ref, b.book_date, b.total_amount "
            "FROM aviation.bookings AS b JOIN monthly_average AS m "
            "ON m.month_start = date_trunc('month', b.book_date) "
            f"WHERE b.total_amount > m.average_amount * {multiplier} "
            "ORDER BY b.total_amount DESC LIMIT 100",
        )

    def _airport_status_pivot(self) -> QueryCase:
        airport = self.random.choice(self.AIRPORTS)
        return QueryCase(
            "airport_status_pivot",
            "SELECT departure_airport, "
            "COUNT(*) FILTER (WHERE status = 'Scheduled') AS scheduled, "
            "COUNT(*) FILTER (WHERE status = 'Departed') AS departed, "
            "COUNT(*) FILTER (WHERE status = 'Arrived') AS arrived, "
            "AVG(EXTRACT(EPOCH FROM (scheduled_arrival - scheduled_departure)) / 60) "
            "AS average_duration_minutes FROM aviation.flights "
            f"WHERE departure_airport = '{airport}' GROUP BY departure_airport",
        )

    def _latest_route_flights(self) -> QueryCase:
        status = self.random.choice(self.STATUSES)
        return QueryCase(
            "latest_route_flights",
            "SELECT DISTINCT ON (departure_airport, arrival_airport) "
            "departure_airport, arrival_airport, flight_no, scheduled_departure "
            "FROM aviation.flights "
            f"WHERE status = '{status}' "
            "ORDER BY departure_airport, arrival_airport, scheduled_departure DESC",
        )

    def _boarding_efficiency(self) -> QueryCase:
        day = self._random_date()
        return QueryCase(
            "boarding_efficiency",
            "WITH selected_flights AS (SELECT * FROM aviation.flights "
            f"WHERE scheduled_departure >= DATE '{day.isoformat()}' "
            f"AND scheduled_departure < DATE '{(day + timedelta(days=7)).isoformat()}'), "
            "sold AS (SELECT tf.flight_id, COUNT(*) AS sold_count "
            "FROM aviation.ticket_flights AS tf JOIN selected_flights AS sf "
            "ON sf.flight_id = tf.flight_id GROUP BY tf.flight_id), "
            "boarded AS (SELECT bp.flight_id, COUNT(*) AS boarded_count "
            "FROM aviation.boarding_passes AS bp JOIN selected_flights AS sf "
            "ON sf.flight_id = bp.flight_id GROUP BY bp.flight_id) "
            "SELECT f.flight_id, COALESCE(s.sold_count, 0) AS sold_count, "
            "COALESCE(b.boarded_count, 0) AS boarded_count, "
            "COALESCE(b.boarded_count, 0)::numeric / NULLIF(s.sold_count, 0) AS ratio "
            "FROM selected_flights AS f "
            "LEFT JOIN sold AS s ON s.flight_id = f.flight_id "
            "LEFT JOIN boarded AS b ON b.flight_id = f.flight_id "
            "",
        )

    def _routes_above_average_revenue(self) -> QueryCase:
        minimum = self.random.randrange(5_000, 50_001, 5_000)
        return QueryCase(
            "routes_above_average_revenue",
            "WITH route_revenue AS (SELECT f.departure_airport, f.arrival_airport, "
            "SUM(tf.amount) AS revenue FROM aviation.flights AS f "
            "JOIN aviation.ticket_flights AS tf ON tf.flight_id = f.flight_id "
            "GROUP BY f.departure_airport, f.arrival_airport) "
            "SELECT departure_airport, arrival_airport, revenue FROM route_revenue "
            "WHERE revenue > (SELECT AVG(revenue) FROM route_revenue) "
            f"AND revenue >= {minimum} ORDER BY revenue DESC",
        )

    def _airport_revenue_rollup(self) -> QueryCase:
        fare = self.random.choice(self.FARES)
        return QueryCase(
            "airport_revenue_rollup",
            "SELECT f.departure_airport, f.arrival_airport, SUM(tf.amount) AS revenue "
            "FROM aviation.flights AS f "
            "JOIN aviation.ticket_flights AS tf ON tf.flight_id = f.flight_id "
            f"WHERE tf.fare_conditions = '{fare}' "
            "GROUP BY ROLLUP (f.departure_airport, f.arrival_airport) "
            "ORDER BY f.departure_airport NULLS LAST, revenue DESC",
        )

    def _passenger_fare_transitions(self) -> QueryCase:
        first_fare = self.random.choice(self.FARES)
        first_ticket = self.random.randint(1, self.TICKET_COUNT - 25_000)
        last_ticket = first_ticket + self.random.choice((5_000, 10_000, 25_000))
        return QueryCase(
            "passenger_fare_transitions",
            "SELECT t.passenger_id, tf1.fare_conditions AS first_fare, "
            "tf2.fare_conditions AS next_fare, COUNT(*) AS transitions "
            "FROM aviation.tickets AS t "
            "JOIN aviation.ticket_flights AS tf1 ON tf1.ticket_no = t.ticket_no "
            "JOIN aviation.ticket_flights AS tf2 ON tf2.ticket_no = t.ticket_no "
            "AND tf2.flight_id > tf1.flight_id "
            f"WHERE tf1.fare_conditions = '{first_fare}' "
            f"AND t.ticket_no BETWEEN '{first_ticket:013d}' AND '{last_ticket:013d}' "
            "GROUP BY t.passenger_id, tf1.fare_conditions, tf2.fare_conditions "
            "HAVING COUNT(*) >= 1 ORDER BY transitions DESC LIMIT 100",
        )

    def _route_traffic_share(self) -> QueryCase:
        status = self.random.choice(self.STATUSES)
        return QueryCase(
            "route_traffic_share",
            "WITH route_counts AS (SELECT departure_airport, arrival_airport, "
            "COUNT(*) AS flight_count FROM aviation.flights "
            f"WHERE status = '{status}' "
            "GROUP BY departure_airport, arrival_airport) "
            "SELECT departure_airport, arrival_airport, flight_count, "
            "flight_count::numeric / SUM(flight_count) OVER "
            "(PARTITION BY departure_airport) AS traffic_share "
            "FROM route_counts ORDER BY traffic_share DESC",
        )

    def _aircraft_capacity_load(self) -> QueryCase:
        aircraft = self.random.choice(("SU9", "320", "321", "738", "E95"))
        return QueryCase(
            "aircraft_capacity_load",
            "WITH capacity AS (SELECT aircraft_code, COUNT(*) AS seats "
            "FROM aviation.seats GROUP BY aircraft_code), "
            "sold AS (SELECT flight_id, COUNT(*) AS tickets "
            "FROM aviation.ticket_flights GROUP BY flight_id) "
            "SELECT f.flight_id, f.aircraft_code, c.seats, COALESCE(s.tickets, 0) AS tickets, "
            "COALESCE(s.tickets, 0)::numeric / NULLIF(c.seats, 0) AS load_factor "
            "FROM aviation.flights AS f "
            "JOIN capacity AS c ON c.aircraft_code = f.aircraft_code "
            "LEFT JOIN sold AS s ON s.flight_id = f.flight_id "
            f"WHERE f.aircraft_code = '{aircraft}' "
            "ORDER BY load_factor DESC NULLS LAST LIMIT 100",
        )

    def _route_moving_average(self) -> QueryCase:
        airport = self.random.choice(self.AIRPORTS)
        window = self.random.choice((2, 4, 6, 8))
        return QueryCase(
            "route_moving_average",
            "WITH daily AS (SELECT scheduled_departure::date AS flight_date, "
            "arrival_airport, COUNT(*) AS flights FROM aviation.flights "
            f"WHERE departure_airport = '{airport}' "
            "GROUP BY scheduled_departure::date, arrival_airport) "
            "SELECT flight_date, arrival_airport, flights, "
            "AVG(flights) OVER (PARTITION BY arrival_airport ORDER BY flight_date "
            f"ROWS BETWEEN {window} PRECEDING AND CURRENT ROW) AS moving_average "
            "FROM daily ORDER BY arrival_airport, flight_date",
        )

    def _flight_gap_analysis(self) -> QueryCase:
        airport = self.random.choice(self.AIRPORTS)
        day = self._random_date()
        return QueryCase(
            "flight_gap_analysis",
            "WITH ordered AS (SELECT flight_id, arrival_airport, scheduled_departure, "
            "LAG(scheduled_departure) OVER (PARTITION BY arrival_airport "
            "ORDER BY scheduled_departure) AS previous_departure "
            "FROM aviation.flights "
            f"WHERE departure_airport = '{airport}') "
            "SELECT flight_id, arrival_airport, scheduled_departure, "
            "scheduled_departure - previous_departure AS departure_gap FROM ordered "
            f"WHERE scheduled_departure >= DATE '{day.isoformat()}' "
            "ORDER BY departure_gap DESC NULLS LAST LIMIT 100",
        )

    def _booking_segment_summary(self) -> QueryCase:
        day = date(2024, 10, 1) + timedelta(days=self.random.randrange(300))
        duration = self.random.choice((3, 7, 14, 21))
        return QueryCase(
            "booking_segment_summary",
            "SELECT b.book_ref, COUNT(DISTINCT t.ticket_no) AS passengers, "
            "COUNT(tf.flight_id) AS segments, SUM(tf.amount) AS segment_revenue "
            "FROM aviation.bookings AS b JOIN aviation.tickets AS t "
            "ON t.book_ref = b.book_ref JOIN aviation.ticket_flights AS tf "
            "ON tf.ticket_no = t.ticket_no "
            f"WHERE b.book_date >= DATE '{day.isoformat()}' "
            f"AND b.book_date < DATE '{(day + timedelta(days=duration)).isoformat()}' "
            "GROUP BY b.book_ref HAVING COUNT(tf.flight_id) >= 2 "
            "ORDER BY segment_revenue DESC LIMIT 100",
        )

    def _fare_deviation(self) -> QueryCase:
        airport = self.random.choice(self.AIRPORTS)
        factor = self.random.choice((1.05, 1.10, 1.20, 1.35))
        return QueryCase(
            "fare_deviation",
            "WITH route_fares AS (SELECT f.departure_airport, f.arrival_airport, "
            "AVG(tf.amount) AS average_fare FROM aviation.flights AS f "
            "JOIN aviation.ticket_flights AS tf ON tf.flight_id = f.flight_id "
            "GROUP BY f.departure_airport, f.arrival_airport) "
            "SELECT tf.ticket_no, f.flight_id, tf.amount, r.average_fare "
            "FROM aviation.ticket_flights AS tf JOIN aviation.flights AS f "
            "ON f.flight_id = tf.flight_id JOIN route_fares AS r "
            "ON r.departure_airport = f.departure_airport "
            "AND r.arrival_airport = f.arrival_airport "
            f"WHERE f.departure_airport = '{airport}' "
            f"AND tf.amount > r.average_fare * {factor} "
            "ORDER BY tf.amount DESC LIMIT 100",
        )

    def _airport_pair_union(self) -> QueryCase:
        airport = self.random.choice(self.AIRPORTS)
        day = self._random_date()
        return QueryCase(
            "airport_pair_union",
            "SELECT flight_id, arrival_airport AS paired_airport, 'departure' AS direction "
            "FROM aviation.flights "
            f"WHERE departure_airport = '{airport}' "
            f"AND scheduled_departure >= DATE '{day.isoformat()}' UNION ALL "
            "SELECT flight_id, departure_airport AS paired_airport, 'arrival' AS direction "
            "FROM aviation.flights "
            f"WHERE arrival_airport = '{airport}' "
            f"AND scheduled_arrival >= DATE '{day.isoformat()}' LIMIT 200",
        )

    def _no_show_revenue(self) -> QueryCase:
        airport = self.random.choice(self.AIRPORTS)
        fare = self.random.choice(self.FARES)
        return QueryCase(
            "no_show_revenue",
            "SELECT f.departure_airport, f.arrival_airport, COUNT(*) AS no_shows, "
            "SUM(tf.amount) AS retained_revenue FROM aviation.ticket_flights AS tf "
            "JOIN aviation.flights AS f ON f.flight_id = tf.flight_id "
            "LEFT JOIN aviation.boarding_passes AS bp ON bp.ticket_no = tf.ticket_no "
            "AND bp.flight_id = tf.flight_id "
            f"WHERE f.departure_airport = '{airport}' "
            f"AND tf.fare_conditions = '{fare}' AND bp.ticket_no IS NULL "
            "GROUP BY f.departure_airport, f.arrival_airport "
            "ORDER BY retained_revenue DESC",
        )

    def _aircraft_revenue_rank(self) -> QueryCase:
        status = self.random.choice(self.STATUSES)
        minimum = self.random.randrange(10_000, 100_001, 2_500)
        return QueryCase(
            "aircraft_revenue_rank",
            "WITH totals AS (SELECT f.aircraft_code, f.departure_airport, "
            "SUM(tf.amount) AS revenue FROM aviation.flights AS f "
            "JOIN aviation.ticket_flights AS tf ON tf.flight_id = f.flight_id "
            f"WHERE f.status = '{status}' GROUP BY f.aircraft_code, f.departure_airport) "
            "SELECT aircraft_code, departure_airport, revenue, DENSE_RANK() OVER "
            "(PARTITION BY aircraft_code ORDER BY revenue DESC) AS revenue_rank "
            f"FROM totals WHERE revenue >= {minimum} ORDER BY aircraft_code, revenue_rank",
        )

    def _route_revenue_grouping_sets(self) -> QueryCase:
        day = self._random_date()
        end_day = day + timedelta(days=self.random.choice((14, 30, 60)))
        return QueryCase(
            "route_revenue_grouping_sets",
            "SELECT f.departure_airport, f.arrival_airport, tf.fare_conditions, "
            "COUNT(*) AS tickets, SUM(tf.amount) AS revenue "
            "FROM aviation.flights AS f JOIN aviation.ticket_flights AS tf "
            "ON tf.flight_id = f.flight_id "
            f"WHERE f.scheduled_departure >= DATE '{day.isoformat()}' "
            f"AND f.scheduled_departure < DATE '{end_day.isoformat()}' "
            "GROUP BY GROUPING SETS ((f.departure_airport, f.arrival_airport, "
            "tf.fare_conditions), (f.departure_airport, f.arrival_airport), ()) "
            "ORDER BY revenue DESC NULLS LAST",
        )

    def _passenger_latest_segment(self) -> QueryCase:
        prefix = self.random.randint(0, 9999)
        return QueryCase(
            "passenger_latest_segment",
            "SELECT t.passenger_id, t.passenger_name, latest.flight_id, latest.amount "
            "FROM aviation.tickets AS t CROSS JOIN LATERAL ("
            "SELECT tf.flight_id, tf.amount FROM aviation.ticket_flights AS tf "
            "WHERE tf.ticket_no = t.ticket_no ORDER BY tf.flight_id DESC LIMIT 1"
            ") AS latest "
            f"WHERE t.passenger_id LIKE '{prefix:04d}-%' "
            "ORDER BY latest.amount DESC LIMIT 100",
        )

    def _flight_capacity_shortfall(self) -> QueryCase:
        day = self._random_date()
        ratio = self.random.choice((0.50, 0.65, 0.75, 0.85))
        return QueryCase(
            "flight_capacity_shortfall",
            "WITH capacity AS (SELECT aircraft_code, COUNT(*) AS seats "
            "FROM aviation.seats GROUP BY aircraft_code), boarded AS ("
            "SELECT flight_id, COUNT(*) AS boarded FROM aviation.boarding_passes "
            "GROUP BY flight_id) SELECT f.flight_id, c.seats, COALESCE(b.boarded, 0) "
            "AS boarded FROM aviation.flights AS f JOIN capacity AS c "
            "ON c.aircraft_code = f.aircraft_code LEFT JOIN boarded AS b "
            "ON b.flight_id = f.flight_id "
            f"WHERE f.scheduled_departure >= DATE '{day.isoformat()}' "
            f"AND COALESCE(b.boarded, 0) < c.seats * {ratio} "
            "ORDER BY boarded LIMIT 100",
        )

    def _booking_value_buckets(self) -> QueryCase:
        day = date(2024, 10, 1) + timedelta(days=self.random.randrange(300))
        buckets = self.random.choice((5, 8, 10, 12))
        return QueryCase(
            "booking_value_buckets",
            f"SELECT width_bucket(total_amount, 0, 500000, {buckets}) AS amount_bucket, "
            "COUNT(*) AS bookings, AVG(total_amount) AS average_amount "
            "FROM aviation.bookings "
            f"WHERE book_date >= DATE '{day.isoformat()}' "
            "GROUP BY amount_bucket ORDER BY amount_bucket",
        )

    def _route_peak_comparison(self) -> QueryCase:
        airport = self.random.choice(self.AIRPORTS)
        minimum = self.random.randrange(5, 31)
        return QueryCase(
            "route_peak_comparison",
            "WITH route_daily AS (SELECT scheduled_departure::date AS flight_date, "
            "departure_airport, arrival_airport, COUNT(*) AS flights "
            "FROM aviation.flights GROUP BY scheduled_departure::date, "
            "departure_airport, arrival_airport), route_average AS ("
            "SELECT departure_airport, arrival_airport, AVG(flights) AS average_flights "
            "FROM route_daily GROUP BY departure_airport, arrival_airport) "
            "SELECT d.flight_date, d.arrival_airport, d.flights, a.average_flights "
            "FROM route_daily AS d JOIN route_average AS a USING "
            "(departure_airport, arrival_airport) "
            f"WHERE d.departure_airport = '{airport}' AND d.flights >= {minimum} "
            "AND d.flights > a.average_flights ORDER BY d.flights DESC",
        )
