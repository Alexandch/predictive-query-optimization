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
        book_ref = f"{self.random.randint(1, 50_000):06d}"
        return QueryCase(
            "passengers_for_booking",
            "SELECT b.book_ref, b.book_date, t.ticket_no, t.passenger_name "
            "FROM aviation.bookings AS b "
            "JOIN aviation.tickets AS t ON t.book_ref = b.book_ref "
            f"WHERE b.book_ref = '{book_ref}'",
        )

    def _flight_revenue(self) -> QueryCase:
        flight_id = self.random.randint(1, 10_000)
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
        passenger_number = self.random.randint(1, 100_000)
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
        flight_id = self.random.randint(1, 10_000)
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
        flight_id = self.random.randint(1, 10_000)
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
        flight_id = self.random.randint(1, 10_000)
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
        passenger_number = self.random.randint(1, 100_000)
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
