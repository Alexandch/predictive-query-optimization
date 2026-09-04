"""Sealed production-like control workload for the retail schema."""

from __future__ import annotations

from datetime import date, timedelta
import random
from typing import Callable

from .query_case import QueryCase


class RetailControlQueryGenerator:
    """Generate query structures absent from :class:`RetailQueryGenerator`."""

    TEMPLATE_IDS = (
        "control_retail_rfm_deciles",
        "control_retail_payment_reconciliation",
        "control_retail_delivery_percentiles",
        "control_retail_rolling_channel_revenue",
        "control_retail_category_tree_revenue",
        "control_retail_stock_rank",
        "control_retail_order_value_outliers",
        "control_retail_customer_cohort",
        "control_retail_latest_order_distinct",
        "control_retail_product_share_change",
        "control_retail_market_basket_lateral",
        "control_retail_fulfillment_funnel",
        "control_retail_channel_status_cube",
        "control_retail_inventory_reorder",
        "control_retail_customer_purchase_gaps",
    )

    def __init__(self, seed: int = 9401) -> None:
        self.random = random.Random(seed)
        self._templates: tuple[Callable[[], QueryCase], ...] = (
            self._rfm_deciles,
            self._payment_reconciliation,
            self._delivery_percentiles,
            self._rolling_channel_revenue,
            self._category_tree_revenue,
            self._stock_rank,
            self._order_value_outliers,
            self._customer_cohort,
            self._latest_order_distinct,
            self._product_share_change,
            self._market_basket_lateral,
            self._fulfillment_funnel,
            self._channel_status_cube,
            self._inventory_reorder,
            self._customer_purchase_gaps,
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

    def _day(self) -> date:
        return date(2023, 6, 1) + timedelta(days=self.random.randrange(850))

    @staticmethod
    def _case(name: str, sql_text: str) -> QueryCase:
        return QueryCase(f"control_retail_{name}", " ".join(sql_text.split()))

    def _rfm_deciles(self) -> QueryCase:
        cutoff = self._day()
        return self._case("rfm_deciles", f"""
            WITH customer_metrics AS (
                SELECT customer_id, DATE '{cutoff}' - MAX(ordered_at)::date AS recency,
                       COUNT(*) AS frequency, SUM(total_amount) AS monetary
                FROM retail.customer_orders WHERE order_status <> 'cancelled'
                GROUP BY customer_id
            ), scored AS (
                SELECT *, NTILE(10) OVER (ORDER BY recency) AS r_score,
                       NTILE(10) OVER (ORDER BY frequency DESC) AS f_score,
                       NTILE(10) OVER (ORDER BY monetary DESC) AS m_score
                FROM customer_metrics
            )
            SELECT r_score, f_score, m_score, COUNT(*) AS customers,
                   AVG(monetary) AS avg_value FROM scored
            GROUP BY r_score, f_score, m_score ORDER BY customers DESC
        """)

    def _payment_reconciliation(self) -> QueryCase:
        tolerance = self.random.choice((1, 5, 20, 100))
        return self._case("payment_reconciliation", f"""
            SELECT o.order_id, o.total_amount, COALESCE(SUM(p.amount), 0) AS paid,
                   o.total_amount - COALESCE(SUM(p.amount), 0) AS difference
            FROM retail.customer_orders o
            LEFT JOIN retail.payments p ON p.order_id = o.order_id
              AND p.payment_status IN ('authorized', 'captured')
            GROUP BY o.order_id
            HAVING ABS(o.total_amount - COALESCE(SUM(p.amount), 0)) > {tolerance}
            ORDER BY ABS(o.total_amount - COALESCE(SUM(p.amount), 0)) DESC LIMIT 300
        """)

    def _delivery_percentiles(self) -> QueryCase:
        carrier = self.random.choice(("DHL", "DPD", "FedEx", "LocalPost"))
        return self._case("delivery_percentiles", f"""
            SELECT carrier,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY delivered_at - shipped_at) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY delivered_at - shipped_at) AS p90,
                   percentile_cont(0.99) WITHIN GROUP (ORDER BY delivered_at - shipped_at) AS p99
            FROM retail.shipments
            WHERE carrier = '{carrier}' AND delivered_at IS NOT NULL
            GROUP BY carrier
        """)

    def _rolling_channel_revenue(self) -> QueryCase:
        width = self.random.choice((6, 13, 29, 59))
        return self._case("rolling_channel_revenue", f"""
            WITH daily AS (
                SELECT ordered_at::date AS day, sales_channel, SUM(total_amount) AS revenue
                FROM retail.customer_orders WHERE order_status <> 'cancelled'
                GROUP BY ordered_at::date, sales_channel
            )
            SELECT day, sales_channel, revenue,
                   SUM(revenue) OVER (PARTITION BY sales_channel ORDER BY day
                       ROWS BETWEEN {width} PRECEDING AND CURRENT ROW) AS rolling_revenue,
                   revenue - LAG(revenue) OVER (PARTITION BY sales_channel ORDER BY day) AS daily_change
            FROM daily ORDER BY day, sales_channel
        """)

    def _category_tree_revenue(self) -> QueryCase:
        root = self.random.randint(1, 10)
        return self._case("category_tree_revenue", f"""
            WITH RECURSIVE category_tree AS (
                SELECT category_id, parent_category_id, category_name, 0 AS depth
                FROM retail.categories WHERE category_id = {root}
                UNION ALL
                SELECT c.category_id, c.parent_category_id, c.category_name, t.depth + 1
                FROM retail.categories c JOIN category_tree t
                  ON c.parent_category_id = t.category_id
            )
            SELECT t.depth, t.category_name, SUM(i.quantity * i.unit_price) AS revenue
            FROM category_tree t
            JOIN retail.products p ON p.category_id = t.category_id
            JOIN retail.order_items i ON i.product_id = p.product_id
            GROUP BY t.depth, t.category_name ORDER BY revenue DESC
        """)

    def _stock_rank(self) -> QueryCase:
        region = f"Region {self.random.randrange(20)}"
        return self._case("stock_rank", f"""
            WITH stock AS (
                SELECT w.region, i.product_id, SUM(i.quantity - i.reserved_quantity) AS available
                FROM retail.inventory i JOIN retail.warehouses w USING (warehouse_id)
                WHERE w.region = '{region}' GROUP BY w.region, i.product_id
            )
            SELECT region, product_id, available,
                   DENSE_RANK() OVER (PARTITION BY region ORDER BY available DESC) AS stock_rank,
                   CUME_DIST() OVER (PARTITION BY region ORDER BY available) AS stock_percentile
            FROM stock ORDER BY stock_rank LIMIT 200
        """)

    def _order_value_outliers(self) -> QueryCase:
        channel = self.random.choice(("web", "mobile", "store", "marketplace"))
        return self._case("order_value_outliers", f"""
            WITH bounds AS (
                SELECT percentile_cont(0.25) WITHIN GROUP (ORDER BY total_amount) AS q1,
                       percentile_cont(0.75) WITHIN GROUP (ORDER BY total_amount) AS q3
                FROM retail.customer_orders WHERE sales_channel = '{channel}'
            )
            SELECT o.order_id, o.customer_id, o.total_amount
            FROM retail.customer_orders o CROSS JOIN bounds b
            WHERE o.sales_channel = '{channel}'
              AND (o.total_amount < b.q1 - 1.5 * (b.q3 - b.q1)
                   OR o.total_amount > b.q3 + 1.5 * (b.q3 - b.q1))
            ORDER BY o.total_amount DESC LIMIT 200
        """)

    def _customer_cohort(self) -> QueryCase:
        segment = self.random.choice(("consumer", "business", "vip"))
        return self._case("customer_cohort", f"""
            WITH first_orders AS (
                SELECT o.customer_id, date_trunc('month', MIN(o.ordered_at)) AS cohort_month
                FROM retail.customer_orders o JOIN retail.customers c USING (customer_id)
                WHERE c.segment = '{segment}' GROUP BY o.customer_id
            ), activity AS (
                SELECT f.cohort_month, date_trunc('month', o.ordered_at) AS activity_month,
                       COUNT(DISTINCT o.customer_id) AS active_customers
                FROM first_orders f JOIN retail.customer_orders o USING (customer_id)
                GROUP BY f.cohort_month, date_trunc('month', o.ordered_at)
            )
            SELECT cohort_month, activity_month, active_customers,
                   active_customers::numeric / FIRST_VALUE(active_customers) OVER (
                       PARTITION BY cohort_month ORDER BY activity_month) AS retention
            FROM activity ORDER BY cohort_month, activity_month
        """)

    def _latest_order_distinct(self) -> QueryCase:
        region = f"Region {self.random.randrange(20)}"
        return self._case("latest_order_distinct", f"""
            SELECT DISTINCT ON (o.customer_id) o.customer_id, o.order_id,
                   o.ordered_at, o.order_status, o.total_amount
            FROM retail.customer_orders o
            JOIN retail.addresses a ON a.address_id = o.shipping_address_id
            WHERE a.region = '{region}'
            ORDER BY o.customer_id, o.ordered_at DESC
        """)

    def _product_share_change(self) -> QueryCase:
        category = self.random.randint(1, 60)
        return self._case("product_share_change", f"""
            WITH monthly AS (
                SELECT date_trunc('month', o.ordered_at) AS month_start, i.product_id,
                       SUM(i.quantity) AS units
                FROM retail.customer_orders o JOIN retail.order_items i USING (order_id)
                JOIN retail.products p USING (product_id)
                WHERE p.category_id = {category}
                GROUP BY date_trunc('month', o.ordered_at), i.product_id
            ), shares AS (
                SELECT *, units::numeric / SUM(units) OVER (PARTITION BY month_start) AS share
                FROM monthly
            )
            SELECT *, share - LAG(share) OVER (PARTITION BY product_id ORDER BY month_start) AS change
            FROM shares ORDER BY ABS(share - LAG(share) OVER (
                PARTITION BY product_id ORDER BY month_start)) DESC NULLS LAST LIMIT 200
        """)

    def _market_basket_lateral(self) -> QueryCase:
        category = self.random.randint(1, 60)
        return self._case("market_basket_lateral", f"""
            SELECT seed.product_id, companion.product_id AS companion_id, companion.purchases
            FROM (SELECT product_id FROM retail.products WHERE category_id = {category} LIMIT 30) seed
            CROSS JOIN LATERAL (
                SELECT other.product_id, COUNT(*) AS purchases
                FROM retail.order_items own JOIN retail.order_items other USING (order_id)
                WHERE own.product_id = seed.product_id AND other.product_id <> own.product_id
                GROUP BY other.product_id ORDER BY purchases DESC LIMIT 3
            ) companion ORDER BY seed.product_id, companion.purchases DESC
        """)

    def _fulfillment_funnel(self) -> QueryCase:
        day = self._day()
        return self._case("fulfillment_funnel", f"""
            WITH cohort AS (
                SELECT order_id FROM retail.customer_orders
                WHERE ordered_at >= DATE '{day}' - INTERVAL '60 days'
                  AND ordered_at < DATE '{day}' + INTERVAL '60 days'
            )
            SELECT COUNT(*) AS orders,
                   COUNT(*) FILTER (WHERE p.payment_status = 'captured') AS captured,
                   COUNT(*) FILTER (WHERE s.shipped_at IS NOT NULL) AS shipped,
                   COUNT(*) FILTER (WHERE s.delivered_at IS NOT NULL) AS delivered
            FROM cohort c LEFT JOIN retail.payments p USING (order_id)
            LEFT JOIN retail.shipments s USING (order_id)
        """)

    def _channel_status_cube(self) -> QueryCase:
        return self._case("channel_status_cube", """
            SELECT sales_channel, order_status, ordered_at::date AS day,
                   COUNT(*) AS orders, SUM(total_amount) AS revenue
            FROM retail.customer_orders
            GROUP BY CUBE (sales_channel, order_status, ordered_at::date)
            ORDER BY day NULLS LAST, sales_channel NULLS LAST, order_status NULLS LAST
        """)

    def _inventory_reorder(self) -> QueryCase:
        days = self.random.choice((30, 60, 90))
        return self._case("inventory_reorder", f"""
            WITH demand AS (
                SELECT i.product_id, SUM(i.quantity)::numeric / {days} AS daily_units
                FROM retail.order_items i JOIN retail.customer_orders o USING (order_id)
                WHERE o.ordered_at >= timestamptz '2025-12-31 00:00:00+00' - INTERVAL '{days} days'
                GROUP BY i.product_id
            ), stock AS (
                SELECT product_id, SUM(quantity - reserved_quantity) AS available
                FROM retail.inventory GROUP BY product_id
            )
            SELECT s.product_id, s.available, d.daily_units,
                   s.available / NULLIF(d.daily_units, 0) AS days_of_supply
            FROM stock s JOIN demand d USING (product_id)
            WHERE s.available / NULLIF(d.daily_units, 0) < 14
            ORDER BY days_of_supply LIMIT 300
        """)

    def _customer_purchase_gaps(self) -> QueryCase:
        segment = self.random.choice(("consumer", "business", "vip"))
        return self._case("customer_purchase_gaps", f"""
            WITH history AS (
                SELECT o.customer_id, o.ordered_at,
                       LAG(o.ordered_at) OVER (
                           PARTITION BY o.customer_id ORDER BY o.ordered_at
                       ) AS previous_order
                FROM retail.customer_orders o JOIN retail.customers c USING (customer_id)
                WHERE c.segment = '{segment}' AND o.order_status <> 'cancelled'
            )
            SELECT customer_id, AVG(ordered_at - previous_order) AS avg_gap,
                   MAX(ordered_at - previous_order) AS max_gap, COUNT(*) AS purchases
            FROM history WHERE previous_order IS NOT NULL GROUP BY customer_id
            ORDER BY max_gap DESC LIMIT 250
        """)

