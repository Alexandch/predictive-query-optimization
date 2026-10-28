SELECT DISTINCT
    c.customer_id
FROM retail.customers AS c
JOIN retail.customer_orders AS o
    ON o.customer_id = c.customer_id
JOIN retail.order_items AS oi
    ON oi.order_id = o.order_id
JOIN retail.products AS p
    ON p.product_id = oi.product_id
WHERE o.order_status <> 'cancelled'
  AND p.is_active = true
  AND oi.quantity > 0
ORDER BY c.customer_id;
