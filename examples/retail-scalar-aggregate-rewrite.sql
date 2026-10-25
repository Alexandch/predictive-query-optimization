SELECT
    p.customer_id,
    (SELECT COUNT(*)
     FROM retail.customer_orders AS o
     WHERE o.customer_id = p.customer_id) AS order_count,
    (SELECT SUM(o.total_amount)
     FROM retail.customer_orders AS o
     WHERE o.customer_id = p.customer_id
       AND o.order_status = 'delivered') AS delivered_amount
FROM retail.customers AS p
WHERE p.customer_id BETWEEN 1 AND 50
ORDER BY p.customer_id;
