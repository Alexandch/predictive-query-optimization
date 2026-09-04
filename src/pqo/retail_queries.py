"""Parameterized training workload for the independent retail schema."""

from __future__ import annotations

from datetime import date, timedelta
import random
from typing import Callable

from .query_case import QueryCase


class RetailQueryGenerator:
    """Generate common OLTP reporting and operational retail queries."""

    TEMPLATE_IDS = (
        "retail_orders_by_status",
        "retail_customer_history",
        "retail_daily_channel_revenue",
        "retail_top_products",
        "retail_low_inventory",
        "retail_stale_pending_orders",
        "retail_failed_payments",
        "retail_shipment_sla",
        "retail_category_revenue",
        "retail_customer_lifetime_value",
        "retail_repeat_customers",
        "retail_stock_by_region",
        "retail_brand_performance",
        "retail_regional_sales",
        "retail_basket_statistics",
        "retail_refund_report",
        "retail_product_affinity",
        "retail_inventory_imbalance",
    )
    STATUSES = ("pending", "paid", "processing", "shipped", "delivered", "cancelled")
    CHANNELS = ("web", "mobile", "store", "marketplace")
    REGIONS = tuple(f"Region {index}" for index in range(20))

    def __init__(self, seed: int = 8401) -> None:
        self.random = random.Random(seed)
        self._templates: tuple[Callable[[], QueryCase], ...] = (
            self._orders_by_status,
            self._customer_history,
            self._daily_channel_revenue,
            self._top_products,
            self._low_inventory,
            self._stale_pending_orders,
            self._failed_payments,
            self._shipment_sla,
            self._category_revenue,
            self._customer_lifetime_value,
            self._repeat_customers,
            self._stock_by_region,
            self._brand_performance,
            self._regional_sales,
            self._basket_statistics,
            self._refund_report,
            self._product_affinity,
            self._inventory_imbalance,
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
        return date(2023, 1, 1) + timedelta(days=self.random.randrange(1000))

    @staticmethod
    def _case(template_id: str, sql_text: str) -> QueryCase:
        return QueryCase(template_id, " ".join(sql_text.split()))

    def _orders_by_status(self) -> QueryCase:
        status, day = self.random.choice(self.STATUSES), self._day()
        return self._case("retail_orders_by_status", f"""
            SELECT order_id, customer_id, ordered_at, total_amount
            FROM retail.customer_orders
            WHERE order_status = '{status}'
              AND ordered_at >= DATE '{day}'
              AND ordered_at < DATE '{day}' + INTERVAL '30 days'
            ORDER BY ordered_at DESC LIMIT 250
        """)

    def _customer_history(self) -> QueryCase:
        customer_id = self.random.randint(1, 50000)
        return self._case("retail_customer_history", f"""
            SELECT o.order_id, o.ordered_at, o.order_status, o.total_amount,
                   COUNT(i.line_no) AS line_count, SUM(i.quantity) AS units
            FROM retail.customer_orders o
            JOIN retail.order_items i ON i.order_id = o.order_id
            WHERE o.customer_id = {customer_id}
            GROUP BY o.order_id ORDER BY o.ordered_at DESC
        """)

    def _daily_channel_revenue(self) -> QueryCase:
        channel, day = self.random.choice(self.CHANNELS), self._day()
        return self._case("retail_daily_channel_revenue", f"""
            SELECT ordered_at::date AS day, COUNT(*) AS orders,
                   SUM(total_amount) AS revenue, AVG(discount_amount) AS avg_discount
            FROM retail.customer_orders
            WHERE sales_channel = '{channel}'
              AND ordered_at >= DATE '{day}' - INTERVAL '90 days'
              AND ordered_at < DATE '{day}' + INTERVAL '90 days'
              AND order_status <> 'cancelled'
            GROUP BY ordered_at::date ORDER BY day
        """)

    def _top_products(self) -> QueryCase:
        day, limit = self._day(), self.random.choice((25, 50, 100))
        return self._case("retail_top_products", f"""
            SELECT p.product_id, p.product_name, SUM(i.quantity) AS units,
                   SUM(i.quantity * i.unit_price) AS revenue
            FROM retail.customer_orders o
            JOIN retail.order_items i ON i.order_id = o.order_id
            JOIN retail.products p ON p.product_id = i.product_id
            WHERE o.ordered_at >= DATE '{day}' - INTERVAL '120 days'
              AND o.order_status IN ('paid', 'processing', 'shipped', 'delivered')
            GROUP BY p.product_id ORDER BY revenue DESC LIMIT {limit}
        """)

    def _low_inventory(self) -> QueryCase:
        region = self.random.choice(self.REGIONS)
        threshold = self.random.randint(3, 30)
        category_id = self.random.randint(1, 60)
        return self._case("retail_low_inventory", f"""
            SELECT w.warehouse_name, p.sku, p.product_name,
                   i.quantity - i.reserved_quantity AS available
            FROM retail.inventory i
            JOIN retail.warehouses w ON w.warehouse_id = i.warehouse_id
            JOIN retail.products p ON p.product_id = i.product_id
            WHERE w.region = '{region}'
              AND i.quantity - i.reserved_quantity < {threshold}
              AND p.category_id = {category_id}
              AND p.is_active
            ORDER BY available, p.sku LIMIT 300
        """)

    def _stale_pending_orders(self) -> QueryCase:
        day = self._day()
        return self._case("retail_stale_pending_orders", f"""
            SELECT o.order_id, o.customer_id, o.ordered_at, o.total_amount
            FROM retail.customer_orders o
            LEFT JOIN retail.payments p ON p.order_id = o.order_id
            WHERE o.order_status = 'pending' AND o.ordered_at < DATE '{day}'
              AND (p.payment_id IS NULL OR p.payment_status = 'failed')
            ORDER BY o.ordered_at LIMIT 250
        """)

    def _failed_payments(self) -> QueryCase:
        method = self.random.choice(("card", "cash", "bank_transfer", "wallet"))
        day = self._day()
        return self._case("retail_failed_payments", f"""
            SELECT p.payment_id, p.order_id, p.amount, o.customer_id, o.ordered_at
            FROM retail.payments p
            JOIN retail.customer_orders o ON o.order_id = p.order_id
            WHERE p.payment_status = 'failed' AND p.payment_method = '{method}'
              AND o.ordered_at >= DATE '{day}' - INTERVAL '120 days'
              AND o.ordered_at < DATE '{day}' + INTERVAL '120 days'
            ORDER BY o.ordered_at DESC LIMIT 500
        """)

    def _shipment_sla(self) -> QueryCase:
        carrier, hours = self.random.choice(("DHL", "DPD", "FedEx", "LocalPost")), self.random.choice((24, 48, 72))
        day = self._day()
        return self._case("retail_shipment_sla", f"""
            SELECT carrier, shipment_status, COUNT(*) AS shipments,
                   AVG(delivered_at - shipped_at) AS avg_delivery_time
            FROM retail.shipments
            WHERE carrier = '{carrier}' AND shipped_at IS NOT NULL
              AND shipped_at >= DATE '{day}' - INTERVAL '180 days'
              AND shipped_at < DATE '{day}' + INTERVAL '180 days'
              AND (delivered_at IS NULL OR delivered_at - shipped_at > INTERVAL '{hours} hours')
            GROUP BY carrier, shipment_status ORDER BY shipments DESC
        """)

    def _category_revenue(self) -> QueryCase:
        category_id, day = self.random.randint(1, 60), self._day()
        return self._case("retail_category_revenue", f"""
            SELECT c.category_name, COUNT(DISTINCT o.order_id) AS orders,
                   SUM(i.quantity * i.unit_price) AS revenue
            FROM retail.categories c
            JOIN retail.products p ON p.category_id = c.category_id
            JOIN retail.order_items i ON i.product_id = p.product_id
            JOIN retail.customer_orders o ON o.order_id = i.order_id
            WHERE c.category_id = {category_id}
              AND o.ordered_at >= DATE '{day}' - INTERVAL '180 days'
            GROUP BY c.category_name
        """)

    def _customer_lifetime_value(self) -> QueryCase:
        segment, minimum = self.random.choice(("consumer", "business", "vip")), self.random.choice((2, 4, 6))
        day = self._day()
        return self._case("retail_customer_lifetime_value", f"""
            SELECT c.customer_id, c.full_name, COUNT(o.order_id) AS orders,
                   SUM(o.total_amount - o.discount_amount) AS lifetime_value,
                   MAX(o.ordered_at) AS last_order
            FROM retail.customers c
            JOIN retail.customer_orders o ON o.customer_id = c.customer_id
            WHERE c.segment = '{segment}' AND o.order_status <> 'cancelled'
              AND o.ordered_at < DATE '{day}' + INTERVAL '1 day'
            GROUP BY c.customer_id HAVING COUNT(o.order_id) >= {minimum}
            ORDER BY lifetime_value DESC LIMIT 200
        """)

    def _repeat_customers(self) -> QueryCase:
        day = self._day()
        return self._case("retail_repeat_customers", f"""
            SELECT customer_id, COUNT(*) AS order_count,
                   MIN(ordered_at) AS first_order, MAX(ordered_at) AS last_order
            FROM retail.customer_orders
            WHERE ordered_at >= DATE '{day}' - INTERVAL '365 days'
              AND order_status = 'delivered'
            GROUP BY customer_id HAVING COUNT(*) >= 3
            ORDER BY order_count DESC, last_order DESC LIMIT 300
        """)

    def _stock_by_region(self) -> QueryCase:
        category_id = self.random.randint(1, 60)
        region = self.random.choice(self.REGIONS)
        return self._case("retail_stock_by_region", f"""
            SELECT w.region, SUM(i.quantity) AS stock,
                   SUM(i.reserved_quantity) AS reserved
            FROM retail.inventory i
            JOIN retail.warehouses w ON w.warehouse_id = i.warehouse_id
            JOIN retail.products p ON p.product_id = i.product_id
            WHERE p.category_id = {category_id} AND w.region = '{region}'
            GROUP BY w.region ORDER BY stock DESC
        """)

    def _brand_performance(self) -> QueryCase:
        brand = f"Brand {self.random.randrange(250)}"
        day = self._day()
        return self._case("retail_brand_performance", f"""
            SELECT p.brand, o.sales_channel, COUNT(DISTINCT o.order_id) AS orders,
                   SUM(i.quantity) AS units, AVG(i.unit_price) AS avg_price
            FROM retail.products p
            JOIN retail.order_items i ON i.product_id = p.product_id
            JOIN retail.customer_orders o ON o.order_id = i.order_id
            WHERE p.brand = '{brand}' AND o.order_status <> 'cancelled'
              AND o.ordered_at >= DATE '{day}' - INTERVAL '365 days'
            GROUP BY p.brand, o.sales_channel ORDER BY units DESC
        """)

    def _regional_sales(self) -> QueryCase:
        region, day = self.random.choice(self.REGIONS), self._day()
        return self._case("retail_regional_sales", f"""
            SELECT a.city, COUNT(DISTINCT o.order_id) AS orders,
                   SUM(o.total_amount) AS gross_sales
            FROM retail.addresses a
            JOIN retail.customer_orders o ON o.shipping_address_id = a.address_id
            WHERE a.region = '{region}'
              AND o.ordered_at >= DATE '{day}' - INTERVAL '180 days'
            GROUP BY a.city ORDER BY gross_sales DESC
        """)

    def _basket_statistics(self) -> QueryCase:
        channel = self.random.choice(self.CHANNELS)
        day = self._day()
        return self._case("retail_basket_statistics", f"""
            SELECT o.sales_channel, COUNT(DISTINCT o.order_id) AS orders,
                   AVG(b.units) AS avg_units, AVG(b.lines) AS avg_lines
            FROM retail.customer_orders o
            JOIN (SELECT order_id, SUM(quantity) AS units, COUNT(*) AS lines
                  FROM retail.order_items GROUP BY order_id) b ON b.order_id = o.order_id
            WHERE o.sales_channel = '{channel}' AND o.order_status <> 'cancelled'
              AND o.ordered_at >= DATE '{day}' - INTERVAL '180 days'
              AND o.ordered_at < DATE '{day}' + INTERVAL '180 days'
            GROUP BY o.sales_channel
        """)

    def _refund_report(self) -> QueryCase:
        day = self._day()
        return self._case("retail_refund_report", f"""
            SELECT p.payment_method, COUNT(*) AS refunds, SUM(p.amount) AS refunded,
                   AVG(o.total_amount) AS avg_order_value
            FROM retail.payments p
            JOIN retail.customer_orders o ON o.order_id = p.order_id
            WHERE p.payment_status = 'refunded'
              AND o.ordered_at >= DATE '{day}' - INTERVAL '365 days'
            GROUP BY p.payment_method ORDER BY refunded DESC
        """)

    def _product_affinity(self) -> QueryCase:
        product_id = self.random.randint(1, 15000)
        return self._case("retail_product_affinity", f"""
            SELECT second.product_id, p.product_name, COUNT(*) AS together_count
            FROM retail.order_items first
            JOIN retail.order_items second
              ON second.order_id = first.order_id AND second.product_id <> first.product_id
            JOIN retail.products p ON p.product_id = second.product_id
            WHERE first.product_id = {product_id}
            GROUP BY second.product_id, p.product_name
            ORDER BY together_count DESC LIMIT 30
        """)

    def _inventory_imbalance(self) -> QueryCase:
        threshold = self.random.randint(80, 300)
        minimum_stock = self.random.randint(10, 150)
        return self._case("retail_inventory_imbalance", f"""
            SELECT product_id, MAX(quantity) - MIN(quantity) AS stock_spread,
                   AVG(quantity) AS avg_stock, SUM(reserved_quantity) AS reserved
            FROM retail.inventory GROUP BY product_id
            HAVING MAX(quantity) - MIN(quantity) > {threshold}
               AND AVG(quantity) >= {minimum_stock}
            ORDER BY stock_spread DESC LIMIT 200
        """)
