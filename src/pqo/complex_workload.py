"""Preassigned development workload; no sealed controls or user SQL."""

from .query_case import QueryCase


def complex_workload():
    for position, bound in enumerate((50000, 100000, 200000, 400000, None)):
        orders = "true" if bound is None else f"o.order_id <= {bound}"
        flights = "true" if bound is None else f"f.flight_id <= {bound // 8}"
        limit = (100, 200, 300, 400, 500)[position]
        retail = f"""SELECT oi.product_id, o.customer_id, o.order_id,
            oi.quantity, oi.unit_price, p.category_id, p.brand
            FROM retail.order_items oi JOIN retail.customer_orders o USING(order_id)
            JOIN retail.products p USING(product_id)
            WHERE ({orders}) AND o.order_status IN ('paid','processing','shipped','delivered')"""
        aviation = f"""SELECT f.flight_id, f.aircraft_code, f.departure_airport,
            f.arrival_airport, f.scheduled_departure, tf.ticket_no, tf.amount, tf.fare_conditions
            FROM aviation.flights f JOIN aviation.ticket_flights tf USING(flight_id)
            WHERE {flights}"""
        families = {
            "train_product_percentiles": f"""
                WITH grouped AS (SELECT category_id, product_id, COUNT(DISTINCT order_id) orders,
                    SUM(quantity * unit_price) revenue,
                    PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY unit_price) median_price,
                    PERCENTILE_CONT(0.9) WITHIN GROUP(ORDER BY unit_price) upper_price
                    FROM ({retail}) source GROUP BY category_id, product_id)
                SELECT *, DENSE_RANK() OVER(PARTITION BY category_id ORDER BY revenue DESC) rank,
                    AVG(revenue) OVER(PARTITION BY category_id) average_revenue
                FROM grouped ORDER BY revenue DESC LIMIT {limit}
            """,
            "train_customer_percentiles": f"""
                WITH grouped AS (SELECT customer_id, COUNT(DISTINCT product_id) products,
                    COUNT(DISTINCT order_id) orders, SUM(quantity * unit_price) revenue,
                    PERCENTILE_CONT(0.75) WITHIN GROUP(ORDER BY unit_price) price
                    FROM ({retail}) source GROUP BY customer_id)
                SELECT *, NTILE(10) OVER(ORDER BY revenue DESC) bucket,
                    SUM(revenue) OVER(ORDER BY revenue DESC ROWS UNBOUNDED PRECEDING) cumulative
                FROM grouped ORDER BY revenue DESC LIMIT {limit}
            """,
            "train_brand_windows": f"""
                WITH grouped AS (SELECT category_id, brand, COUNT(DISTINCT customer_id) customers,
                    SUM(quantity * unit_price) revenue,
                    PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY unit_price) median_price
                    FROM ({retail}) source GROUP BY category_id, brand)
                SELECT *, RANK() OVER(PARTITION BY category_id ORDER BY revenue DESC) rank,
                    SUM(revenue) OVER(PARTITION BY category_id) total
                FROM grouped ORDER BY revenue DESC LIMIT {limit}
            """,
            "train_stock_product_windows": f"""
                WITH sales AS (SELECT product_id, SUM(quantity * unit_price) revenue,
                    COUNT(DISTINCT customer_id) customers FROM ({retail}) source GROUP BY product_id),
                stock AS (SELECT product_id, SUM(quantity - reserved_quantity) available,
                    COUNT(DISTINCT warehouse_id) warehouses FROM retail.inventory GROUP BY product_id)
                SELECT p.category_id, p.product_id, s.revenue, st.available,
                    AVG(s.revenue) OVER(PARTITION BY p.category_id) average_revenue,
                    DENSE_RANK() OVER(PARTITION BY p.brand ORDER BY s.revenue DESC) rank
                FROM retail.products p JOIN sales s USING(product_id) LEFT JOIN stock st USING(product_id)
                ORDER BY s.revenue DESC LIMIT {limit}
            """,
            "train_materialized_reuse": f"""
                WITH source AS MATERIALIZED ({retail}),
                brands AS (SELECT category_id, brand, SUM(quantity * unit_price) revenue,
                    COUNT(DISTINCT order_id) orders FROM source GROUP BY category_id, brand),
                totals AS (SELECT category_id, COUNT(DISTINCT customer_id) customers,
                    SUM(quantity * unit_price) total FROM source GROUP BY category_id)
                SELECT b.*, t.customers, b.revenue / NULLIF(t.total,0) share,
                    DENSE_RANK() OVER(PARTITION BY b.category_id ORDER BY b.revenue DESC) rank
                FROM brands b JOIN totals t USING(category_id) ORDER BY b.revenue DESC LIMIT {limit}
            """,
            "train_airport_percentiles": f"""
                WITH grouped AS (SELECT departure_airport, arrival_airport,
                    COUNT(DISTINCT ticket_no) passengers, SUM(amount) revenue,
                    PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY amount) median_amount
                    FROM ({aviation}) source GROUP BY departure_airport, arrival_airport)
                SELECT *, RANK() OVER(PARTITION BY departure_airport ORDER BY revenue DESC) rank,
                    AVG(revenue) OVER(PARTITION BY departure_airport) average_revenue
                FROM grouped ORDER BY revenue DESC LIMIT {limit}
            """,
            "train_fare_month_windows": f"""
                WITH grouped AS (SELECT fare_conditions, date_trunc('month',scheduled_departure) AS month,
                    COUNT(DISTINCT ticket_no) passengers, SUM(amount) revenue,
                    PERCENTILE_CONT(0.9) WITHIN GROUP(ORDER BY amount) upper_amount
                    FROM ({aviation}) source GROUP BY fare_conditions, month)
                SELECT *, SUM(revenue) OVER(PARTITION BY fare_conditions ORDER BY month) running,
                    LAG(revenue) OVER(PARTITION BY fare_conditions ORDER BY month) previous
                FROM grouped ORDER BY revenue DESC LIMIT {limit}
            """,
            "train_reused_flight_totals": f"""
                WITH source AS MATERIALIZED ({aviation}),
                per_flight AS (SELECT departure_airport, flight_id, SUM(amount) revenue,
                    COUNT(DISTINCT ticket_no) passengers FROM source GROUP BY departure_airport,flight_id),
                totals AS (SELECT departure_airport, SUM(amount) total,
                    COUNT(DISTINCT ticket_no) passengers FROM source GROUP BY departure_airport)
                SELECT p.*, t.total, p.revenue / NULLIF(t.total,0) share,
                    ROW_NUMBER() OVER(PARTITION BY p.departure_airport ORDER BY p.revenue DESC) rank
                FROM per_flight p JOIN totals t USING(departure_airport)
                ORDER BY p.revenue DESC LIMIT {limit}
            """,
            "validation_category_percentiles": f"""
                WITH grouped AS (SELECT category_id, COUNT(DISTINCT customer_id) customers,
                    SUM(quantity * unit_price) revenue,
                    PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY unit_price) median_price,
                    PERCENTILE_CONT(0.95) WITHIN GROUP(ORDER BY unit_price) upper_price
                    FROM ({retail}) source GROUP BY category_id)
                SELECT *, PERCENT_RANK() OVER(ORDER BY revenue) percentile
                FROM grouped ORDER BY revenue DESC LIMIT {limit}
            """,
            "validation_aircraft_month": f"""
                WITH grouped AS (SELECT aircraft_code, date_trunc('month',scheduled_departure) AS month,
                    SUM(amount) revenue, COUNT(DISTINCT ticket_no) passengers,
                    PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY amount) median_amount
                    FROM ({aviation}) source GROUP BY aircraft_code, month)
                SELECT *, DENSE_RANK() OVER(PARTITION BY month ORDER BY revenue DESC) rank,
                    AVG(revenue) OVER(PARTITION BY aircraft_code) average_revenue
                FROM grouped ORDER BY revenue DESC LIMIT {limit}
            """,
            "acceptance_customer_category": f"""
                WITH grouped AS (SELECT category_id, customer_id, COUNT(DISTINCT order_id) orders,
                    SUM(quantity * unit_price) revenue,
                    PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY unit_price) median_price
                    FROM ({retail}) source GROUP BY category_id,customer_id),
                category_stock AS (SELECT p.category_id, SUM(i.quantity - i.reserved_quantity) available
                    FROM retail.products p JOIN retail.inventory i USING(product_id) GROUP BY p.category_id),
                ranked AS (SELECT g.*, st.available,
                    DENSE_RANK() OVER(PARTITION BY category_id ORDER BY revenue DESC) rank,
                    SUM(revenue) OVER(PARTITION BY category_id ORDER BY revenue DESC,customer_id
                        ROWS UNBOUNDED PRECEDING) cumulative
                    FROM grouped g LEFT JOIN category_stock st USING(category_id))
                SELECT * FROM ranked WHERE rank <= 30 ORDER BY revenue DESC LIMIT {limit}
            """,
            "acceptance_airport_daily": f"""
                WITH grouped AS (SELECT departure_airport, arrival_airport,
                    date_trunc('day',scheduled_departure) AS day, COUNT(DISTINCT ticket_no) passengers,
                    SUM(amount) revenue, PERCENTILE_CONT(0.75) WITHIN GROUP(ORDER BY amount) price
                    FROM ({aviation}) source GROUP BY departure_airport,arrival_airport,day),
                ranked AS (SELECT *, ROW_NUMBER() OVER(PARTITION BY departure_airport,day ORDER BY revenue DESC) rank,
                    SUM(revenue) OVER(PARTITION BY arrival_airport ORDER BY day) cumulative
                    FROM grouped)
                SELECT * FROM ranked WHERE rank <= 10 ORDER BY revenue DESC LIMIT {limit}
            """,
        }
        for name, sql in families.items():
            yield name.split("_", 1)[0], QueryCase("complex_" + name, sql.strip())
