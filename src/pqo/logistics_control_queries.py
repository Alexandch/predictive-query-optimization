"""Unseen-database control workload for the logistics schema."""

from __future__ import annotations

from datetime import date, timedelta
import random
from typing import Callable

from .query_case import QueryCase


class LogisticsControlQueryGenerator:
    """Generate zero-shot queries that are never included in model training."""

    TEMPLATE_IDS = (
        "control_logistics_supplier_otif",
        "control_logistics_delivery_percentiles",
        "control_logistics_tracking_gaps",
        "control_logistics_facility_throughput",
        "control_logistics_inventory_balance",
        "control_logistics_order_reconciliation",
        "control_logistics_carrier_efficiency",
        "control_logistics_lane_performance",
        "control_logistics_exception_funnel",
        "control_logistics_item_velocity_class",
        "control_logistics_latest_tracking_event",
        "control_logistics_stockout_risk",
        "control_logistics_supplier_concentration",
        "control_logistics_shipment_value_density",
        "control_logistics_lead_time_outliers",
    )

    def __init__(self, seed: int = 10401) -> None:
        self.random = random.Random(seed)
        self._templates: tuple[Callable[[], QueryCase], ...] = (
            self._supplier_otif,
            self._delivery_percentiles,
            self._tracking_gaps,
            self._facility_throughput,
            self._inventory_balance,
            self._order_reconciliation,
            self._carrier_efficiency,
            self._lane_performance,
            self._exception_funnel,
            self._item_velocity_class,
            self._latest_tracking_event,
            self._stockout_risk,
            self._supplier_concentration,
            self._shipment_value_density,
            self._lead_time_outliers,
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
        return QueryCase(f"control_logistics_{name}", " ".join(sql_text.split()))

    def _supplier_otif(self) -> QueryCase:
        country = self.random.choice(("BY", "PL", "DE", "CN", "TR", "LT", "LV", "KZ"))
        return self._case("supplier_otif", f"""
            SELECT s.supplier_id, s.supplier_name, COUNT(po.purchase_order_id) AS orders,
                   COUNT(*) FILTER (WHERE po.order_status = 'received'
                       AND sh.delivered_at <= po.expected_at) AS on_time_in_full,
                   AVG(EXTRACT(EPOCH FROM (sh.delivered_at - po.ordered_at)) / 3600) AS lead_hours
            FROM logistics.suppliers s
            JOIN logistics.purchase_orders po USING (supplier_id)
            LEFT JOIN logistics.shipments sh USING (purchase_order_id)
            WHERE s.country_code = '{country}'
            GROUP BY s.supplier_id HAVING COUNT(po.purchase_order_id) >= 3
            ORDER BY on_time_in_full DESC, lead_hours LIMIT 200
        """)
    def _delivery_percentiles(self) -> QueryCase:
        service = self.random.choice(("economy", "standard", "express"))
        return self._case("delivery_percentiles", f"""
            SELECT c.service_level,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY sh.delivered_at - sh.shipped_at) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY sh.delivered_at - sh.shipped_at) AS p90,
                   percentile_cont(0.99) WITHIN GROUP (ORDER BY sh.delivered_at - sh.shipped_at) AS p99
            FROM logistics.shipments sh JOIN logistics.carriers c USING (carrier_id)
            WHERE c.service_level = '{service}' AND sh.delivered_at IS NOT NULL
            GROUP BY c.service_level
        """)

    def _tracking_gaps(self) -> QueryCase:
        hours = self.random.choice((12, 24, 36, 48))
        event = self.random.choice(("departed", "arrived", "customs", "exception"))
        return self._case("tracking_gaps", f"""
            WITH timeline AS (
                SELECT shipment_id, event_type, event_time,
                       LAG(event_time) OVER (PARTITION BY shipment_id ORDER BY event_time) AS prior_event
                FROM logistics.tracking_events
            )
            SELECT shipment_id, event_type, event_time, event_time - prior_event AS event_gap
            FROM timeline WHERE event_type = '{event}'
              AND event_time - prior_event > INTERVAL '{hours} hours'
            ORDER BY event_gap DESC LIMIT 300
        """)

    def _facility_throughput(self) -> QueryCase:
        facility = self.random.randint(1, 30)
        width = self.random.choice((6, 13, 29))
        return self._case("facility_throughput", f"""
            WITH daily AS (
                SELECT occurred_at::date AS day,
                       SUM(ABS(quantity_delta)) AS moved_units,
                       COUNT(*) AS movements
                FROM logistics.stock_movements WHERE facility_id = {facility}
                GROUP BY occurred_at::date
            )
            SELECT day, moved_units, movements,
                   SUM(moved_units) OVER (ORDER BY day ROWS BETWEEN {width} PRECEDING AND CURRENT ROW) AS rolling_units,
                   AVG(movements) OVER (ORDER BY day ROWS BETWEEN {width} PRECEDING AND CURRENT ROW) AS rolling_events
            FROM daily ORDER BY day
        """)

    def _inventory_balance(self) -> QueryCase:
        item_class = self.random.choice(("standard", "fragile", "cold", "hazardous"))
        return self._case("inventory_balance", f"""
            WITH balance AS (
                SELECT sm.facility_id, sm.item_id, SUM(sm.quantity_delta) AS on_hand
                FROM logistics.stock_movements sm JOIN logistics.items i USING (item_id)
                WHERE i.item_class = '{item_class}' GROUP BY sm.facility_id, sm.item_id
            )
            SELECT facility_id, COUNT(*) FILTER (WHERE on_hand < 0) AS negative_items,
                   SUM(on_hand) AS net_units, AVG(on_hand) AS avg_units
            FROM balance GROUP BY facility_id ORDER BY negative_items DESC
        """)

    def _order_reconciliation(self) -> QueryCase:
        tolerance = self.random.choice((50, 100, 250, 500))
        return self._case("order_reconciliation", f"""
            SELECT po.purchase_order_id, po.total_value,
                   SUM(pol.ordered_quantity * pol.unit_cost) AS line_value,
                   po.total_value - SUM(pol.ordered_quantity * pol.unit_cost) AS difference
            FROM logistics.purchase_orders po
            JOIN logistics.purchase_order_lines pol USING (purchase_order_id)
            GROUP BY po.purchase_order_id
            HAVING ABS(po.total_value - SUM(pol.ordered_quantity * pol.unit_cost)) > {tolerance}
            ORDER BY ABS(po.total_value - SUM(pol.ordered_quantity * pol.unit_cost)) DESC LIMIT 250
        """)

    def _carrier_efficiency(self) -> QueryCase:
        day = self._day()
        return self._case("carrier_efficiency", f"""
            SELECT c.carrier_id, c.carrier_name, COUNT(*) AS shipments,
                   AVG(sh.shipping_cost) AS avg_cost,
                   AVG(EXTRACT(EPOCH FROM (sh.delivered_at - sh.shipped_at)) / 3600) AS avg_hours,
                   COUNT(*) FILTER (WHERE sh.delivered_at > sh.promised_at)::numeric / COUNT(*) AS late_rate
            FROM logistics.carriers c JOIN logistics.shipments sh USING (carrier_id)
            WHERE sh.shipped_at >= DATE '{day}' - INTERVAL '180 days'
              AND sh.delivered_at IS NOT NULL
            GROUP BY c.carrier_id ORDER BY late_rate, avg_cost
        """)

    def _lane_performance(self) -> QueryCase:
        status = self.random.choice(("picked_up", "in_transit", "delivered", "exception"))
        return self._case("lane_performance", f"""
            SELECT origin_facility_id, destination_facility_id,
                   COUNT(*) AS shipments, AVG(shipping_cost) AS avg_cost,
                   AVG(delivered_at - shipped_at) AS avg_transit
            FROM logistics.shipments WHERE shipment_status = '{status}'
            GROUP BY origin_facility_id, destination_facility_id
            HAVING COUNT(*) >= 20 ORDER BY shipments DESC
        """)

    def _exception_funnel(self) -> QueryCase:
        day = self._day()
        return self._case("exception_funnel", f"""
            WITH cohort AS (
                SELECT shipment_id FROM logistics.shipments
                WHERE shipped_at >= DATE '{day}' - INTERVAL '90 days'
                  AND shipped_at < DATE '{day}' + INTERVAL '90 days'
            )
            SELECT COUNT(DISTINCT c.shipment_id) AS shipments,
                   COUNT(DISTINCT c.shipment_id) FILTER (WHERE e.event_type = 'departed') AS departed,
                   COUNT(DISTINCT c.shipment_id) FILTER (WHERE e.event_type = 'customs') AS customs,
                   COUNT(DISTINCT c.shipment_id) FILTER (WHERE e.event_type = 'exception') AS exceptions,
                   COUNT(DISTINCT c.shipment_id) FILTER (WHERE e.event_type = 'delivered') AS delivered
            FROM cohort c LEFT JOIN logistics.tracking_events e USING (shipment_id)
        """)

    def _item_velocity_class(self) -> QueryCase:
        facility = self.random.randint(1, 30)
        return self._case("item_velocity_class", f"""
            WITH velocity AS (
                SELECT item_id, SUM(ABS(quantity_delta)) AS moved_units
                FROM logistics.stock_movements WHERE facility_id = {facility}
                GROUP BY item_id
            ), classified AS (
                SELECT *, NTILE(3) OVER (ORDER BY moved_units DESC) AS velocity_class
                FROM velocity
            )
            SELECT velocity_class, COUNT(*) AS items, SUM(moved_units) AS units,
                   AVG(moved_units) AS avg_units
            FROM classified GROUP BY velocity_class ORDER BY velocity_class
        """)

    def _latest_tracking_event(self) -> QueryCase:
        facility = self.random.randint(1, 30)
        return self._case("latest_tracking_event", f"""
            SELECT DISTINCT ON (shipment_id) shipment_id, event_type, event_time, facility_id
            FROM logistics.tracking_events WHERE facility_id = {facility}
            ORDER BY shipment_id, event_time DESC
        """)

    def _stockout_risk(self) -> QueryCase:
        days = self.random.choice((30, 60, 90))
        return self._case("stockout_risk", f"""
            WITH balances AS (
                SELECT facility_id, item_id, SUM(quantity_delta) AS on_hand
                FROM logistics.stock_movements GROUP BY facility_id, item_id
            ), demand AS (
                SELECT facility_id, item_id,
                       -SUM(quantity_delta) FILTER (WHERE quantity_delta < 0)::numeric / {days} AS daily_demand
                FROM logistics.stock_movements
                WHERE occurred_at >= timestamptz '2025-12-31 00:00:00+00' - INTERVAL '{days} days'
                GROUP BY facility_id, item_id
            )
            SELECT b.facility_id, b.item_id, b.on_hand, d.daily_demand,
                   b.on_hand / NULLIF(d.daily_demand, 0) AS days_supply
            FROM balances b JOIN demand d USING (facility_id, item_id)
            WHERE b.on_hand / NULLIF(d.daily_demand, 0) < 14
            ORDER BY days_supply LIMIT 300
        """)

    def _supplier_concentration(self) -> QueryCase:
        facility = self.random.randint(1, 30)
        return self._case("supplier_concentration", f"""
            WITH supplier_value AS (
                SELECT supplier_id, SUM(total_value)::numeric AS supplied_value
                FROM logistics.purchase_orders WHERE destination_facility_id = {facility}
                GROUP BY supplier_id
            ), shares AS (
                SELECT *, supplied_value / SUM(supplied_value) OVER () AS share
                FROM supplier_value
            )
            SELECT SUM(share * share) AS hhi, COUNT(*) AS suppliers,
                   MAX(share) AS largest_supplier_share FROM shares
        """)

    def _shipment_value_density(self) -> QueryCase:
        item_class = self.random.choice(("standard", "fragile", "cold", "hazardous"))
        return self._case("shipment_value_density", f"""
            SELECT sh.shipment_id,
                   SUM(si.quantity * i.unit_value) AS cargo_value,
                   SUM(si.quantity * i.unit_weight_kg) AS cargo_weight,
                   SUM(si.quantity * i.unit_value) /
                       NULLIF(SUM(si.quantity * i.unit_weight_kg), 0) AS value_per_kg
            FROM logistics.shipments sh JOIN logistics.shipment_items si USING (shipment_id)
            JOIN logistics.items i USING (item_id)
            WHERE i.item_class = '{item_class}'
            GROUP BY sh.shipment_id ORDER BY value_per_kg DESC LIMIT 250
        """)

    def _lead_time_outliers(self) -> QueryCase:
        priority = self.random.choice(("low", "normal", "high", "critical"))
        return self._case("lead_time_outliers", f"""
            WITH bounds AS (
                SELECT percentile_cont(0.25) WITHIN GROUP (ORDER BY expected_at - ordered_at) AS q1,
                       percentile_cont(0.75) WITHIN GROUP (ORDER BY expected_at - ordered_at) AS q3
                FROM logistics.purchase_orders WHERE priority = '{priority}'
            )
            SELECT po.purchase_order_id, po.supplier_id,
                   po.expected_at - po.ordered_at AS planned_lead_time
            FROM logistics.purchase_orders po CROSS JOIN bounds b
            WHERE po.priority = '{priority}'
              AND po.expected_at - po.ordered_at > b.q3 + 1.5 * (b.q3 - b.q1)
            ORDER BY planned_lead_time DESC LIMIT 250
        """)
