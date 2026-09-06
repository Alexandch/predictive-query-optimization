"""Parameterized read-only training workload for the CH-benCHmark schema."""

from __future__ import annotations

from datetime import date, timedelta
import random
from typing import Callable

from .query_case import QueryCase


class CHBenchmarkQueryGenerator:
    """Generate training SQL inspired by hybrid order-entry analytics."""

    TEMPLATE_IDS = (
        "train_ch_line_summary",
        "train_ch_regional_low_stock",
        "train_ch_district_revenue",
        "train_ch_customer_order_totals",
        "train_ch_product_sales_by_state",
        "train_ch_supplier_sales",
        "train_ch_delivery_delay",
        "train_ch_backlog",
        "train_ch_warehouse_inventory",
        "train_ch_customer_balance_rank",
        "train_ch_payment_activity",
        "train_ch_item_price_band",
        "train_ch_remote_supply",
        "train_ch_district_daily_sales",
        "train_ch_repeat_customers",
        "train_ch_promotional_items",
        "train_ch_credit_risk",
        "train_ch_order_basket",
        "train_ch_unsold_items",
        "train_ch_customer_last_order",
    )

    def __init__(self, seed: int = 15101) -> None:
        self.random = random.Random(seed)
        self._templates: tuple[Callable[[], QueryCase], ...] = (
            self._line_summary,
            self._regional_low_stock,
            self._district_revenue,
            self._customer_order_totals,
            self._product_sales_by_state,
            self._supplier_sales,
            self._delivery_delay,
            self._backlog,
            self._warehouse_inventory,
            self._customer_balance_rank,
            self._payment_activity,
            self._item_price_band,
            self._remote_supply,
            self._district_daily_sales,
            self._repeat_customers,
            self._promotional_items,
            self._credit_risk,
            self._order_basket,
            self._unsold_items,
            self._customer_last_order,
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
        return date(2023, 2, 1) + timedelta(days=self.random.randrange(1000))

    @staticmethod
    def _case(name: str, sql_text: str) -> QueryCase:
        return QueryCase(f"train_ch_{name}", " ".join(sql_text.split()))

    def _line_summary(self) -> QueryCase:
        day = self._day()
        return self._case("line_summary", f"""
            SELECT ol_number, SUM(ol_quantity) AS units,
                   SUM(ol_amount) AS revenue, AVG(ol_amount) AS avg_amount,
                   COUNT(*) AS line_count
            FROM chbenchmark.order_line
            WHERE ol_delivery_d >= DATE '{day}' - INTERVAL '120 days'
              AND ol_delivery_d < DATE '{day}' + INTERVAL '30 days'
            GROUP BY ol_number ORDER BY revenue DESC
        """)

    def _regional_low_stock(self) -> QueryCase:
        region = self.random.choice(("AFRICA", "AMERICA", "ASIA", "EUROPE", "MIDDLE EAST"))
        threshold = self.random.choice((25, 40, 60, 80))
        return self._case("regional_low_stock", f"""
            SELECT r.r_name, n.n_name, su.su_name, i.i_id, i.i_name,
                   MIN(s.s_quantity) AS minimum_quantity
            FROM chbenchmark.stock s
            JOIN chbenchmark.item i ON i.i_id = s.s_i_id
            JOIN chbenchmark.supplier su ON su.su_suppkey = MOD(s.s_w_id * s.s_i_id, 10000)
            JOIN chbenchmark.nation n ON n.n_nationkey = su.su_nationkey
            JOIN chbenchmark.region r ON r.r_regionkey = n.n_regionkey
            WHERE r.r_name = '{region}' AND s.s_quantity < {threshold}
            GROUP BY r.r_name, n.n_name, su.su_name, i.i_id, i.i_name
            ORDER BY minimum_quantity, i.i_id LIMIT 250
        """)

    def _district_revenue(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        day = self._day()
        return self._case("district_revenue", f"""
            SELECT o.o_d_id, COUNT(DISTINCT o.o_id) AS orders,
                   SUM(ol.ol_quantity) AS units, SUM(ol.ol_amount) AS revenue
            FROM chbenchmark.oorder o
            JOIN chbenchmark.order_line ol
              ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id
            WHERE o.o_w_id = {warehouse}
              AND o.o_entry_d >= DATE '{day}' - INTERVAL '90 days'
              AND o.o_entry_d < DATE '{day}' + INTERVAL '1 day'
            GROUP BY o.o_d_id ORDER BY revenue DESC
        """)

    def _customer_order_totals(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        minimum = self.random.choice((1000, 2500, 5000, 10000))
        return self._case("customer_order_totals", f"""
            SELECT c.c_id, c.c_first, c.c_last, COUNT(DISTINCT o.o_id) AS orders,
                   SUM(ol.ol_amount) AS lifetime_value
            FROM chbenchmark.customer c
            JOIN chbenchmark.oorder o
              ON o.o_w_id = c.c_w_id AND o.o_d_id = c.c_d_id AND o.o_c_id = c.c_id
            JOIN chbenchmark.order_line ol
              ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id
            WHERE c.c_w_id = {warehouse}
            GROUP BY c.c_id, c.c_first, c.c_last
            HAVING SUM(ol.ol_amount) > {minimum}
            ORDER BY lifetime_value DESC LIMIT 200
        """)

    def _product_sales_by_state(self) -> QueryCase:
        state = self.random.choice(("CA", "NY", "TX", "WA", "FL", "OH", "NV"))
        return self._case("product_sales_by_state", f"""
            SELECT i.i_id, i.i_name, COUNT(DISTINCT o.o_id) AS orders,
                   SUM(ol.ol_quantity) AS units, SUM(ol.ol_amount) AS revenue
            FROM chbenchmark.customer c
            JOIN chbenchmark.oorder o
              ON o.o_w_id = c.c_w_id AND o.o_d_id = c.c_d_id AND o.o_c_id = c.c_id
            JOIN chbenchmark.order_line ol
              ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id
            JOIN chbenchmark.item i ON i.i_id = ol.ol_i_id
            WHERE c.c_state = '{state}'
            GROUP BY i.i_id, i.i_name ORDER BY revenue DESC LIMIT 150
        """)

    def _supplier_sales(self) -> QueryCase:
        nation = self.random.randrange(25)
        return self._case("supplier_sales", f"""
            SELECT su.su_suppkey, su.su_name, COUNT(*) AS supplied_lines,
                   SUM(ol.ol_amount) AS revenue, AVG(s.s_quantity) AS avg_stock
            FROM chbenchmark.supplier su
            JOIN chbenchmark.stock s ON MOD(s.s_w_id * s.s_i_id, 10000) = su.su_suppkey
            JOIN chbenchmark.order_line ol
              ON ol.ol_supply_w_id = s.s_w_id AND ol.ol_i_id = s.s_i_id
            WHERE su.su_nationkey = {nation}
            GROUP BY su.su_suppkey, su.su_name ORDER BY revenue DESC LIMIT 200
        """)

    def _delivery_delay(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        hours = self.random.choice((12, 24, 48, 72))
        return self._case("delivery_delay", f"""
            SELECT o.o_d_id, o.o_id, o.o_entry_d,
                   MAX(ol.ol_delivery_d) AS completed_at,
                   MAX(ol.ol_delivery_d) - o.o_entry_d AS elapsed
            FROM chbenchmark.oorder o
            JOIN chbenchmark.order_line ol
              ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id
            WHERE o.o_w_id = {warehouse} AND ol.ol_delivery_d IS NOT NULL
            GROUP BY o.o_d_id, o.o_id, o.o_entry_d
            HAVING MAX(ol.ol_delivery_d) - o.o_entry_d > INTERVAL '{hours} hours'
            ORDER BY elapsed DESC LIMIT 250
        """)

    def _backlog(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        return self._case("backlog", f"""
            SELECT no.no_d_id, COUNT(*) AS pending_orders,
                   MIN(o.o_entry_d) AS oldest_order,
                   SUM(ol.ol_amount) AS pending_value
            FROM chbenchmark.new_order no
            JOIN chbenchmark.oorder o
              ON o.o_w_id = no.no_w_id AND o.o_d_id = no.no_d_id AND o.o_id = no.no_o_id
            JOIN chbenchmark.order_line ol
              ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id
            WHERE no.no_w_id = {warehouse}
            GROUP BY no.no_d_id ORDER BY oldest_order
        """)

    def _warehouse_inventory(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        prefix = self.random.choice(("standard", "PROMO", "clearance"))
        return self._case("warehouse_inventory", f"""
            SELECT s.s_w_id, COUNT(*) AS sku_count, SUM(s.s_quantity) AS units,
                   SUM(s.s_quantity * i.i_price) AS inventory_value
            FROM chbenchmark.stock s
            JOIN chbenchmark.item i ON i.i_id = s.s_i_id
            WHERE s.s_w_id = {warehouse} AND i.i_data ILIKE '%{prefix}%'
            GROUP BY s.s_w_id
        """)

    def _customer_balance_rank(self) -> QueryCase:
        state = self.random.choice(("CA", "NY", "TX", "WA", "FL", "OH", "NV"))
        return self._case("customer_balance_rank", f"""
            SELECT c_w_id, c_d_id, c_id, c_balance,
                   DENSE_RANK() OVER (PARTITION BY c_w_id ORDER BY c_balance DESC) AS balance_rank,
                   CUME_DIST() OVER (PARTITION BY c_w_id ORDER BY c_balance) AS balance_percentile
            FROM chbenchmark.customer WHERE c_state = '{state}'
            ORDER BY c_w_id, balance_rank LIMIT 500
        """)

    def _payment_activity(self) -> QueryCase:
        day = self._day()
        amount = self.random.choice((50, 100, 250, 400))
        return self._case("payment_activity", f"""
            SELECT h.h_w_id, h.h_d_id, COUNT(*) AS payments,
                   SUM(h.h_amount) AS amount, COUNT(DISTINCT h.h_c_id) AS customers
            FROM chbenchmark.history h
            WHERE h.h_date >= DATE '{day}' - INTERVAL '180 days'
              AND h.h_amount >= {amount}
            GROUP BY h.h_w_id, h.h_d_id ORDER BY amount DESC
        """)

    def _item_price_band(self) -> QueryCase:
        lower = self.random.choice((10, 50, 100, 200))
        upper = lower + self.random.choice((50, 100, 200))
        return self._case("item_price_band", f"""
            SELECT width_bucket(i_price, {lower}, {upper}, 8) AS price_band,
                   COUNT(*) AS items, AVG(i_price) AS avg_price,
                   COUNT(*) FILTER (WHERE i_data ILIKE '%PROMO%') AS promotional
            FROM chbenchmark.item
            WHERE i_price BETWEEN {lower} AND {upper}
            GROUP BY price_band ORDER BY price_band
        """)

    def _remote_supply(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        return self._case("remote_supply", f"""
            SELECT ol.ol_supply_w_id, COUNT(*) AS remote_lines,
                   SUM(ol.ol_quantity) AS units, SUM(ol.ol_amount) AS revenue
            FROM chbenchmark.order_line ol
            WHERE ol.ol_w_id = {warehouse} AND ol.ol_supply_w_id <> ol.ol_w_id
            GROUP BY ol.ol_supply_w_id ORDER BY revenue DESC
        """)

    def _district_daily_sales(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        width = self.random.choice((6, 13, 29))
        return self._case("district_daily_sales", f"""
            WITH daily AS (
                SELECT o.o_entry_d::date AS day, o.o_d_id,
                       SUM(ol.ol_amount) AS revenue
                FROM chbenchmark.oorder o
                JOIN chbenchmark.order_line ol
                  ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id
                WHERE o.o_w_id = {warehouse}
                GROUP BY o.o_entry_d::date, o.o_d_id
            )
            SELECT day, o_d_id, revenue,
                   AVG(revenue) OVER (PARTITION BY o_d_id ORDER BY day
                       ROWS BETWEEN {width} PRECEDING AND CURRENT ROW) AS rolling_revenue
            FROM daily ORDER BY o_d_id, day
        """)

    def _repeat_customers(self) -> QueryCase:
        minimum = self.random.choice((2, 3, 4, 5))
        return self._case("repeat_customers", f"""
            SELECT c.c_state, COUNT(*) AS repeat_customers,
                   AVG(customer_orders.order_count) AS avg_orders
            FROM chbenchmark.customer c
            JOIN (
                SELECT o_w_id, o_d_id, o_c_id, COUNT(*) AS order_count
                FROM chbenchmark.oorder
                GROUP BY o_w_id, o_d_id, o_c_id HAVING COUNT(*) >= {minimum}
            ) customer_orders
              ON customer_orders.o_w_id = c.c_w_id
             AND customer_orders.o_d_id = c.c_d_id
             AND customer_orders.o_c_id = c.c_id
            GROUP BY c.c_state ORDER BY repeat_customers DESC
        """)

    def _promotional_items(self) -> QueryCase:
        day = self._day()
        return self._case("promotional_items", f"""
            SELECT i.i_id, i.i_name, SUM(ol.ol_quantity) AS units,
                   SUM(ol.ol_amount) AS revenue
            FROM chbenchmark.item i
            JOIN chbenchmark.order_line ol ON ol.ol_i_id = i.i_id
            JOIN chbenchmark.oorder o
              ON o.o_w_id = ol.ol_w_id AND o.o_d_id = ol.ol_d_id AND o.o_id = ol.ol_o_id
            WHERE i.i_data ILIKE '%PROMO%'
              AND o.o_entry_d >= DATE '{day}' - INTERVAL '365 days'
            GROUP BY i.i_id, i.i_name ORDER BY revenue DESC LIMIT 100
        """)

    def _credit_risk(self) -> QueryCase:
        state = self.random.choice(("CA", "NY", "TX", "WA", "FL", "OH", "NV"))
        balance = self.random.choice((1000, 2500, 5000))
        return self._case("credit_risk", f"""
            SELECT c.c_w_id, c.c_d_id, c.c_id, c.c_balance,
                   COUNT(no.no_o_id) AS pending_orders
            FROM chbenchmark.customer c
            LEFT JOIN chbenchmark.oorder o
              ON o.o_w_id = c.c_w_id AND o.o_d_id = c.c_d_id AND o.o_c_id = c.c_id
            LEFT JOIN chbenchmark.new_order no
              ON no.no_w_id = o.o_w_id AND no.no_d_id = o.o_d_id AND no.no_o_id = o.o_id
            WHERE c.c_credit = 'BC' AND c.c_state = '{state}' AND c.c_balance > {balance}
            GROUP BY c.c_w_id, c.c_d_id, c.c_id, c.c_balance
            ORDER BY pending_orders DESC, c.c_balance DESC LIMIT 250
        """)

    def _order_basket(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        minimum = self.random.choice((250, 500, 750, 1000))
        return self._case("order_basket", f"""
            SELECT o.o_d_id, o.o_id, o.o_entry_d, COUNT(*) AS lines,
                   SUM(ol.ol_quantity) AS units, SUM(ol.ol_amount) AS basket_value
            FROM chbenchmark.oorder o
            JOIN chbenchmark.order_line ol
              ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id
            WHERE o.o_w_id = {warehouse}
            GROUP BY o.o_d_id, o.o_id, o.o_entry_d
            HAVING SUM(ol.ol_amount) >= {minimum}
            ORDER BY basket_value DESC LIMIT 300
        """)

    def _unsold_items(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        day = self._day()
        return self._case("unsold_items", f"""
            SELECT i.i_id, i.i_name, i.i_price, s.s_quantity
            FROM chbenchmark.item i
            JOIN chbenchmark.stock s ON s.s_i_id = i.i_id AND s.s_w_id = {warehouse}
            WHERE NOT EXISTS (
                SELECT 1 FROM chbenchmark.order_line ol
                JOIN chbenchmark.oorder o
                  ON o.o_w_id = ol.ol_w_id AND o.o_d_id = ol.ol_d_id AND o.o_id = ol.ol_o_id
                WHERE ol.ol_i_id = i.i_id AND ol.ol_w_id = {warehouse}
                  AND o.o_entry_d >= DATE '{day}' - INTERVAL '90 days'
            )
            ORDER BY s.s_quantity DESC LIMIT 300
        """)

    def _customer_last_order(self) -> QueryCase:
        warehouse = self.random.randint(1, 4)
        district = self.random.randint(1, 10)
        return self._case("customer_last_order", f"""
            SELECT c.c_id, c.c_first, c.c_last, latest.o_id, latest.o_entry_d,
                   latest.order_value
            FROM chbenchmark.customer c
            LEFT JOIN LATERAL (
                SELECT o.o_id, o.o_entry_d, SUM(ol.ol_amount) AS order_value
                FROM chbenchmark.oorder o
                JOIN chbenchmark.order_line ol
                  ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id
                WHERE o.o_w_id = c.c_w_id AND o.o_d_id = c.c_d_id AND o.o_c_id = c.c_id
                GROUP BY o.o_id, o.o_entry_d ORDER BY o.o_entry_d DESC LIMIT 1
            ) latest ON true
            WHERE c.c_w_id = {warehouse} AND c.c_d_id = {district}
            ORDER BY latest.o_entry_d DESC NULLS LAST LIMIT 300
        """)
