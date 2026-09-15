"""Reproducible development workload for optimization-strategy learning."""

from __future__ import annotations

from .pagila_queries import PagilaQueryGenerator
from .query_case import QueryCase
from .query_generator import AviationQueryGenerator
from .retail_queries import RetailQueryGenerator


def generate_strategy_workload(
    *,
    ordinary_per_domain: int = 6,
    rewrite_variants: int = 3,
    seed: int = 42,
) -> list[QueryCase]:
    """Mix ordinary queries with explicit, production-relevant rewrite patterns."""
    if ordinary_per_domain <= 0 or rewrite_variants <= 0:
        raise ValueError("Workload sizes must be positive")
    ordinary = [
        *AviationQueryGenerator(seed).generate(ordinary_per_domain),
        *RetailQueryGenerator(seed + 1).generate(ordinary_per_domain),
        *PagilaQueryGenerator(seed + 2).generate(ordinary_per_domain),
    ]
    rewrites: list[QueryCase] = []
    for offset in range(rewrite_variants):
        customer_limit = 40 + offset * 30
        product_limit = 20 + offset * 15
        booking_limit = 20 + offset * 15
        film_limit = 200 + offset * 150
        order_limit = 10_000 + offset * 10_000
        ticket_limit = 50_000 + offset * 25_000
        inventory_limit = 2_000 + offset * 1_000
        rewrites.extend(
            (
                QueryCase(
                    "strategy_retail_customer_has_orders",
                    "SELECT c.customer_id FROM retail.customers AS c "
                    f"WHERE c.customer_id <= {customer_limit} AND "
                    "(SELECT COUNT(*) FROM retail.customer_orders AS o "
                    "WHERE o.customer_id = c.customer_id) > 0",
                ),
                QueryCase(
                    "strategy_retail_product_has_sales",
                    "SELECT p.product_id FROM retail.products AS p "
                    f"WHERE p.product_id <= {product_limit} AND "
                    "(SELECT COUNT(*) FROM retail.order_items AS i "
                    "WHERE i.product_id = p.product_id) > 0",
                ),
                QueryCase(
                    "strategy_aviation_booking_has_tickets",
                    "SELECT b.book_ref FROM aviation.bookings AS b "
                    f"WHERE b.book_ref <= '{booking_limit:06d}' AND "
                    "(SELECT COUNT(*) FROM aviation.tickets AS t "
                    "WHERE t.book_ref = b.book_ref) > 0",
                ),
                QueryCase(
                    "strategy_pagila_film_has_inventory",
                    "SELECT f.film_id FROM pagila.film AS f "
                    f"WHERE f.film_id <= {film_limit} AND "
                    "(SELECT COUNT(*) FROM pagila.inventory AS i "
                    "WHERE i.film_id = f.film_id) > 0",
                ),
                QueryCase(
                    "strategy_retail_order_has_items",
                    "SELECT o.order_id FROM retail.customer_orders AS o "
                    f"WHERE o.order_id <= {order_limit} AND "
                    "(SELECT COUNT(*) FROM retail.order_items AS i "
                    "WHERE i.order_id = o.order_id) > 0",
                ),
                QueryCase(
                    "strategy_aviation_ticket_has_flights",
                    "SELECT t.ticket_no FROM aviation.tickets AS t "
                    f"WHERE t.ticket_no <= '{ticket_limit:013d}' AND "
                    "(SELECT COUNT(*) FROM aviation.ticket_flights AS tf "
                    "WHERE tf.ticket_no = t.ticket_no) > 0",
                ),
                QueryCase(
                    "strategy_pagila_inventory_has_rentals",
                    "SELECT i.inventory_id FROM pagila.inventory AS i "
                    f"WHERE i.inventory_id <= {inventory_limit} AND "
                    "(SELECT COUNT(*) FROM pagila.rental AS r "
                    "WHERE r.inventory_id = i.inventory_id) > 0",
                ),
            )
        )
    return [*ordinary, *rewrites]
