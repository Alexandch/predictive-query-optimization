"""Disjoint ordered-aggregate families; no user-provided SQL in training."""

from .query_case import QueryCase


def ordered_workload():
    for bound in (4500, 19000, 58000):
        sources = {
            "retail": f"""SELECT p.category_id AS g1,p.brand AS g2,oi.product_id AS entity,
                o.customer_id AS person,oi.quantity*oi.unit_price AS value
                FROM retail.order_items oi JOIN retail.customer_orders o USING(order_id)
                JOIN retail.products p USING(product_id)
                WHERE o.order_id <= {bound * 2} AND oi.quantity > 0""",
            "aviation": f"""SELECT f.departure_airport AS g1,tf.fare_conditions AS g2,
                f.flight_id AS entity,tf.ticket_no AS person,tf.amount AS value
                FROM aviation.ticket_flights tf JOIN aviation.flights f USING(flight_id)
                WHERE f.flight_id <= {bound} AND tf.amount > 950""",
        }
        for domain, source in sources.items():
            families = {
                "train_percentile_array": """SELECT g1,g2,entity,COUNT(*) AS n,SUM(value) AS revenue,
                    percentile_cont(ARRAY[0.25,0.5,0.75]) WITHIN GROUP(ORDER BY value) AS quartiles
                    FROM src GROUP BY g1,g2,entity HAVING COUNT(*)>2 ORDER BY revenue DESC LIMIT 175""",
                "train_two_percentiles": """SELECT g1,g2,entity,COUNT(DISTINCT person) AS people,
                    percentile_cont(0.35) WITHIN GROUP(ORDER BY value) AS low,
                    percentile_cont(0.85) WITHIN GROUP(ORDER BY value) AS high,SUM(value) AS revenue
                    FROM src GROUP BY g1,g2,entity ORDER BY high DESC,entity LIMIT 175""",
                "train_percentile_rank": """SELECT *,DENSE_RANK() OVER(PARTITION BY g1 ORDER BY mid DESC) AS rank
                    FROM (SELECT g1,g2,entity,percentile_cont(0.55) WITHIN GROUP(ORDER BY value) AS mid,
                    COUNT(DISTINCT person) AS people FROM src GROUP BY g1,g2,entity) g
                    ORDER BY rank,people DESC LIMIT 175""",
                "train_window_then_percentile": """SELECT g1,g2,percentile_cont(0.65) WITHIN GROUP(ORDER BY moving) AS price,
                    COUNT(DISTINCT person) AS people FROM (SELECT *,AVG(value) OVER(PARTITION BY entity
                    ORDER BY value,person ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) AS moving FROM src) w
                    GROUP BY g1,g2 ORDER BY price DESC LIMIT 175""",
                "train_distinct_rank": """SELECT *,SUM(revenue) OVER(PARTITION BY g1 ORDER BY people,person
                    ROWS UNBOUNDED PRECEDING) AS running FROM (SELECT g1,person,SUM(value) AS revenue,
                    COUNT(DISTINCT entity) AS people FROM src GROUP BY g1,person) g
                    ORDER BY running DESC,person LIMIT 175""",
                "validation_percentile_ntile": """SELECT *,NTILE(7) OVER(PARTITION BY g2 ORDER BY price) AS bucket
                    FROM (SELECT g1,g2,entity,percentile_cont(0.72) WITHIN GROUP(ORDER BY value) AS price,
                    SUM(value) AS revenue FROM src GROUP BY g1,g2,entity) g ORDER BY revenue DESC LIMIT 185""",
                "validation_distinct_window": """SELECT *,AVG(revenue) OVER(PARTITION BY g2) AS average,
                    RANK() OVER(PARTITION BY g1 ORDER BY revenue DESC) AS rank FROM (SELECT g1,g2,person,
                    COUNT(DISTINCT entity) AS entities,SUM(value) AS revenue FROM src GROUP BY g1,g2,person) g
                    ORDER BY rank,entities DESC LIMIT 185""",
                "acceptance_percentile_deviation": """SELECT g1,g2,SUM(revenue) AS revenue,AVG(high-low) AS spread,
                    MAX(rank) AS ranks FROM (SELECT *,RANK() OVER(PARTITION BY g1 ORDER BY high DESC) AS rank
                    FROM (SELECT g1,g2,entity,SUM(value) AS revenue,
                    percentile_cont(0.2) WITHIN GROUP(ORDER BY value) AS low,
                    percentile_cont(0.8) WITHIN GROUP(ORDER BY value) AS high
                    FROM src GROUP BY g1,g2,entity) g) w GROUP BY g1,g2 ORDER BY spread DESC LIMIT 195""",
                "acceptance_percentile_join": """SELECT s.g1,s.g2,COUNT(DISTINCT s.person) AS people,SUM(s.value) AS revenue
                    FROM src s JOIN (SELECT g1,percentile_cont(0.58) WITHIN GROUP(ORDER BY value) AS cutoff
                    FROM src GROUP BY g1) c ON s.g1=c.g1 WHERE s.value>c.cutoff
                    GROUP BY s.g1,s.g2 ORDER BY revenue DESC LIMIT 195""",
                "acceptance_percentile_rollup": """SELECT g1,g2,GROUPING(g1,g2) AS level,
                    percentile_cont(0.43) WITHIN GROUP(ORDER BY value) AS price,
                    COUNT(DISTINCT entity) AS entities,SUM(value) AS revenue FROM src
                    GROUP BY GROUPING SETS((g1,g2),(g1)) ORDER BY price DESC LIMIT 195""",
                "acceptance_distinct_peers": """SELECT g1,AVG(moving) AS average,MAX(rank) AS ranks,SUM(revenue) AS revenue
                    FROM (SELECT *,AVG(revenue) OVER(PARTITION BY g2 ORDER BY entities,person
                    ROWS BETWEEN 4 PRECEDING AND 2 FOLLOWING) AS moving,
                    DENSE_RANK() OVER(PARTITION BY g1 ORDER BY revenue DESC) AS rank
                    FROM (SELECT g1,g2,person,COUNT(DISTINCT entity) AS entities,SUM(value) AS revenue
                    FROM src GROUP BY g1,g2,person) g) w GROUP BY g1 ORDER BY average DESC LIMIT 195""",
            }
            for name, body in families.items():
                yield name.split("_", 1)[0], QueryCase(
                    f"ordered_{domain}_{name}", f"WITH src AS ({source}) {body}")
