WITH sales AS (
    SELECT oi.product_id,
        COUNT(DISTINCT oi.order_id) AS order_count,
        SUM(oi.quantity) AS units_sold,
        SUM(oi.quantity * oi.unit_price) AS revenue,
        AVG(oi.unit_price) AS average_sale_price,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY oi.unit_price) AS median_sale_price
    FROM retail.order_items AS oi
    JOIN retail.customer_orders AS o ON o.order_id = oi.order_id
    WHERE o.order_status IN ('paid', 'processing', 'shipped', 'delivered')
    GROUP BY oi.product_id
), stock AS (
    SELECT product_id,
        SUM(quantity - reserved_quantity) AS available_stock,
        COUNT(DISTINCT warehouse_id) AS warehouse_count
    FROM retail.inventory GROUP BY product_id
), category_sales AS (
    SELECT p.category_id,
        COUNT(DISTINCT oi.order_id) AS category_order_count,
        SUM(oi.quantity * oi.unit_price) AS category_revenue
    FROM retail.products AS p
    JOIN retail.order_items AS oi ON oi.product_id = p.product_id
    JOIN retail.customer_orders AS o ON o.order_id = oi.order_id
    WHERE o.order_status IN ('paid', 'processing', 'shipped', 'delivered')
    GROUP BY p.category_id
), ranked AS (
    SELECT c.category_name, p.product_id, p.product_name, p.brand, p.price,
        s.order_count, s.units_sold, s.revenue, s.average_sale_price, s.median_sale_price,
        COALESCE(st.available_stock, 0) AS available_stock,
        COALESCE(st.warehouse_count, 0) AS warehouse_count,
        cs.category_order_count,
        100.0 * s.revenue / NULLIF(cs.category_revenue, 0) AS category_share_percent,
        DENSE_RANK() OVER (PARTITION BY p.category_id ORDER BY s.revenue DESC) AS revenue_rank,
        AVG(s.revenue) OVER (PARTITION BY p.category_id) AS category_average_product_revenue,
        SUM(s.revenue) OVER (
            PARTITION BY p.category_id ORDER BY s.revenue DESC, p.product_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cumulative_revenue
    FROM retail.products AS p
    JOIN retail.categories AS c ON c.category_id = p.category_id
    JOIN sales AS s ON s.product_id = p.product_id
    JOIN category_sales AS cs ON cs.category_id = p.category_id
    LEFT JOIN stock AS st ON st.product_id = p.product_id
    WHERE p.is_active = true
)
SELECT category_name, product_id, product_name, brand, order_count, units_sold,
    ROUND(revenue::numeric, 2) AS revenue,
    ROUND(median_sale_price::numeric, 2) AS median_sale_price,
    available_stock, warehouse_count, revenue_rank,
    ROUND(category_share_percent::numeric, 2) AS category_share_percent,
    ROUND(category_average_product_revenue::numeric, 2) AS category_average_product_revenue,
    ROUND(cumulative_revenue::numeric, 2) AS cumulative_revenue
FROM ranked
WHERE revenue_rank <= 30
   OR (available_stock = 0 AND revenue > category_average_product_revenue)
ORDER BY category_name, revenue_rank, product_id
LIMIT 1000;
