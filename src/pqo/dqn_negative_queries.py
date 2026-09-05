"""Training-only workload biased toward index actions that should not help."""

from __future__ import annotations

import random
from typing import Callable

from .query_case import QueryCase


class DQNNegativeQueryGenerator:
    """Generate low-selectivity and non-sargable queries outside all controls."""

    TEMPLATE_IDS = (
        "negative_aviation_flight_status_majority",
        "negative_aviation_departure_month_expression",
        "negative_aviation_passenger_normalized_prefix",
        "negative_aviation_amount_arithmetic",
        "negative_aviation_booking_bucket",
        "negative_aviation_seat_fare_exclusion",
        "negative_aviation_boarding_modulo",
        "negative_aviation_flight_duration_expression",
        "negative_retail_customer_normalized_prefix",
        "negative_retail_order_status_majority",
        "negative_retail_order_month_expression",
        "negative_retail_product_price_arithmetic",
        "negative_retail_inventory_available_expression",
        "negative_retail_payment_date_expression",
        "negative_retail_item_value_expression",
        "negative_retail_shipment_duration_expression",
    )

    def __init__(self, seed: int = 12001) -> None:
        self.random = random.Random(seed)
        self._templates: tuple[Callable[[], QueryCase], ...] = (
            self._aviation_flight_status_majority,
            self._aviation_departure_month_expression,
            self._aviation_passenger_normalized_prefix,
            self._aviation_amount_arithmetic,
            self._aviation_booking_bucket,
            self._aviation_seat_fare_exclusion,
            self._aviation_boarding_modulo,
            self._aviation_flight_duration_expression,
            self._retail_customer_normalized_prefix,
            self._retail_order_status_majority,
            self._retail_order_month_expression,
            self._retail_product_price_arithmetic,
            self._retail_inventory_available_expression,
            self._retail_payment_date_expression,
            self._retail_item_value_expression,
            self._retail_shipment_duration_expression,
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

    @staticmethod
    def _case(template_id: str, sql_text: str) -> QueryCase:
        return QueryCase(template_id, " ".join(sql_text.split()))

    def _aviation_flight_status_majority(self) -> QueryCase:
        excluded = self.random.choice(("Scheduled", "On Time", "Departed", "Arrived"))
        return self._case("negative_aviation_flight_status_majority", f"""
            SELECT status, COUNT(*) AS flights, AVG(EXTRACT(EPOCH FROM
                   (scheduled_arrival - scheduled_departure))) AS avg_duration_seconds
            FROM aviation.flights WHERE status <> '{excluded}' GROUP BY status
        """)

    def _aviation_departure_month_expression(self) -> QueryCase:
        lower = self.random.randint(1, 4)
        upper = self.random.randint(9, 12)
        return self._case("negative_aviation_departure_month_expression", f"""
            SELECT COUNT(*) AS flights, COUNT(DISTINCT departure_airport) AS origins
            FROM aviation.flights
            WHERE EXTRACT(MONTH FROM scheduled_departure) BETWEEN {lower} AND {upper}
        """)

    def _aviation_passenger_normalized_prefix(self) -> QueryCase:
        suffix = self.random.choice(("%", "1%", "2%", "3%", "4%"))
        return self._case("negative_aviation_passenger_normalized_prefix", f"""
            SELECT COUNT(*) AS passengers, COUNT(DISTINCT book_ref) AS bookings
            FROM aviation.tickets
            WHERE LOWER(TRIM(passenger_name)) LIKE 'passenger {suffix}'
        """)

    def _aviation_amount_arithmetic(self) -> QueryCase:
        multiplier = self.random.choice((1.05, 1.10, 1.15, 1.20))
        threshold = self.random.randrange(1000, 8001, 500)
        return self._case("negative_aviation_amount_arithmetic", f"""
            SELECT fare_conditions, COUNT(*) AS segments, SUM(amount) AS revenue
            FROM aviation.ticket_flights
            WHERE amount * {multiplier:.2f} >= {threshold}
            GROUP BY fare_conditions
        """)

    def _aviation_booking_bucket(self) -> QueryCase:
        divisor = self.random.choice((5000, 10000, 20000))
        bucket = self.random.randint(0, 3)
        return self._case("negative_aviation_booking_bucket", f"""
            SELECT FLOOR(total_amount / {divisor}) AS amount_bucket, COUNT(*) AS bookings
            FROM aviation.bookings
            WHERE FLOOR(total_amount / {divisor}) >= {bucket}
            GROUP BY amount_bucket
        """)

    def _aviation_seat_fare_exclusion(self) -> QueryCase:
        excluded = self.random.choice(("Economy", "Comfort", "Business"))
        return self._case("negative_aviation_seat_fare_exclusion", f"""
            SELECT aircraft_code, COUNT(*) AS seats
            FROM aviation.seats WHERE fare_conditions <> '{excluded}'
            GROUP BY aircraft_code
        """)

    def _aviation_boarding_modulo(self) -> QueryCase:
        modulus = self.random.choice((2, 3, 4, 5, 7))
        return self._case("negative_aviation_boarding_modulo", f"""
            SELECT COUNT(*) AS boarding_passes, COUNT(DISTINCT flight_id) AS flights
            FROM aviation.boarding_passes WHERE boarding_no % {modulus} <> 0
        """)

    def _aviation_flight_duration_expression(self) -> QueryCase:
        hours = self.random.choice((1, 2, 3, 4))
        return self._case("negative_aviation_flight_duration_expression", f"""
            SELECT status, COUNT(*) AS flights
            FROM aviation.flights
            WHERE scheduled_arrival - scheduled_departure >= INTERVAL '{hours} hours'
            GROUP BY status
        """)

    def _retail_customer_normalized_prefix(self) -> QueryCase:
        suffix = self.random.choice(("%", "1%", "2%", "3%", "4%"))
        return self._case("negative_retail_customer_normalized_prefix", f"""
            SELECT segment, COUNT(*) AS customers
            FROM retail.customers
            WHERE LOWER(TRIM(full_name)) LIKE 'customer {suffix}'
            GROUP BY segment
        """)

    def _retail_order_status_majority(self) -> QueryCase:
        excluded = self.random.choice(
            ("pending", "paid", "processing", "shipped", "delivered", "cancelled")
        )
        return self._case("negative_retail_order_status_majority", f"""
            SELECT sales_channel, COUNT(*) AS orders, SUM(total_amount) AS revenue
            FROM retail.customer_orders WHERE order_status <> '{excluded}'
            GROUP BY sales_channel
        """)

    def _retail_order_month_expression(self) -> QueryCase:
        lower = self.random.randint(1, 4)
        upper = self.random.randint(9, 12)
        return self._case("negative_retail_order_month_expression", f"""
            SELECT order_status, COUNT(*) AS orders
            FROM retail.customer_orders
            WHERE EXTRACT(MONTH FROM ordered_at) BETWEEN {lower} AND {upper}
            GROUP BY order_status
        """)

    def _retail_product_price_arithmetic(self) -> QueryCase:
        multiplier = self.random.choice((1.05, 1.10, 1.15, 1.20))
        threshold = self.random.randrange(10, 101, 10)
        return self._case("negative_retail_product_price_arithmetic", f"""
            SELECT brand, COUNT(*) AS products, AVG(price) AS avg_price
            FROM retail.products WHERE price * {multiplier:.2f} >= {threshold}
            GROUP BY brand
        """)

    def _retail_inventory_available_expression(self) -> QueryCase:
        threshold = self.random.choice((0, 2, 5, 10, 20))
        return self._case("negative_retail_inventory_available_expression", f"""
            SELECT warehouse_id, COUNT(*) AS products, SUM(quantity) AS stock
            FROM retail.inventory
            WHERE quantity - reserved_quantity >= {threshold}
            GROUP BY warehouse_id
        """)

    def _retail_payment_date_expression(self) -> QueryCase:
        days = self.random.choice((7, 14, 30, 60, 90))
        return self._case("negative_retail_payment_date_expression", f"""
            SELECT payment_status, COUNT(*) AS payments, SUM(amount) AS total_amount
            FROM retail.payments
            WHERE COALESCE(paid_at, TIMESTAMPTZ '2023-01-01')
                  >= TIMESTAMPTZ '2023-01-01' + INTERVAL '{days} days'
            GROUP BY payment_status
        """)

    def _retail_item_value_expression(self) -> QueryCase:
        threshold = self.random.randrange(50, 501, 25)
        return self._case("negative_retail_item_value_expression", f"""
            SELECT COUNT(*) AS lines, SUM(quantity * unit_price) AS value
            FROM retail.order_items WHERE quantity * unit_price >= {threshold}
        """)

    def _retail_shipment_duration_expression(self) -> QueryCase:
        days = self.random.choice((1, 2, 3, 5, 7))
        return self._case("negative_retail_shipment_duration_expression", f"""
            SELECT shipment_status, COUNT(*) AS shipments
            FROM retail.shipments
            WHERE COALESCE(delivered_at, shipped_at) - shipped_at >= INTERVAL '{days} days'
            GROUP BY shipment_status
        """)
