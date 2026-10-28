"""Fixed fresh SQL for paired comparison; these queries never train a model."""

from .query_case import QueryCase


def paired_workload(bounds=(2300, 13000, 43000)):
    for bound in bounds:
        sources = {
            "retail": f"""SELECT p.category_id AS g1,p.brand AS g2,oi.product_id AS entity,
                o.customer_id AS person,oi.quantity*oi.unit_price AS value
                FROM retail.customer_orders o JOIN retail.order_items oi USING(order_id)
                JOIN retail.products p USING(product_id)
                WHERE o.order_id <= {bound * 2} AND o.order_status IN ('paid','processing','delivered')""",
            "aviation": f"""SELECT f.departure_airport AS g1,f.arrival_airport AS g2,
                f.flight_id AS entity,tf.ticket_no AS person,tf.amount AS value
                FROM aviation.flights f JOIN aviation.ticket_flights tf USING(flight_id)
                WHERE f.flight_id <= {bound} AND tf.fare_conditions <> 'Business'""",
        }
        for schema, source in sources.items():
            families = {
                "ordered_detail": f"""SELECT g1,g2,entity,person,value FROM ({source}) src
                    WHERE value >= 1900 ORDER BY value DESC,entity,person LIMIT 90""",
                "grouped_report": f"""SELECT g1,g2,COUNT(DISTINCT person) AS people,
                    SUM(value) AS revenue,AVG(value) AS average FROM ({source}) src
                    GROUP BY g1,g2 HAVING COUNT(*) >= 3 ORDER BY revenue DESC LIMIT 90""",
                "member_summary": f"""WITH src AS MATERIALIZED ({source}),
                    members AS (SELECT person FROM src GROUP BY person HAVING COUNT(*) >= 3)
                    SELECT g1,COUNT(DISTINCT entity) AS entities,SUM(value) AS revenue
                    FROM src WHERE person IN (SELECT person FROM members)
                    GROUP BY g1 ORDER BY revenue DESC LIMIT 90""",
                "crossed_windows": f"""SELECT g1,g2,entity,person,value,
                    SUM(value) OVER(PARTITION BY g1 ORDER BY entity,person
                        ROWS BETWEEN 4 PRECEDING AND 2 FOLLOWING) AS moving,
                    AVG(value) OVER(PARTITION BY g2 ORDER BY value
                        RANGE UNBOUNDED PRECEDING) AS average
                    FROM ({source}) src ORDER BY moving DESC,entity LIMIT 90""",
                "reduced_percentiles": f"""WITH g AS (
                    SELECT g1,entity,SUM(value) AS revenue,COUNT(DISTINCT person) AS people
                    FROM ({source}) src GROUP BY g1,entity)
                    SELECT g1,SUM(revenue) AS revenue,AVG(people) AS average_people,
                        percentile_cont(0.45) WITHIN GROUP(ORDER BY revenue) AS middle
                    FROM g GROUP BY g1 ORDER BY revenue DESC LIMIT 90""",
                "limited_group_ranks": f"""WITH ranked AS (
                    SELECT *,ROW_NUMBER() OVER(PARTITION BY g1 ORDER BY value DESC,entity,person) AS position
                    FROM ({source}) src)
                    SELECT g1,g2,entity,person,value,position FROM ranked
                    WHERE position <= 4 ORDER BY g1,position LIMIT 90""",
            }
            for name, sql in families.items():
                yield QueryCase(f"paired_{schema}_{name}", sql.strip())
