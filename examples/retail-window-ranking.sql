WITH product_sales AS (
    SELECT oi.product_id,
           COUNT(DISTINCT oi.order_id) AS order_count,
           SUM(oi.quantity) AS units_sold,
           SUM(oi.quantity * oi.unit_price) AS revenue,
           AVG(oi.unit_price) AS average_sale_price
    FROM retail.order_items AS oi
    JOIN retail.customer_orders AS o ON o.order_id = oi.order_id
    WHERE o.order_status IN ('paid', 'processing', 'shipped', 'delivered')
    GROUP BY oi.product_id
), product_stock AS (
    SELECT i.product_id,
           SUM(i.quantity) AS total_stock,
           SUM(i.reserved_quantity) AS reserved_stock,
           SUM(i.quantity - i.reserved_quantity) AS available_stock,
           COUNT(DISTINCT i.warehouse_id) AS warehouse_count
    FROM retail.inventory AS i
    GROUP BY i.product_id
), ranked_products AS (
    SELECT c.category_name, p.product_id, p.product_name, p.brand, p.price,
           COALESCE(ps.order_count, 0) AS order_count,
           COALESCE(ps.units_sold, 0) AS units_sold,
           COALESCE(ps.revenue, 0) AS revenue,
           COALESCE(st.total_stock, 0) AS total_stock,
           COALESCE(st.reserved_stock, 0) AS reserved_stock,
           COALESCE(st.available_stock, 0) AS available_stock,
           COALESCE(st.warehouse_count, 0) AS warehouse_count,
           DENSE_RANK() OVER (
               PARTITION BY c.category_id ORDER BY COALESCE(ps.revenue, 0) DESC
           ) AS revenue_rank,
           AVG(COALESCE(ps.revenue, 0)) OVER (
               PARTITION BY c.category_id
           ) AS category_average_revenue
    FROM retail.products AS p
    JOIN retail.categories AS c ON c.category_id = p.category_id
    LEFT JOIN product_sales AS ps ON ps.product_id = p.product_id
    LEFT JOIN product_stock AS st ON st.product_id = p.product_id
    WHERE p.is_active = true
)
SELECT category_name, product_id, product_name, brand, price, order_count,
       units_sold, ROUND(revenue::numeric, 2) AS revenue, total_stock,
       reserved_stock, available_stock, warehouse_count, revenue_rank,
       ROUND(category_average_revenue::numeric, 2) AS category_average_revenue
FROM ranked_products
WHERE revenue_rank <= 20 OR available_stock = 0
ORDER BY category_name, revenue_rank, available_stock
LIMIT 500;
