"""Fresh, preassigned control for combined SQL-structure and plan-flow features."""

from .query_case import QueryCase


def ordered_transfer_workload():
    for bound in (7100, 23500, 61000):
        sources = {
            "retail": f"""SELECT p.category_id AS g1,p.brand AS g2,oi.product_id AS entity,
                o.customer_id AS person,oi.quantity*oi.unit_price AS value
                FROM retail.order_items oi JOIN retail.customer_orders o USING(order_id)
                JOIN retail.products p USING(product_id)
                WHERE o.order_id <= {bound * 2} AND oi.unit_price > 25""",
            "aviation": f"""SELECT f.departure_airport AS g1,tf.fare_conditions AS g2,
                f.flight_id AS entity,tf.ticket_no AS person,tf.amount AS value
                FROM aviation.ticket_flights tf JOIN aviation.flights f USING(flight_id)
                WHERE f.flight_id <= {bound} AND tf.amount > 1150""",
        }
        for domain, source in sources.items():
            patterns = {
                "percentile_rank_filter": """SELECT * FROM (SELECT *,
                    DENSE_RANK() OVER(PARTITION BY g1 ORDER BY revenue DESC) AS rank,
                    AVG(mid) OVER(PARTITION BY g2) AS average FROM (
                    SELECT g1,g2,entity,COUNT(DISTINCT person) AS people,SUM(value) AS revenue,
                    percentile_cont(0.45) WITHIN GROUP(ORDER BY value) AS mid,
                    percentile_cont(0.92) WITHIN GROUP(ORDER BY value) AS high
                    FROM src GROUP BY g1,g2,entity HAVING COUNT(*)>4) g) w
                    WHERE rank<=18 OR high>average*1.6 ORDER BY revenue DESC,entity LIMIT 230""",
                "percentile_running_reduction": """SELECT g1,g2,AVG(price) AS average,
                    MAX(cumulative) AS peak,SUM(revenue) AS revenue FROM (SELECT *,
                    SUM(revenue) OVER(PARTITION BY g2 ORDER BY price,entity ROWS UNBOUNDED PRECEDING) AS cumulative
                    FROM (SELECT g1,g2,entity,SUM(value) AS revenue,
                    percentile_cont(0.68) WITHIN GROUP(ORDER BY value) AS price
                    FROM src GROUP BY g1,g2,entity) g) w GROUP BY g1,g2 ORDER BY peak DESC LIMIT 230""",
                "deduplicated_member_rank": """SELECT * FROM (SELECT *,
                    DENSE_RANK() OVER(PARTITION BY g1 ORDER BY revenue DESC) AS rank,
                    SUM(revenue) OVER(PARTITION BY g2) AS total FROM (
                    SELECT g1,g2,person,COUNT(*) AS lines,SUM(value) AS revenue FROM
                    (SELECT DISTINCT g1,g2,person,entity,value FROM src) d GROUP BY g1,g2,person) g) w
                    WHERE rank<=22 OR revenue>total*0.04 ORDER BY revenue DESC,person LIMIT 230""",
                "window_percentile_spread": """SELECT g1,g2,
                    percentile_cont(0.31) WITHIN GROUP(ORDER BY moving) AS low,
                    percentile_cont(0.79) WITHIN GROUP(ORDER BY moving) AS high,
                    SUM(value) AS revenue FROM (SELECT *,SUM(value) OVER(PARTITION BY entity
                    ORDER BY value,person ROWS BETWEEN 2 PRECEDING AND 1 FOLLOWING) AS moving
                    FROM src) w GROUP BY g1,g2 ORDER BY high DESC LIMIT 230""",
            }
            for name, body in patterns.items():
                yield QueryCase(f"ordered_transfer_{domain}_{name}", f"WITH src AS ({source}) {body}")
