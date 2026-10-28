SELECT DISTINCT
    o.order_id,
    o.customer_id
FROM retail.customer_orders AS o
JOIN retail.order_items AS oi
    ON oi.order_id = o.order_id
JOIN retail.products AS p
    ON p.product_id = oi.product_id
JOIN retail.payments AS pay
    ON pay.order_id = o.order_id
WHERE o.order_status <> 'cancelled'
  AND p.is_active = true
  AND pay.amount > 0
ORDER BY o.order_id;
