"""Sealed unseen-template control workload for the CH-benCHmark schema."""

from __future__ import annotations

from datetime import date, timedelta
import random
from typing import Callable

from .query_case import QueryCase


class CHBenchmarkControlQueryGenerator:
    """Generate control templates which must never be merged into training."""

    TEMPLATE_IDS = (
        "control_ch_customer_rfm",
        "control_ch_stockout_risk",
        "control_ch_demand_volatility",
        "control_ch_supplier_concentration",
        "control_ch_order_gaps",
        "control_ch_basket_affinity",
        "control_ch_district_anomaly",
        "control_ch_cohort_retention",
        "control_ch_product_pareto",
        "control_ch_warehouse_reconciliation",
    )

    def __init__(self, seed: int = 16101) -> None:
        self.random = random.Random(seed)
        self._templates: tuple[Callable[[], QueryCase], ...] = (
            self._customer_rfm,
            self._stockout_risk,
            self._demand_volatility,
            self._supplier_concentration,
            self._order_gaps,
            self._basket_affinity,
            self._district_anomaly,
            self._cohort_retention,
            self._product_pareto,
            self._warehouse_reconciliation,
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
        return date(2024, 1, 1) + timedelta(days=self.random.randrange(700))

    @staticmethod
    def _case(name: str, sql_text: str) -> QueryCase:
        return QueryCase(f"control_ch_{name}", " ".join(sql_text.split()))

    def _customer_rfm(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        return self._case("customer_rfm", f"""
            WITH customer_value AS (
                SELECT o.o_c_id, o.o_d_id,
                       MAX(o.o_entry_d) AS last_order,
                       COUNT(DISTINCT o.o_id) AS frequency,
                       SUM(ol.ol_amount) AS monetary
                FROM chbenchmark.oorder o
                JOIN chbenchmark.order_line ol
                  ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id
                WHERE o.o_w_id = {warehouse}
                GROUP BY o.o_c_id, o.o_d_id
            ), scored AS (
                SELECT *, NTILE(5) OVER (ORDER BY last_order) AS recency_score,
                       NTILE(5) OVER (ORDER BY frequency) AS frequency_score,
                       NTILE(5) OVER (ORDER BY monetary) AS monetary_score
                FROM customer_value
            )
            SELECT recency_score, frequency_score, monetary_score,
                   COUNT(*) AS customers, SUM(monetary) AS revenue
            FROM scored GROUP BY recency_score, frequency_score, monetary_score
            ORDER BY revenue DESC
        """)

    def _stockout_risk(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        days = self.random.choice((30, 60, 90))
        return self._case("stockout_risk", f"""
            WITH demand AS (
                SELECT ol.ol_i_id, SUM(ol.ol_quantity)::numeric / {days} AS daily_units
                FROM chbenchmark.order_line ol
                JOIN chbenchmark.oorder o
                  ON o.o_w_id = ol.ol_w_id AND o.o_d_id = ol.ol_d_id AND o.o_id = ol.ol_o_id
                WHERE ol.ol_w_id = {warehouse}
                  AND o.o_entry_d >= timestamp '2025-12-31' - INTERVAL '{days} days'
                GROUP BY ol.ol_i_id
            )
            SELECT s.s_i_id, s.s_quantity, d.daily_units,
                   s.s_quantity / NULLIF(d.daily_units, 0) AS days_of_supply
            FROM chbenchmark.stock s JOIN demand d ON d.ol_i_id = s.s_i_id
            WHERE s.s_w_id = {warehouse}
              AND s.s_quantity / NULLIF(d.daily_units, 0) < 21
            ORDER BY days_of_supply LIMIT 300
        """)

    def _demand_volatility(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        return self._case("demand_volatility", f"""
            WITH weekly AS (
                SELECT ol.ol_i_id, date_trunc('week', o.o_entry_d) AS week,
                       SUM(ol.ol_quantity) AS units
                FROM chbenchmark.order_line ol
                JOIN chbenchmark.oorder o
                  ON o.o_w_id = ol.ol_w_id AND o.o_d_id = ol.ol_d_id AND o.o_id = ol.ol_o_id
                WHERE ol.ol_w_id = {warehouse}
                GROUP BY ol.ol_i_id, date_trunc('week', o.o_entry_d)
            )
            SELECT ol_i_id, AVG(units) AS avg_units, STDDEV_SAMP(units) AS volatility,
                   STDDEV_SAMP(units) / NULLIF(AVG(units), 0) AS coefficient_of_variation
            FROM weekly GROUP BY ol_i_id HAVING COUNT(*) >= 3
            ORDER BY coefficient_of_variation DESC NULLS LAST LIMIT 250
        """)

    def _supplier_concentration(self) -> QueryCase:
        region = self.random.randrange(5)
        return self._case("supplier_concentration", f"""
            WITH supplier_revenue AS (
                SELECT su.su_suppkey, SUM(ol.ol_amount) AS revenue
                FROM chbenchmark.supplier su
                JOIN chbenchmark.nation n ON n.n_nationkey = su.su_nationkey
                JOIN chbenchmark.stock s ON MOD(s.s_w_id * s.s_i_id, 10000) = su.su_suppkey
                JOIN chbenchmark.order_line ol
                  ON ol.ol_supply_w_id = s.s_w_id AND ol.ol_i_id = s.s_i_id
                WHERE n.n_regionkey = {region}
                GROUP BY su.su_suppkey
            ), shares AS (
                SELECT revenue / NULLIF(SUM(revenue) OVER (), 0) AS share
                FROM supplier_revenue
            )
            SELECT COUNT(*) AS suppliers, MAX(share) AS largest_share,
                   SUM(share * share) AS hhi FROM shares
        """)

    def _order_gaps(self) -> QueryCase:
        state = self.random.choice(("CA", "NY", "TX", "WA", "FL", "OH", "NV"))
        gap = self.random.choice((30, 60, 90, 120))
        return self._case("order_gaps", f"""
            WITH timeline AS (
                SELECT c.c_w_id, c.c_d_id, c.c_id, o.o_id, o.o_entry_d,
                       LAG(o.o_entry_d) OVER (
                           PARTITION BY c.c_w_id, c.c_d_id, c.c_id ORDER BY o.o_entry_d
                       ) AS previous_order
                FROM chbenchmark.customer c
                JOIN chbenchmark.oorder o
                  ON o.o_w_id = c.c_w_id AND o.o_d_id = c.c_d_id AND o.o_c_id = c.c_id
                WHERE c.c_state = '{state}'
            )
            SELECT * FROM timeline
            WHERE o_entry_d - previous_order > INTERVAL '{gap} days'
            ORDER BY o_entry_d - previous_order DESC LIMIT 300
        """)

    def _basket_affinity(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        district = self.random.randint(1, 10)
        return self._case("basket_affinity", f"""
            SELECT LEAST(a.ol_i_id, b.ol_i_id) AS item_a,
                   GREATEST(a.ol_i_id, b.ol_i_id) AS item_b,
                   COUNT(*) AS baskets
            FROM chbenchmark.order_line a
            JOIN chbenchmark.order_line b
              ON b.ol_w_id = a.ol_w_id AND b.ol_d_id = a.ol_d_id
             AND b.ol_o_id = a.ol_o_id AND b.ol_i_id > a.ol_i_id
            WHERE a.ol_w_id = {warehouse} AND a.ol_d_id = {district}
            GROUP BY LEAST(a.ol_i_id, b.ol_i_id), GREATEST(a.ol_i_id, b.ol_i_id)
            HAVING COUNT(*) >= 2 ORDER BY baskets DESC LIMIT 200
        """)

    def _district_anomaly(self) -> QueryCase:
        day = self._day()
        return self._case("district_anomaly", f"""
            WITH daily AS (
                SELECT o.o_w_id, o.o_d_id, o.o_entry_d::date AS day,
                       SUM(ol.ol_amount) AS revenue
                FROM chbenchmark.oorder o
                JOIN chbenchmark.order_line ol
                  ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id
                WHERE o.o_entry_d >= DATE '{day}' - INTERVAL '365 days'
                GROUP BY o.o_w_id, o.o_d_id, o.o_entry_d::date
            ), stats AS (
                SELECT *, AVG(revenue) OVER (PARTITION BY o_w_id, o_d_id) AS mean_revenue,
                       STDDEV_SAMP(revenue) OVER (PARTITION BY o_w_id, o_d_id) AS sd_revenue
                FROM daily
            )
            SELECT *, (revenue - mean_revenue) / NULLIF(sd_revenue, 0) AS z_score
            FROM stats WHERE ABS((revenue - mean_revenue) / NULLIF(sd_revenue, 0)) > 2.5
            ORDER BY ABS((revenue - mean_revenue) / NULLIF(sd_revenue, 0)) DESC
        """)

    def _cohort_retention(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        return self._case("cohort_retention", f"""
            WITH activity AS (
                SELECT o.o_c_id, o.o_d_id, date_trunc('quarter', MIN(o.o_entry_d)) AS cohort,
                       COUNT(DISTINCT date_trunc('quarter', o.o_entry_d)) AS active_quarters
                FROM chbenchmark.oorder o WHERE o.o_w_id = {warehouse}
                GROUP BY o.o_c_id, o.o_d_id
            )
            SELECT cohort, COUNT(*) AS customers,
                   COUNT(*) FILTER (WHERE active_quarters >= 2) AS retained_2q,
                   COUNT(*) FILTER (WHERE active_quarters >= 3) AS retained_3q
            FROM activity GROUP BY cohort ORDER BY cohort
        """)

    def _product_pareto(self) -> QueryCase:
        state = self.random.choice(("CA", "NY", "TX", "WA", "FL", "OH", "NV"))
        return self._case("product_pareto", f"""
            WITH product_revenue AS (
                SELECT ol.ol_i_id, SUM(ol.ol_amount) AS revenue
                FROM chbenchmark.customer c
                JOIN chbenchmark.oorder o
                  ON o.o_w_id = c.c_w_id AND o.o_d_id = c.c_d_id AND o.o_c_id = c.c_id
                JOIN chbenchmark.order_line ol
                  ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id
                WHERE c.c_state = '{state}' GROUP BY ol.ol_i_id
            ), cumulative AS (
                SELECT *, SUM(revenue) OVER (ORDER BY revenue DESC) /
                    NULLIF(SUM(revenue) OVER (), 0) AS cumulative_share
                FROM product_revenue
            )
            SELECT COUNT(*) FILTER (WHERE cumulative_share <= 0.80) AS core_products,
                   COUNT(*) AS all_products, MAX(revenue) AS top_product_revenue
            FROM cumulative
        """)

    def _warehouse_reconciliation(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        return self._case("warehouse_reconciliation", f"""
            WITH ordered AS (
                SELECT ol.ol_i_id, SUM(ol.ol_quantity) AS sold_units
                FROM chbenchmark.order_line ol WHERE ol.ol_w_id = {warehouse}
                GROUP BY ol.ol_i_id
            ), stock_activity AS (
                SELECT s.s_i_id, s.s_quantity, s.s_ytd, s.s_order_cnt
                FROM chbenchmark.stock s WHERE s.s_w_id = {warehouse}
            )
            SELECT sa.s_i_id, sa.s_quantity, sa.s_ytd, sa.s_order_cnt,
                   o.sold_units, o.sold_units - sa.s_ytd AS quantity_difference
            FROM stock_activity sa JOIN ordered o ON o.ol_i_id = sa.s_i_id
            WHERE ABS(o.sold_units - sa.s_ytd) > 100
            ORDER BY ABS(o.sold_units - sa.s_ytd) DESC LIMIT 300
        """)
