-- Two independent opportunities: COUNT filter -> EXISTS and redundant inner ORDER BY.
-- A speedup is not guaranteed; compare all variants on the connected database.
SELECT q.product_id, q.product_name
FROM (
    SELECT p.product_id, p.product_name
    FROM retail.products AS p
    WHERE p.is_active = true
      AND (
          SELECT COUNT(*)
          FROM retail.order_items AS oi
          WHERE oi.product_id = p.product_id
      ) > 0
    ORDER BY p.product_name DESC, p.product_id
) AS q
ORDER BY q.product_id
LIMIT 200;
