"""Preassigned SQL-v3 experiment: new reports, never old held-out SQL."""

from .query_case import QueryCase


def structural_workload():
    for bound in (12000, 67000, None):
        rf = "true" if bound is None else f"o.order_id <= {bound}"
        af = "true" if bound is None else f"f.flight_id <= {bound // 5}"
        retail = f"""SELECT p.category_id,p.brand,oi.product_id,o.customer_id,
            date_trunc('month',o.ordered_at) AS period,oi.quantity,
            oi.unit_price,oi.quantity*oi.unit_price AS value
            FROM retail.order_items oi JOIN retail.customer_orders o USING(order_id)
            JOIN retail.products p USING(product_id)
            WHERE ({rf}) AND o.order_status IN ('paid','shipped','delivered')"""
        air = f"""SELECT f.departure_airport,f.arrival_airport,f.aircraft_code,
            date_trunc('month',f.scheduled_departure) AS period,
            tf.ticket_no,tf.fare_conditions,tf.amount AS value
            FROM aviation.ticket_flights tf JOIN aviation.flights f USING(flight_id)
            WHERE ({af}) AND tf.amount > 1000"""
        families = {
            "train_retail_period_sets": f"""SELECT category_id,period,
                GROUPING(category_id,period) AS level,SUM(value) AS revenue,
                COUNT(DISTINCT customer_id) AS customers,
                percentile_cont(0.7) WITHIN GROUP(ORDER BY value) AS price
                FROM ({retail}) src GROUP BY GROUPING SETS((category_id,period),(period),())
                ORDER BY revenue DESC LIMIT 160""",
            "train_air_period_cube": f"""SELECT aircraft_code,period,
                GROUPING(aircraft_code,period) AS level,SUM(value) AS revenue,
                COUNT(DISTINCT ticket_no) AS tickets,
                percentile_cont(0.7) WITHIN GROUP(ORDER BY value) AS price
                FROM ({air}) src GROUP BY CUBE(aircraft_code,period)
                ORDER BY revenue DESC LIMIT 160""",
            "train_retail_peer_frames": f"""WITH g AS (
                SELECT category_id,product_id,SUM(value) AS revenue FROM ({retail}) src
                GROUP BY category_id,product_id)
                SELECT *,SUM(revenue) OVER(PARTITION BY category_id ORDER BY revenue
                    GROUPS BETWEEN 2 PRECEDING AND 2 FOLLOWING EXCLUDE CURRENT ROW) AS peers,
                    AVG(revenue) OVER(PARTITION BY category_id ORDER BY revenue
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running
                FROM g ORDER BY peers DESC NULLS LAST LIMIT 160""",
            "train_air_period_range": f"""WITH g AS (
                SELECT aircraft_code,period,SUM(value) AS revenue,STDDEV_POP(value) AS deviation
                FROM ({air}) src GROUP BY aircraft_code,period)
                SELECT *,SUM(revenue) OVER(PARTITION BY aircraft_code ORDER BY period
                    RANGE BETWEEN INTERVAL '3 months' PRECEDING AND INTERVAL '1 month' FOLLOWING) AS neighborhood,
                    AVG(revenue) OVER(PARTITION BY aircraft_code ORDER BY period
                    ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING EXCLUDE TIES) AS moving
                FROM g ORDER BY neighborhood DESC LIMIT 160""",
            "validation_retail_brand_sets": f"""WITH g AS (
                SELECT brand,period,GROUPING(brand,period) AS level,SUM(value) AS revenue,
                    COUNT(DISTINCT product_id) AS products FROM ({retail}) src
                GROUP BY GROUPING SETS((brand,period),(brand),()))
                SELECT *,RANK() OVER(PARTITION BY level ORDER BY revenue DESC) AS rank
                FROM g ORDER BY revenue DESC LIMIT 140""",
            "validation_air_arrival_rollup": f"""WITH g AS (
                SELECT arrival_airport,period,GROUPING(arrival_airport,period) AS level,
                    SUM(value) AS revenue,COUNT(DISTINCT ticket_no) AS tickets,
                    percentile_cont(0.3) WITHIN GROUP(ORDER BY value) AS price
                FROM ({air}) src GROUP BY ROLLUP(arrival_airport,period))
                SELECT *,SUM(revenue) OVER(PARTITION BY level) AS total
                FROM g ORDER BY revenue DESC LIMIT 140""",
            "validation_retail_customer_peers": f"""WITH g AS (
                SELECT brand,customer_id,SUM(value) AS revenue FROM ({retail}) src
                GROUP BY brand,customer_id)
                SELECT *,AVG(revenue) OVER(PARTITION BY brand ORDER BY revenue
                    GROUPS BETWEEN 3 PRECEDING AND 1 FOLLOWING EXCLUDE TIES) AS peers,
                    SUM(revenue) OVER(PARTITION BY brand ORDER BY revenue
                    ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS remaining
                FROM g ORDER BY revenue DESC LIMIT 140""",
            "validation_air_fare_range": f"""WITH g AS (
                SELECT fare_conditions,period,SUM(value) AS revenue,
                    percentile_cont(0.2) WITHIN GROUP(ORDER BY value) AS low_price
                FROM ({air}) src GROUP BY fare_conditions,period)
                SELECT *,AVG(revenue) OVER(PARTITION BY fare_conditions ORDER BY period
                    RANGE BETWEEN INTERVAL '1 month' PRECEDING AND INTERVAL '2 months' FOLLOWING) AS moving
                FROM g ORDER BY moving DESC LIMIT 140""",
            "acceptance_retail_two_stage_sets": f"""WITH monthly AS (
                SELECT category_id,brand,period,SUM(value) AS revenue,
                    COUNT(DISTINCT customer_id) AS customers FROM ({retail}) src
                GROUP BY category_id,brand,period)
                SELECT category_id,period,GROUPING(category_id,period) AS level,
                    SUM(revenue) AS revenue,AVG(customers) AS customers,
                    percentile_cont(0.8) WITHIN GROUP(ORDER BY revenue) AS upper_revenue
                FROM monthly GROUP BY GROUPING SETS((category_id,period),(category_id),())
                ORDER BY revenue DESC LIMIT 180""",
            "acceptance_air_two_stage_rollup": f"""WITH monthly AS (
                SELECT departure_airport,aircraft_code,period,SUM(value) AS revenue,
                    COUNT(DISTINCT ticket_no) AS tickets FROM ({air}) src
                GROUP BY departure_airport,aircraft_code,period)
                SELECT departure_airport,period,GROUPING(departure_airport,period) AS level,
                    SUM(revenue) AS revenue,STDDEV_POP(revenue) AS dispersion,
                    percentile_cont(0.8) WITHIN GROUP(ORDER BY revenue) AS upper_revenue
                FROM monthly GROUP BY ROLLUP(departure_airport,period)
                ORDER BY revenue DESC LIMIT 180""",
            "acceptance_retail_ranked_stock": f"""WITH g AS (
                SELECT category_id,product_id,SUM(value) AS revenue FROM ({retail}) src
                GROUP BY category_id,product_id),
                stock AS (SELECT product_id,SUM(quantity-reserved_quantity) AS available
                    FROM retail.inventory GROUP BY product_id),
                ranked AS (SELECT g.*,st.available,
                    SUM(revenue) OVER(PARTITION BY category_id ORDER BY revenue
                    GROUPS BETWEEN 1 PRECEDING AND 2 FOLLOWING EXCLUDE GROUP) AS peers
                    FROM g JOIN stock st USING(product_id))
                SELECT * FROM ranked WHERE available > 0 ORDER BY peers DESC NULLS LAST LIMIT 180""",
            "acceptance_air_route_excluded_frame": f"""WITH g AS (
                SELECT departure_airport,arrival_airport,period,SUM(value) AS revenue,
                    percentile_cont(0.6) WITHIN GROUP(ORDER BY value) AS price
                FROM ({air}) src GROUP BY departure_airport,arrival_airport,period)
                SELECT *,AVG(revenue) OVER(PARTITION BY departure_airport,arrival_airport
                    ORDER BY period RANGE BETWEEN INTERVAL '4 months' PRECEDING
                    AND CURRENT ROW EXCLUDE CURRENT ROW) AS prior_average,
                    DENSE_RANK() OVER(PARTITION BY period ORDER BY revenue DESC) AS rank
                FROM g ORDER BY revenue DESC LIMIT 180""",
        }
        for name, sql in families.items():
            yield name.split("_", 1)[0], QueryCase("structural_" + name, sql.strip())
