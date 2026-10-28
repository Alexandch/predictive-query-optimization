"""Costly operator patterns with splits fixed before execution."""

from .query_case import QueryCase


def heavy_fresh_workload():
    for bound in (1800, 5100, 12500, 26000, 42000, 54000):
        sources = {
            "retail": f"""SELECT p.category_id AS g1,p.brand AS g2,oi.product_id AS entity,
                o.customer_id AS person,oi.quantity*oi.unit_price AS value
                FROM retail.order_items oi JOIN retail.customer_orders o USING(order_id)
                JOIN retail.products p USING(product_id)
                WHERE o.order_id <= {bound * 2} AND o.order_status <> 'cancelled'""",
            "aviation": f"""SELECT f.departure_airport AS g1,f.arrival_airport AS g2,
                f.flight_id AS entity,tf.ticket_no AS person,tf.amount AS value
                FROM aviation.ticket_flights tf JOIN aviation.flights f USING(flight_id)
                WHERE f.flight_id <= {bound} AND tf.amount > 700""",
        }
        for domain, source in sources.items():
            families = {
                "train_raw_batches": f"""SELECT g1,g2,entity,person,value,
                    SUM(value) OVER(PARTITION BY g1 ORDER BY value,person
                        ROWS BETWEEN 11 PRECEDING AND 4 FOLLOWING) AS neighborhood,
                    DENSE_RANK() OVER(PARTITION BY g2 ORDER BY value DESC,entity) AS price_rank
                    FROM ({source}) src ORDER BY neighborhood DESC,entity LIMIT 140""",
                "train_window_reduction": f"""WITH w AS (
                    SELECT *,AVG(value) OVER(PARTITION BY person ORDER BY entity,value
                        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS average,
                        SUM(value) OVER(PARTITION BY g2 ORDER BY entity,person
                        ROWS UNBOUNDED PRECEDING) AS running FROM ({source}) src)
                    SELECT entity,COUNT(DISTINCT g1) AS groups,AVG(average) AS average,
                        MAX(running) AS peak,SUM(value) AS revenue FROM w
                    GROUP BY entity ORDER BY peak DESC LIMIT 140""",
                "train_reused_membership": f"""WITH src AS MATERIALIZED ({source}),
                    active AS (SELECT entity FROM src GROUP BY entity HAVING SUM(value)>10000),
                    populations AS (SELECT g1,COUNT(DISTINCT person) AS people FROM src GROUP BY g1)
                    SELECT g1,COUNT(DISTINCT entity) AS entities,SUM(value) AS revenue,MAX(people) AS people
                    FROM src JOIN populations USING(g1) WHERE entity IN(SELECT entity FROM active)
                    GROUP BY g1 ORDER BY revenue DESC LIMIT 140""",
                "train_staged_sorts": f"""WITH g AS (
                    SELECT g1,g2,entity,SUM(value) AS revenue,COUNT(DISTINCT person) AS people,
                        percentile_cont(0.6) WITHIN GROUP(ORDER BY value) AS price
                    FROM ({source}) src GROUP BY g1,g2,entity),
                    w AS (SELECT *,SUM(revenue) OVER(PARTITION BY g1 ORDER BY people,entity
                        ROWS UNBOUNDED PRECEDING) AS running,
                        RANK() OVER(PARTITION BY g2 ORDER BY price DESC) AS rank FROM g)
                    SELECT g1,SUM(revenue) AS revenue,AVG(rank) AS average_rank,MAX(running) AS peak
                    FROM w GROUP BY g1 ORDER BY peak DESC LIMIT 140""",
                "validation_distinct_stream": f"""WITH unique_rows AS (
                    SELECT DISTINCT g1,g2,person,value FROM ({source}) src)
                    SELECT *,SUM(value) OVER(PARTITION BY g1 ORDER BY value
                        RANGE UNBOUNDED PRECEDING) AS running,
                        DENSE_RANK() OVER(PARTITION BY g2 ORDER BY value DESC) AS rank
                    FROM unique_rows ORDER BY running DESC LIMIT 160""",
                "validation_repeated_cutoff": f"""WITH src AS MATERIALIZED ({source}),
                    cutoffs AS (SELECT g2,percentile_disc(0.7) WITHIN GROUP(ORDER BY value) AS price
                        FROM src GROUP BY g2)
                    SELECT s.g1,s.entity,COUNT(*) AS lines,SUM(s.value) AS revenue
                    FROM src s JOIN cutoffs c ON c.g2=s.g2 WHERE s.value>=c.price
                    GROUP BY s.g1,s.entity ORDER BY revenue DESC LIMIT 160""",
                "acceptance_peer_rollup": f"""WITH w AS (
                    SELECT *,SUM(value) OVER(PARTITION BY g1 ORDER BY value,entity,person
                        GROUPS BETWEEN 2 PRECEDING AND 1 FOLLOWING EXCLUDE TIES) AS peers,
                        AVG(value) OVER(PARTITION BY g2 ORDER BY entity,person
                        ROWS BETWEEN 15 PRECEDING AND CURRENT ROW) AS moving
                    FROM ({source}) src)
                    SELECT g1,g2,GROUPING(g1,g2) AS level,SUM(value) AS revenue,
                        AVG(peers) AS peers,MAX(moving) AS peak FROM w
                    GROUP BY GROUPING SETS((g1,g2),(g1)) ORDER BY revenue DESC LIMIT 180""",
                "acceptance_member_profile": f"""WITH src AS MATERIALIZED ({source}),
                    members AS (SELECT person FROM src GROUP BY person HAVING COUNT(DISTINCT entity)>=2)
                    SELECT g1,g2,COUNT(DISTINCT person) AS people,SUM(value) AS revenue,
                        percentile_cont(0.4) WITHIN GROUP(ORDER BY value) AS price
                    FROM src WHERE person IN(SELECT person FROM members)
                    GROUP BY g1,g2 ORDER BY revenue DESC LIMIT 180""",
                "acceptance_reduced_rank": f"""WITH g AS (
                    SELECT g1,person,SUM(value) AS revenue,COUNT(DISTINCT entity) AS entities
                    FROM ({source}) src GROUP BY g1,person),
                    w AS (SELECT *,DENSE_RANK() OVER(PARTITION BY g1 ORDER BY revenue DESC) AS rank,
                        SUM(revenue) OVER(PARTITION BY g1 ORDER BY entities,person
                        ROWS BETWEEN 5 PRECEDING AND CURRENT ROW) AS moving FROM g)
                    SELECT g1,MIN(moving) AS low,MAX(rank) AS ranks,SUM(revenue) AS revenue
                    FROM w GROUP BY g1 ORDER BY revenue DESC LIMIT 180""",
                "acceptance_cte_branches": f"""WITH src AS MATERIALIZED ({source}),
                    first AS (SELECT g1,AVG(value) AS cutoff FROM src GROUP BY g1),
                    second AS (SELECT g2,COUNT(DISTINCT person) AS people FROM src GROUP BY g2)
                    SELECT g1,g2,SUM(value) AS revenue,MAX(people) AS people,COUNT(*) AS lines
                    FROM src JOIN first USING(g1) JOIN second USING(g2) WHERE value>=cutoff
                    GROUP BY g1,g2 HAVING COUNT(*)>3 ORDER BY revenue DESC LIMIT 180""",
            }
            for name, sql in families.items():
                split = name.split("_", 1)[0]
                if split != "train" and bound not in (5100, 26000, 54000):
                    continue
                yield split, QueryCase(f"heavy_fresh_{domain}_{name}", sql.strip())
