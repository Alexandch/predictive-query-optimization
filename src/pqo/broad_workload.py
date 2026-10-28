"""Fresh family-disjoint workload for first-prediction experiments.

Split membership is fixed here, before any measurements. Previously sealed
logistics/CH controls and user examples are deliberately not reused.
"""

from .query_case import QueryCase


def broad_workload():
    for bound in (18000, 85000, None):
        rf = "true" if bound is None else f"o.order_id <= {bound}"
        af = "true" if bound is None else f"f.flight_id <= {bound // 6}"
        retail = f"""SELECT o.order_id, o.customer_id, oi.product_id,
            p.category_id, p.brand, oi.quantity, oi.unit_price
            FROM retail.customer_orders o JOIN retail.order_items oi USING(order_id)
            JOIN retail.products p USING(product_id)
            WHERE ({rf}) AND o.order_status <> 'cancelled'"""
        air = f"""SELECT f.flight_id, f.departure_airport, f.arrival_airport,
            f.aircraft_code, f.scheduled_departure, tf.ticket_no, tf.amount,
            tf.fare_conditions FROM aviation.flights f
            JOIN aviation.ticket_flights tf USING(flight_id) WHERE {af}"""
        families = {
            "train_retail_grouping_sets": f"""WITH grouped AS (
                SELECT category_id, brand, GROUPING(brand) level,
                    SUM(quantity*unit_price) revenue, COUNT(DISTINCT customer_id) customers
                FROM ({retail}) src GROUP BY GROUPING SETS ((category_id,brand),(category_id)))
                SELECT *, RANK() OVER(PARTITION BY level ORDER BY revenue DESC) r
                FROM grouped ORDER BY revenue DESC LIMIT 150""",
            "train_air_rollup": f"""WITH grouped AS (
                SELECT departure_airport, fare_conditions, GROUPING(fare_conditions) level,
                    SUM(amount) revenue, COUNT(DISTINCT ticket_no) tickets
                FROM ({air}) src GROUP BY ROLLUP(departure_airport,fare_conditions))
                SELECT *, SUM(revenue) OVER(PARTITION BY level) total
                FROM grouped ORDER BY revenue DESC LIMIT 150""",
            "train_retail_customer_frames": f"""WITH grouped AS (
                SELECT customer_id, order_id, SUM(quantity*unit_price) revenue,
                    COUNT(DISTINCT product_id) products FROM ({retail}) src
                GROUP BY customer_id,order_id)
                SELECT *, SUM(revenue) OVER(PARTITION BY customer_id ORDER BY order_id
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) recent,
                    LAG(revenue,2) OVER(PARTITION BY customer_id ORDER BY order_id) previous
                FROM grouped ORDER BY recent DESC LIMIT 150""",
            "train_air_flight_frames": f"""WITH grouped AS (
                SELECT aircraft_code, flight_id, scheduled_departure, SUM(amount) revenue,
                    COUNT(DISTINCT ticket_no) tickets FROM ({air}) src
                GROUP BY aircraft_code,flight_id,scheduled_departure)
                SELECT *, AVG(revenue) OVER(PARTITION BY aircraft_code ORDER BY scheduled_departure
                    ROWS BETWEEN 6 PRECEDING AND 2 FOLLOWING) moving,
                    NTILE(7) OVER(PARTITION BY aircraft_code ORDER BY revenue) bucket
                FROM grouped ORDER BY moving DESC LIMIT 150""",
            "train_retail_filtered_stats": f"""WITH grouped AS (
                SELECT brand, category_id, COUNT(DISTINCT order_id) orders,
                    SUM(quantity*unit_price) FILTER(WHERE quantity > 1) bulk,
                    STDDEV_POP(unit_price) deviation,
                    PERCENTILE_CONT(0.65) WITHIN GROUP(ORDER BY unit_price) price
                FROM ({retail}) src GROUP BY brand,category_id)
                SELECT *, DENSE_RANK() OVER(PARTITION BY category_id ORDER BY bulk DESC) r
                FROM grouped ORDER BY price DESC LIMIT 150""",
            "train_air_filtered_stats": f"""WITH grouped AS (
                SELECT arrival_airport, aircraft_code, COUNT(DISTINCT ticket_no) tickets,
                    SUM(amount) FILTER(WHERE fare_conditions='Business') premium,
                    STDDEV_POP(amount) deviation,
                    PERCENTILE_CONT(0.65) WITHIN GROUP(ORDER BY amount) price
                FROM ({air}) src GROUP BY arrival_airport,aircraft_code)
                SELECT *, DENSE_RANK() OVER(PARTITION BY aircraft_code ORDER BY premium DESC) r
                FROM grouped ORDER BY price DESC LIMIT 150""",
            "train_retail_materialized_join": f"""WITH src AS MATERIALIZED ({retail}),
                customer AS (SELECT customer_id,SUM(quantity*unit_price) revenue FROM src GROUP BY customer_id),
                product AS (SELECT product_id,AVG(unit_price) price FROM src GROUP BY product_id)
                SELECT s.category_id,COUNT(DISTINCT s.customer_id) customers,
                    SUM(c.revenue) weighted,AVG(p.price) price
                FROM src s JOIN customer c USING(customer_id) JOIN product p USING(product_id)
                GROUP BY s.category_id ORDER BY weighted DESC LIMIT 150""",
            "train_air_materialized_join": f"""WITH src AS MATERIALIZED ({air}),
                route AS (SELECT departure_airport,arrival_airport,SUM(amount) revenue FROM src
                    GROUP BY departure_airport,arrival_airport),
                aircraft AS (SELECT aircraft_code,AVG(amount) price FROM src GROUP BY aircraft_code)
                SELECT s.fare_conditions,COUNT(DISTINCT s.ticket_no) tickets,
                    SUM(r.revenue) weighted,AVG(a.price) price FROM src s
                JOIN route r USING(departure_airport,arrival_airport)
                JOIN aircraft a USING(aircraft_code)
                GROUP BY s.fare_conditions ORDER BY weighted DESC LIMIT 150""",
            "validation_retail_product_deviation": f"""WITH grouped AS (
                SELECT category_id,product_id,COUNT(DISTINCT order_id) orders,
                    SUM(quantity*unit_price) revenue,VAR_POP(unit_price) variance
                FROM ({retail}) src GROUP BY category_id,product_id)
                SELECT *, CUME_DIST() OVER(PARTITION BY category_id ORDER BY revenue) distribution,
                    AVG(revenue) OVER(PARTITION BY category_id) average
                FROM grouped ORDER BY revenue DESC LIMIT 175""",
            "validation_air_route_deviation": f"""WITH grouped AS (
                SELECT aircraft_code,departure_airport,arrival_airport,
                    COUNT(DISTINCT ticket_no) tickets,SUM(amount) revenue,VAR_POP(amount) variance
                FROM ({air}) src GROUP BY aircraft_code,departure_airport,arrival_airport)
                SELECT *, CUME_DIST() OVER(PARTITION BY aircraft_code ORDER BY revenue) distribution,
                    AVG(revenue) OVER(PARTITION BY aircraft_code) average
                FROM grouped ORDER BY revenue DESC LIMIT 175""",
            "validation_retail_cube": f"""SELECT category_id,brand,GROUPING(category_id,brand) level,
                COUNT(DISTINCT product_id) products,SUM(quantity*unit_price) revenue,
                PERCENTILE_CONT(0.35) WITHIN GROUP(ORDER BY unit_price) price
                FROM ({retail}) src GROUP BY CUBE(category_id,brand)
                ORDER BY revenue DESC LIMIT 175""",
            "validation_air_day_frame": f"""WITH grouped AS (
                SELECT aircraft_code,date_trunc('day',scheduled_departure) AS day,
                    SUM(amount) revenue,COUNT(DISTINCT flight_id) flights FROM ({air}) src
                GROUP BY aircraft_code,day)
                SELECT *, SUM(revenue) OVER(PARTITION BY aircraft_code ORDER BY day
                    RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW) recent
                FROM grouped ORDER BY recent DESC LIMIT 175""",
            "acceptance_retail_customer_rollup": f"""WITH grouped AS (
                SELECT customer_id,category_id,GROUPING(category_id) level,
                    SUM(quantity*unit_price) revenue,COUNT(DISTINCT product_id) products
                FROM ({retail}) src GROUP BY ROLLUP(customer_id,category_id))
                SELECT *, PERCENT_RANK() OVER(PARTITION BY level ORDER BY revenue) percentile
                FROM grouped ORDER BY revenue DESC LIMIT 125""",
            "acceptance_air_fare_cube": f"""SELECT departure_airport,aircraft_code,
                GROUPING(departure_airport,aircraft_code) level,
                COUNT(DISTINCT ticket_no) tickets,SUM(amount) revenue,
                PERCENTILE_CONT(0.85) WITHIN GROUP(ORDER BY amount) price
                FROM ({air}) src GROUP BY CUBE(departure_airport,aircraft_code)
                ORDER BY revenue DESC LIMIT 125""",
            "acceptance_retail_stock_deviation": f"""WITH sales AS (
                SELECT product_id, SUM(quantity*unit_price) revenue,
                    COUNT(DISTINCT customer_id) customers FROM ({retail}) src GROUP BY product_id),
                stock AS (SELECT product_id,SUM(quantity-reserved_quantity) available,
                    STDDEV_POP(quantity) dispersion FROM retail.inventory GROUP BY product_id)
                SELECT p.brand,p.product_id,s.*,st.available,st.dispersion,
                    PERCENT_RANK() OVER(PARTITION BY p.brand ORDER BY revenue) percentile
                FROM retail.products p JOIN sales s USING(product_id) JOIN stock st USING(product_id)
                ORDER BY revenue DESC LIMIT 125""",
            "acceptance_air_month_range": f"""WITH grouped AS (
                SELECT arrival_airport,date_trunc('month',scheduled_departure) AS month,
                    SUM(amount) revenue,COUNT(DISTINCT ticket_no) tickets,
                    PERCENTILE_CONT(0.4) WITHIN GROUP(ORDER BY amount) price
                FROM ({air}) src GROUP BY arrival_airport,month)
                SELECT *, AVG(revenue) OVER(PARTITION BY arrival_airport ORDER BY month
                    RANGE BETWEEN INTERVAL '2 months' PRECEDING AND CURRENT ROW) moving,
                    LEAD(revenue) OVER(PARTITION BY arrival_airport ORDER BY month) following
                FROM grouped ORDER BY moving DESC LIMIT 125""",
        }
        for name, sql in families.items():
            yield name.split("_", 1)[0], QueryCase("broad_" + name, sql.strip())
