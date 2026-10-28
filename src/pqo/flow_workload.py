"""Fixed, fresh reports for testing estimated operator-input features."""

from .query_case import QueryCase


def flow_workload():
    for bound in (1600, 9500, 38000):
        retail = f"""SELECT o.order_id,o.customer_id,oi.product_id,p.category_id,p.brand,
            oi.quantity,oi.unit_price,oi.quantity*oi.unit_price AS value
            FROM retail.order_items oi JOIN retail.customer_orders o USING(order_id)
            JOIN retail.products p USING(product_id)
            WHERE o.order_id <= {bound * 3} AND o.order_status <> 'cancelled'"""
        air = f"""SELECT f.flight_id,f.departure_airport,f.arrival_airport,f.aircraft_code,
            tf.ticket_no,tf.fare_conditions,tf.amount AS value
            FROM aviation.flights f JOIN aviation.ticket_flights tf USING(flight_id)
            WHERE f.flight_id <= {bound} AND tf.amount >= 500"""
        families = {
            "train_retail_raw_windows": f"""WITH w AS (
                SELECT *,SUM(value) OVER(PARTITION BY customer_id ORDER BY value,product_id
                    ROWS UNBOUNDED PRECEDING) AS running,
                    DENSE_RANK() OVER(PARTITION BY category_id ORDER BY value DESC) AS price_rank
                FROM ({retail}) src)
                SELECT category_id,AVG(running) AS average_running,MAX(price_rank) AS max_rank,
                    SUM(value) AS revenue FROM w GROUP BY category_id
                ORDER BY revenue DESC LIMIT 120""",
            "train_air_raw_windows": f"""WITH w AS (
                SELECT *,AVG(value) OVER(PARTITION BY departure_airport ORDER BY value,ticket_no
                    ROWS BETWEEN 8 PRECEDING AND CURRENT ROW) AS moving,
                    RANK() OVER(PARTITION BY fare_conditions ORDER BY value DESC) AS fare_rank
                FROM ({air}) src)
                SELECT aircraft_code,AVG(moving) AS moving,MAX(fare_rank) AS max_rank,
                    SUM(value) AS revenue FROM w GROUP BY aircraft_code
                ORDER BY revenue DESC LIMIT 120""",
            "train_retail_reduced_windows": f"""WITH g AS (
                SELECT customer_id,category_id,SUM(value) AS revenue,COUNT(*) AS lines
                FROM ({retail}) src GROUP BY customer_id,category_id)
                SELECT *,AVG(revenue) OVER(PARTITION BY category_id ORDER BY lines
                    ROWS BETWEEN 3 PRECEDING AND 3 FOLLOWING) AS moving,
                    SUM(revenue) OVER(PARTITION BY customer_id) AS customer_total
                FROM g ORDER BY moving DESC LIMIT 120""",
            "train_air_reduced_windows": f"""WITH g AS (
                SELECT arrival_airport,flight_id,SUM(value) AS revenue,COUNT(*) AS tickets,
                    percentile_cont(0.55) WITHIN GROUP(ORDER BY value) AS price
                FROM ({air}) src GROUP BY arrival_airport,flight_id)
                SELECT *,SUM(revenue) OVER(PARTITION BY arrival_airport ORDER BY tickets,flight_id
                    ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS moving
                FROM g ORDER BY moving DESC LIMIT 120""",
            "validation_retail_brand_raw": f"""WITH w AS (
                SELECT *,AVG(value) OVER(PARTITION BY brand ORDER BY order_id,product_id
                    ROWS BETWEEN 10 PRECEDING AND 1 FOLLOWING) AS moving,
                    SUM(value) OVER(PARTITION BY customer_id ORDER BY value
                    RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative
                FROM ({retail}) src)
                SELECT brand,AVG(moving) AS moving,MAX(cumulative) AS peak,
                    SUM(value) AS revenue FROM w GROUP BY brand
                ORDER BY peak DESC LIMIT 130""",
            "validation_air_route_raw": f"""WITH w AS (
                SELECT *,SUM(value) OVER(PARTITION BY departure_airport,arrival_airport
                    ORDER BY value,ticket_no ROWS UNBOUNDED PRECEDING) AS cumulative,
                    DENSE_RANK() OVER(PARTITION BY aircraft_code ORDER BY value) AS price_rank
                FROM ({air}) src)
                SELECT fare_conditions,MAX(cumulative) AS peak,AVG(price_rank) AS average_rank
                FROM w GROUP BY fare_conditions ORDER BY peak DESC LIMIT 130""",
            "validation_retail_two_levels": f"""WITH g AS (
                SELECT brand,product_id,SUM(value) AS revenue,COUNT(DISTINCT customer_id) AS customers
                FROM ({retail}) src GROUP BY brand,product_id),
                w AS (SELECT *,SUM(revenue) OVER(PARTITION BY brand ORDER BY customers,product_id
                    ROWS UNBOUNDED PRECEDING) AS cumulative FROM g)
                SELECT brand,SUM(revenue) AS revenue,AVG(cumulative) AS average_cumulative
                FROM w GROUP BY brand ORDER BY revenue DESC LIMIT 130""",
            "validation_air_two_levels": f"""WITH g AS (
                SELECT departure_airport,aircraft_code,COUNT(DISTINCT ticket_no) AS tickets,
                    SUM(value) AS revenue FROM ({air}) src GROUP BY departure_airport,aircraft_code),
                w AS (SELECT *,AVG(revenue) OVER(PARTITION BY aircraft_code ORDER BY tickets
                    GROUPS BETWEEN 1 PRECEDING AND 1 FOLLOWING) AS peers FROM g)
                SELECT departure_airport,SUM(revenue) AS revenue,MAX(peers) AS peak
                FROM w GROUP BY departure_airport ORDER BY peak DESC LIMIT 130""",
            "acceptance_retail_category_raw": f"""WITH w AS (
                SELECT *,SUM(value) OVER(PARTITION BY category_id ORDER BY unit_price,order_id
                    ROWS BETWEEN 7 PRECEDING AND 2 FOLLOWING) AS neighborhood,
                    AVG(quantity) OVER(PARTITION BY product_id ORDER BY value
                    RANGE UNBOUNDED PRECEDING) AS average_quantity
                FROM ({retail}) src)
                SELECT customer_id,MAX(neighborhood) AS peak,AVG(average_quantity) AS average_quantity,
                    SUM(value) AS revenue FROM w GROUP BY customer_id
                ORDER BY peak DESC LIMIT 140""",
            "acceptance_air_fare_raw": f"""WITH w AS (
                SELECT *,AVG(value) OVER(PARTITION BY fare_conditions ORDER BY flight_id,ticket_no
                    ROWS BETWEEN 9 PRECEDING AND 3 FOLLOWING) AS neighborhood,
                    SUM(value) OVER(PARTITION BY arrival_airport ORDER BY value
                    RANGE UNBOUNDED PRECEDING) AS cumulative
                FROM ({air}) src)
                SELECT departure_airport,MAX(neighborhood) AS peak,AVG(cumulative) AS average_cumulative
                FROM w GROUP BY departure_airport ORDER BY peak DESC LIMIT 140""",
            "acceptance_retail_two_stage_rank": f"""WITH g AS (
                SELECT category_id,brand,customer_id,SUM(value) AS revenue,
                    COUNT(DISTINCT product_id) AS products FROM ({retail}) src
                GROUP BY category_id,brand,customer_id),
                w AS (SELECT *,DENSE_RANK() OVER(PARTITION BY brand ORDER BY revenue DESC) AS rank,
                    SUM(revenue) OVER(PARTITION BY category_id ORDER BY products,customer_id
                    ROWS UNBOUNDED PRECEDING) AS cumulative FROM g)
                SELECT category_id,AVG(rank) AS average_rank,MAX(cumulative) AS peak,
                    SUM(revenue) AS revenue FROM w GROUP BY category_id
                ORDER BY peak DESC LIMIT 140""",
            "acceptance_air_two_stage_rank": f"""WITH g AS (
                SELECT aircraft_code,arrival_airport,fare_conditions,SUM(value) AS revenue,
                    COUNT(DISTINCT ticket_no) AS tickets FROM ({air}) src
                GROUP BY aircraft_code,arrival_airport,fare_conditions),
                w AS (SELECT *,RANK() OVER(PARTITION BY arrival_airport ORDER BY revenue DESC) AS rank,
                    AVG(revenue) OVER(PARTITION BY fare_conditions ORDER BY tickets
                    GROUPS BETWEEN 2 PRECEDING AND CURRENT ROW) AS moving FROM g)
                SELECT aircraft_code,AVG(rank) AS average_rank,MAX(moving) AS peak,
                    SUM(revenue) AS revenue FROM w GROUP BY aircraft_code
                ORDER BY peak DESC LIMIT 140""",
        }
        for name, sql in families.items():
            yield name.split("_", 1)[0], QueryCase("flow_" + name, sql.strip())
