"""Development-only production-like workload for the Pagila sample database."""

from __future__ import annotations

import random
from typing import Callable

from .query_case import QueryCase


class PagilaQueryGenerator:
    """Generate balanced analytical and operational queries for Pagila."""

    TEMPLATE_IDS = (
        "pagila_category_revenue",
        "pagila_customer_lifetime_value",
        "pagila_store_monthly_revenue",
        "pagila_film_popularity",
        "pagila_actor_rental_impact",
        "pagila_overdue_rentals",
        "pagila_customer_recency_rank",
        "pagila_inventory_availability",
        "pagila_country_revenue",
        "pagila_replacement_exposure",
        "pagila_payment_moving_average",
        "pagila_category_percentiles",
        "pagila_staff_performance",
        "pagila_rental_gaps",
        "pagila_inactive_customers",
        "pagila_film_pair_affinity",
        "pagila_customer_cohorts",
        "pagila_store_inventory_turnover",
        "pagila_actor_category_diversity",
        "pagila_rating_revenue_pivot",
        "pagila_language_catalog",
        "pagila_city_customer_density",
        "pagila_duration_outliers",
        "pagila_repeat_renters",
        "pagila_daily_revenue_rollup",
        "pagila_return_sla",
        "pagila_top_films_per_category",
        "pagila_customer_payment_transitions",
        "pagila_unrented_films",
        "pagila_inventory_imbalance",
    )

    def __init__(self, seed: int = 18101) -> None:
        self.random = random.Random(seed)
        self._templates: tuple[Callable[[], QueryCase], ...] = tuple(
            getattr(self, f"_{template_id.removeprefix('pagila_')}")
            for template_id in self.TEMPLATE_IDS
        )

    @property
    def template_ids(self) -> tuple[str, ...]:
        return self.TEMPLATE_IDS

    def generate(self, count: int) -> list[QueryCase]:
        if count <= 0:
            raise ValueError("count must be greater than zero")
        result: list[QueryCase] = []
        while len(result) < count:
            cycle = list(self._templates)
            self.random.shuffle(cycle)
            result.extend(template() for template in cycle[: count - len(result)])
        return result

    def generate_one_per_template(self) -> list[QueryCase]:
        return [template() for template in self._templates]

    @staticmethod
    def _case(template_id: str, sql: str) -> QueryCase:
        return QueryCase(template_id, " ".join(sql.split()))

    def _date_bounds(self) -> tuple[str, str]:
        month = self.random.randint(1, 6)
        return f"2022-{month:02d}-01", f"2022-{month + 1:02d}-01"

    def _category_revenue(self) -> QueryCase:
        start, end = self._date_bounds()
        return self._case("pagila_category_revenue", f"""
            SELECT c.name, COUNT(DISTINCT r.rental_id) AS rentals, SUM(p.amount) AS revenue
            FROM pagila.category c JOIN pagila.film_category fc USING (category_id)
            JOIN pagila.inventory i USING (film_id) JOIN pagila.rental r USING (inventory_id)
            JOIN pagila.payment p USING (rental_id)
            WHERE p.payment_date >= TIMESTAMPTZ '{start}' AND p.payment_date < TIMESTAMPTZ '{end}'
            GROUP BY c.name ORDER BY revenue DESC
        """)

    def _customer_lifetime_value(self) -> QueryCase:
        minimum = self.random.randint(2, 12)
        customer = self.random.randint(1, 300)
        return self._case("pagila_customer_lifetime_value", f"""
            SELECT c.customer_id, c.first_name, c.last_name, COUNT(p.payment_id) AS payments,
                   SUM(p.amount) AS lifetime_value
            FROM pagila.customer c JOIN pagila.payment p USING (customer_id)
            WHERE c.customer_id >= {customer}
            GROUP BY c.customer_id, c.first_name, c.last_name
            HAVING COUNT(p.payment_id) >= {minimum} ORDER BY lifetime_value DESC LIMIT 50
        """)

    def _store_monthly_revenue(self) -> QueryCase:
        store = self.random.choice((1, 2))
        return self._case("pagila_store_monthly_revenue", f"""
            SELECT date_trunc('month', p.payment_date) AS month, SUM(p.amount) AS revenue,
                   COUNT(DISTINCT p.customer_id) AS customers
            FROM pagila.payment p JOIN pagila.staff s USING (staff_id)
            WHERE s.store_id = {store} GROUP BY month ORDER BY month
        """)

    def _film_popularity(self) -> QueryCase:
        rating = self.random.choice(("G", "PG", "PG-13", "R"))
        return self._case("pagila_film_popularity", f"""
            SELECT f.film_id, f.title, COUNT(r.rental_id) AS rentals
            FROM pagila.film f JOIN pagila.inventory i USING (film_id)
            LEFT JOIN pagila.rental r USING (inventory_id)
            WHERE f.rating = '{rating}' GROUP BY f.film_id, f.title
            ORDER BY rentals DESC, f.title LIMIT 40
        """)

    def _actor_rental_impact(self) -> QueryCase:
        actor = self.random.randint(1, 200)
        return self._case("pagila_actor_rental_impact", f"""
            SELECT a.actor_id, a.first_name, a.last_name, COUNT(r.rental_id) AS rentals,
                   COALESCE(SUM(p.amount), 0) AS revenue
            FROM pagila.actor a JOIN pagila.film_actor fa USING (actor_id)
            JOIN pagila.inventory i USING (film_id) LEFT JOIN pagila.rental r USING (inventory_id)
            LEFT JOIN pagila.payment p USING (rental_id) WHERE a.actor_id >= {actor}
            GROUP BY a.actor_id, a.first_name, a.last_name ORDER BY revenue DESC LIMIT 30
        """)

    def _overdue_rentals(self) -> QueryCase:
        days = self.random.randint(3, 12)
        return self._case("pagila_overdue_rentals", f"""
            SELECT r.customer_id, COUNT(*) AS overdue_count,
                   AVG(EXTRACT(EPOCH FROM (r.return_date - r.rental_date)) / 86400) AS avg_days
            FROM pagila.rental r WHERE r.return_date IS NOT NULL
              AND r.return_date - r.rental_date > INTERVAL '{days} days'
            GROUP BY r.customer_id ORDER BY overdue_count DESC
        """)

    def _customer_recency_rank(self) -> QueryCase:
        store = self.random.choice((1, 2))
        return self._case("pagila_customer_recency_rank", f"""
            WITH activity AS (
                SELECT c.customer_id, c.store_id, MAX(r.rental_date) AS last_rental
                FROM pagila.customer c LEFT JOIN pagila.rental r USING (customer_id)
                WHERE c.store_id = {store} GROUP BY c.customer_id, c.store_id
            )
            SELECT customer_id, last_rental,
                   dense_rank() OVER (ORDER BY last_rental DESC NULLS LAST) AS recency_rank
            FROM activity ORDER BY recency_rank, customer_id
        """)

    def _inventory_availability(self) -> QueryCase:
        film = self.random.randint(1, 1000)
        return self._case("pagila_inventory_availability", f"""
            SELECT i.store_id, COUNT(*) AS copies,
                   COUNT(*) FILTER (WHERE r.rental_id IS NULL) AS available
            FROM pagila.inventory i LEFT JOIN pagila.rental r
              ON r.inventory_id = i.inventory_id AND r.return_date IS NULL
            WHERE i.film_id BETWEEN {film} AND {min(1000, film + 100)}
            GROUP BY i.store_id ORDER BY i.store_id
        """)

    def _country_revenue(self) -> QueryCase:
        country = self.random.randint(1, 90)
        return self._case("pagila_country_revenue", f"""
            SELECT co.country, COUNT(DISTINCT cu.customer_id) AS customers, SUM(p.amount) AS revenue
            FROM pagila.country co JOIN pagila.city ci USING (country_id)
            JOIN pagila.address a USING (city_id) JOIN pagila.customer cu USING (address_id)
            JOIN pagila.payment p USING (customer_id) WHERE co.country_id >= {country}
            GROUP BY co.country ORDER BY revenue DESC
        """)

    def _replacement_exposure(self) -> QueryCase:
        threshold = self.random.choice((15, 20, 25, 30))
        return self._case("pagila_replacement_exposure", f"""
            SELECT f.rating, COUNT(*) AS copies, SUM(f.replacement_cost) AS exposure
            FROM pagila.film f JOIN pagila.inventory i USING (film_id)
            WHERE f.replacement_cost >= {threshold} GROUP BY f.rating ORDER BY exposure DESC
        """)

    def _payment_moving_average(self) -> QueryCase:
        customer = self.random.randint(1, 500)
        return self._case("pagila_payment_moving_average", f"""
            SELECT p.customer_id, p.payment_date, p.amount,
                   AVG(p.amount) OVER (PARTITION BY p.customer_id ORDER BY p.payment_date
                       ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS moving_avg
            FROM pagila.payment p WHERE p.customer_id BETWEEN {customer} AND {min(599, customer + 50)}
            ORDER BY p.customer_id, p.payment_date
        """)

    def _category_percentiles(self) -> QueryCase:
        minimum = self.random.choice((1.99, 2.99, 3.99, 4.99))
        return self._case("pagila_category_percentiles", f"""
            SELECT c.name, percentile_cont(0.5) WITHIN GROUP (ORDER BY p.amount) AS median_payment,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY p.amount) AS p90_payment
            FROM pagila.category c JOIN pagila.film_category fc USING (category_id)
            JOIN pagila.inventory i USING (film_id) JOIN pagila.rental r USING (inventory_id)
            JOIN pagila.payment p USING (rental_id) WHERE p.amount >= {minimum:.2f}
            GROUP BY c.name ORDER BY p90_payment DESC
        """)

    def _staff_performance(self) -> QueryCase:
        start, end = self._date_bounds()
        return self._case("pagila_staff_performance", f"""
            SELECT s.staff_id, s.first_name, s.last_name, COUNT(p.payment_id) AS payments,
                   SUM(p.amount) AS revenue
            FROM pagila.staff s JOIN pagila.payment p USING (staff_id)
            WHERE p.payment_date >= TIMESTAMPTZ '{start}' AND p.payment_date < TIMESTAMPTZ '{end}'
            GROUP BY s.staff_id, s.first_name, s.last_name ORDER BY revenue DESC
        """)

    def _rental_gaps(self) -> QueryCase:
        customer = self.random.randint(1, 500)
        return self._case("pagila_rental_gaps", f"""
            WITH ordered AS (
                SELECT r.customer_id, r.rental_date,
                       lag(r.rental_date) OVER (PARTITION BY r.customer_id ORDER BY r.rental_date) AS previous
                FROM pagila.rental r WHERE r.customer_id >= {customer}
            )
            SELECT customer_id, AVG(rental_date - previous) AS avg_gap
            FROM ordered WHERE previous IS NOT NULL GROUP BY customer_id ORDER BY avg_gap DESC
        """)

    def _inactive_customers(self) -> QueryCase:
        cutoff = self.random.choice(("2022-04-01", "2022-05-01", "2022-06-01"))
        return self._case("pagila_inactive_customers", f"""
            SELECT c.customer_id, c.first_name, c.last_name
            FROM pagila.customer c WHERE c.activebool = true AND NOT EXISTS (
                SELECT 1 FROM pagila.rental r WHERE r.customer_id = c.customer_id
                  AND r.rental_date >= TIMESTAMPTZ '{cutoff}'
            ) ORDER BY c.customer_id
        """)

    def _film_pair_affinity(self) -> QueryCase:
        minimum = self.random.randint(2, 5)
        return self._case("pagila_film_pair_affinity", f"""
            SELECT i1.film_id AS first_film, i2.film_id AS second_film,
                   COUNT(DISTINCT r1.customer_id) AS shared_customers
            FROM pagila.rental r1 JOIN pagila.inventory i1 USING (inventory_id)
            JOIN pagila.rental r2 ON r2.customer_id = r1.customer_id AND r2.rental_id > r1.rental_id
            JOIN pagila.inventory i2 ON i2.inventory_id = r2.inventory_id
            WHERE i1.film_id < i2.film_id GROUP BY i1.film_id, i2.film_id
            HAVING COUNT(DISTINCT r1.customer_id) >= {minimum}
            ORDER BY shared_customers DESC LIMIT 40
        """)

    def _customer_cohorts(self) -> QueryCase:
        year = self.random.choice((2005, 2006))
        return self._case("pagila_customer_cohorts", f"""
            SELECT date_trunc('month', c.create_date) AS cohort,
                   COUNT(DISTINCT c.customer_id) AS customers, COUNT(r.rental_id) AS rentals
            FROM pagila.customer c LEFT JOIN pagila.rental r USING (customer_id)
            WHERE EXTRACT(YEAR FROM c.create_date) >= {year}
            GROUP BY cohort ORDER BY cohort
        """)

    def _store_inventory_turnover(self) -> QueryCase:
        days = self.random.randint(3, 9)
        return self._case("pagila_store_inventory_turnover", f"""
            SELECT i.store_id, COUNT(DISTINCT i.inventory_id) AS inventory,
                   COUNT(r.rental_id) AS rentals,
                   COUNT(r.rental_id)::numeric / NULLIF(COUNT(DISTINCT i.inventory_id), 0) AS turnover
            FROM pagila.inventory i LEFT JOIN pagila.rental r USING (inventory_id)
            WHERE r.return_date - r.rental_date >= INTERVAL '{days} days'
            GROUP BY i.store_id ORDER BY turnover DESC
        """)

    def _actor_category_diversity(self) -> QueryCase:
        actor = self.random.randint(1, 160)
        return self._case("pagila_actor_category_diversity", f"""
            SELECT a.actor_id, a.first_name, a.last_name,
                   COUNT(DISTINCT fc.category_id) AS category_count
            FROM pagila.actor a JOIN pagila.film_actor fa USING (actor_id)
            JOIN pagila.film_category fc USING (film_id) WHERE a.actor_id >= {actor}
            GROUP BY a.actor_id, a.first_name, a.last_name ORDER BY category_count DESC
        """)

    def _rating_revenue_pivot(self) -> QueryCase:
        start, end = self._date_bounds()
        return self._case("pagila_rating_revenue_pivot", f"""
            SELECT i.store_id,
                   SUM(p.amount) FILTER (WHERE f.rating = 'G') AS g_revenue,
                   SUM(p.amount) FILTER (WHERE f.rating = 'PG') AS pg_revenue,
                   SUM(p.amount) FILTER (WHERE f.rating = 'R') AS r_revenue
            FROM pagila.payment p JOIN pagila.rental r USING (rental_id)
            JOIN pagila.inventory i USING (inventory_id) JOIN pagila.film f USING (film_id)
            WHERE p.payment_date >= TIMESTAMPTZ '{start}' AND p.payment_date < TIMESTAMPTZ '{end}'
            GROUP BY i.store_id ORDER BY i.store_id
        """)

    def _language_catalog(self) -> QueryCase:
        length = self.random.choice((60, 90, 120, 150))
        return self._case("pagila_language_catalog", f"""
            SELECT l.name, f.rating, COUNT(*) AS films, AVG(f.length) AS avg_length
            FROM pagila.language l JOIN pagila.film f USING (language_id)
            WHERE f.length >= {length} GROUP BY l.name, f.rating ORDER BY films DESC
        """)

    def _city_customer_density(self) -> QueryCase:
        country = self.random.randint(1, 100)
        return self._case("pagila_city_customer_density", f"""
            SELECT co.country, ci.city, COUNT(cu.customer_id) AS customers
            FROM pagila.country co JOIN pagila.city ci USING (country_id)
            JOIN pagila.address a USING (city_id) LEFT JOIN pagila.customer cu USING (address_id)
            WHERE co.country_id <= {country} GROUP BY co.country, ci.city
            ORDER BY customers DESC LIMIT 60
        """)

    def _duration_outliers(self) -> QueryCase:
        rating = self.random.choice(("G", "PG", "PG-13", "R", "NC-17"))
        return self._case("pagila_duration_outliers", f"""
            WITH stats AS (
                SELECT rating, AVG(length) AS avg_length, STDDEV_POP(length) AS sd
                FROM pagila.film GROUP BY rating
            )
            SELECT f.film_id, f.title, f.length FROM pagila.film f
            JOIN stats s USING (rating) WHERE f.rating = '{rating}'
              AND f.length > s.avg_length + s.sd ORDER BY f.length DESC
        """)

    def _repeat_renters(self) -> QueryCase:
        minimum = self.random.randint(15, 35)
        customer = self.random.randint(1, 300)
        return self._case("pagila_repeat_renters", f"""
            SELECT c.customer_id, c.first_name, c.last_name, COUNT(r.rental_id) AS rentals
            FROM pagila.customer c JOIN pagila.rental r USING (customer_id)
            WHERE c.customer_id >= {customer}
            GROUP BY c.customer_id, c.first_name, c.last_name
            HAVING COUNT(r.rental_id) >= {minimum} ORDER BY rentals DESC
        """)

    def _daily_revenue_rollup(self) -> QueryCase:
        start, end = self._date_bounds()
        return self._case("pagila_daily_revenue_rollup", f"""
            SELECT date_trunc('day', p.payment_date) AS day, s.store_id, SUM(p.amount) AS revenue
            FROM pagila.payment p JOIN pagila.staff s USING (staff_id)
            WHERE p.payment_date >= TIMESTAMPTZ '{start}' AND p.payment_date < TIMESTAMPTZ '{end}'
            GROUP BY GROUPING SETS ((day, s.store_id), (day), ())
            ORDER BY day NULLS LAST, s.store_id NULLS LAST
        """)

    def _return_sla(self) -> QueryCase:
        days = self.random.randint(3, 10)
        return self._case("pagila_return_sla", f"""
            SELECT i.store_id, COUNT(*) AS rentals,
                   COUNT(*) FILTER (WHERE r.return_date <= r.rental_date + INTERVAL '{days} days') AS within_sla
            FROM pagila.rental r JOIN pagila.inventory i USING (inventory_id)
            WHERE r.return_date IS NOT NULL GROUP BY i.store_id ORDER BY i.store_id
        """)

    def _top_films_per_category(self) -> QueryCase:
        limit = self.random.randint(3, 8)
        rating = self.random.choice(("G", "PG", "PG-13", "R", "NC-17"))
        return self._case("pagila_top_films_per_category", f"""
            WITH ranked AS (
                SELECT c.name AS category, f.title, COUNT(r.rental_id) AS rentals,
                       dense_rank() OVER (PARTITION BY c.category_id ORDER BY COUNT(r.rental_id) DESC) AS rank
                FROM pagila.category c JOIN pagila.film_category fc USING (category_id)
                JOIN pagila.film f USING (film_id) JOIN pagila.inventory i USING (film_id)
                LEFT JOIN pagila.rental r USING (inventory_id) WHERE f.rating = '{rating}'
                GROUP BY c.category_id, c.name, f.film_id, f.title
            ) SELECT category, title, rentals FROM ranked WHERE rank <= {limit}
            ORDER BY category, rentals DESC
        """)

    def _customer_payment_transitions(self) -> QueryCase:
        customer = self.random.randint(1, 500)
        return self._case("pagila_customer_payment_transitions", f"""
            SELECT p.customer_id, p.payment_date, p.amount,
                   lag(p.amount) OVER (PARTITION BY p.customer_id ORDER BY p.payment_date) AS previous_amount,
                   p.amount - lag(p.amount) OVER (PARTITION BY p.customer_id ORDER BY p.payment_date) AS delta
            FROM pagila.payment p WHERE p.customer_id >= {customer}
            ORDER BY p.customer_id, p.payment_date
        """)

    def _unrented_films(self) -> QueryCase:
        rating = self.random.choice(("G", "PG", "PG-13", "R", "NC-17"))
        return self._case("pagila_unrented_films", f"""
            SELECT f.film_id, f.title FROM pagila.film f
            WHERE f.rating = '{rating}' AND NOT EXISTS (
                SELECT 1 FROM pagila.inventory i JOIN pagila.rental r USING (inventory_id)
                WHERE i.film_id = f.film_id
            ) ORDER BY f.title
        """)

    def _inventory_imbalance(self) -> QueryCase:
        minimum = self.random.randint(1, 4)
        return self._case("pagila_inventory_imbalance", f"""
            SELECT i.film_id,
                   COUNT(*) FILTER (WHERE i.store_id = 1) AS store_one,
                   COUNT(*) FILTER (WHERE i.store_id = 2) AS store_two
            FROM pagila.inventory i WHERE i.film_id >= {minimum}
            GROUP BY i.film_id
            HAVING ABS(COUNT(*) FILTER (WHERE i.store_id = 1) -
                       COUNT(*) FILTER (WHERE i.store_id = 2)) >= {minimum}
            ORDER BY i.film_id
        """)
