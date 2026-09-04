INSERT INTO retail.categories (category_id, parent_category_id, category_name)
SELECT category_id,
       CASE WHEN category_id <= 10 THEN NULL ELSE ((category_id - 1) % 10) + 1 END,
       'Category ' || category_id
FROM generate_series(1, 60) AS category_id
ON CONFLICT DO NOTHING;

INSERT INTO retail.customers (
    customer_id, email, full_name, segment, registered_at, is_active
)
SELECT customer_id,
       'customer' || customer_id || '@example.test',
       'Customer ' || customer_id,
       (ARRAY['consumer', 'business', 'vip'])[(customer_id % 3) + 1],
       timestamptz '2021-01-01 00:00:00+00'
           + (customer_id % 1460) * interval '1 day',
       customer_id % 29 <> 0
FROM generate_series(1, 50000) AS customer_id
ON CONFLICT DO NOTHING;

INSERT INTO retail.addresses (
    address_id, customer_id, city, region, postal_code, address_type
)
SELECT address_id,
       ((address_id - 1) % 50000) + 1,
       'City ' || (address_id % 300),
       'Region ' || (address_id % 20),
       lpad((100000 + address_id % 900000)::text, 6, '0'),
       (ARRAY['billing', 'shipping'])[(address_id % 2) + 1]
FROM generate_series(1, 75000) AS address_id
ON CONFLICT DO NOTHING;

INSERT INTO retail.products (
    product_id, category_id, sku, product_name, price, brand,
    attributes, is_active, created_at
)
SELECT product_id,
       ((product_id - 1) % 60) + 1,
       'SKU-' || lpad(product_id::text, 8, '0'),
       'Product ' || product_id,
       (5 + (product_id * 37 % 200000) / 100.0)::numeric(12, 2),
       'Brand ' || (product_id % 250),
       jsonb_build_object(
           'color', (ARRAY['black', 'white', 'blue', 'red'])[(product_id % 4) + 1],
           'rating', ((product_id % 41) + 10) / 10.0
       ),
       product_id % 43 <> 0,
       timestamptz '2022-01-01 00:00:00+00'
           + (product_id % 1000) * interval '1 day'
FROM generate_series(1, 15000) AS product_id
ON CONFLICT DO NOTHING;

INSERT INTO retail.warehouses (warehouse_id, warehouse_name, region, capacity)
SELECT warehouse_id,
       'Warehouse ' || warehouse_id,
       'Region ' || (warehouse_id % 20),
       100000 + warehouse_id * 25000
FROM generate_series(1, 15) AS warehouse_id
ON CONFLICT DO NOTHING;

INSERT INTO retail.inventory (
    warehouse_id, product_id, quantity, reserved_quantity, updated_at
)
SELECT warehouse_id,
       product_id,
       (warehouse_id * 17 + product_id * 13) % 500,
       LEAST(
           (warehouse_id * 17 + product_id * 13) % 500,
           (warehouse_id * 7 + product_id * 3) % 40
       ),
       timestamptz '2025-01-01 00:00:00+00'
           + (product_id % 365) * interval '1 day'
FROM generate_series(1, 15) AS warehouse_id
CROSS JOIN generate_series(1, 15000) AS product_id
ON CONFLICT DO NOTHING;

INSERT INTO retail.customer_orders (
    order_id, customer_id, shipping_address_id, order_status,
    sales_channel, ordered_at, total_amount, discount_amount
)
SELECT order_id,
       ((order_id * 17 - 1) % 50000) + 1,
       ((order_id * 17 - 1) % 50000) + 1,
       (ARRAY['pending', 'paid', 'processing', 'shipped', 'delivered', 'cancelled'])[
           (order_id % 6) + 1
       ],
       (ARRAY['web', 'mobile', 'store', 'marketplace'])[(order_id % 4) + 1],
       timestamptz '2023-01-01 00:00:00+00'
           + (order_id % 1095) * interval '1 day'
           + (order_id % 86400) * interval '1 second',
       (20 + (order_id * 97 % 500000) / 100.0)::numeric(14, 2),
       ((order_id * 11 % 15000) / 100.0)::numeric(12, 2)
FROM generate_series(1, 150000) AS order_id
ON CONFLICT DO NOTHING;

INSERT INTO retail.order_items (
    order_id, line_no, product_id, quantity, unit_price
)
SELECT order_id,
       line_no,
       ((order_id * 31 + line_no * 101 - 1) % 15000) + 1,
       ((order_id + line_no) % 5) + 1,
       (5 + ((order_id * 31 + line_no * 101) * 37 % 200000) / 100.0)::numeric(12, 2)
FROM generate_series(1, 150000) AS order_id
CROSS JOIN generate_series(1, 4) AS line_no
ON CONFLICT DO NOTHING;

INSERT INTO retail.payments (
    payment_id, order_id, payment_method, payment_status, amount, paid_at
)
SELECT order_id,
       order_id,
       (ARRAY['card', 'cash', 'bank_transfer', 'wallet'])[(order_id % 4) + 1],
       (ARRAY['authorized', 'captured', 'failed', 'refunded'])[(order_id % 4) + 1],
       (20 + (order_id * 97 % 500000) / 100.0)::numeric(14, 2),
       CASE WHEN order_id % 4 = 2 THEN NULL
            ELSE timestamptz '2023-01-01 00:10:00+00'
                + (order_id % 1095) * interval '1 day'
                + (order_id % 86400) * interval '1 second'
       END
FROM generate_series(1, 150000) AS order_id
ON CONFLICT DO NOTHING;

INSERT INTO retail.shipments (
    shipment_id, order_id, warehouse_id, carrier, tracking_number,
    shipment_status, shipped_at, delivered_at
)
SELECT order_id,
       order_id,
       ((order_id - 1) % 15) + 1,
       (ARRAY['DHL', 'DPD', 'FedEx', 'LocalPost'])[(order_id % 4) + 1],
       'TRACK-' || lpad(order_id::text, 12, '0'),
       (ARRAY['ready', 'in_transit', 'delivered', 'returned'])[(order_id % 4) + 1],
       timestamptz '2023-01-02 00:00:00+00'
           + (order_id % 1095) * interval '1 day',
       CASE WHEN order_id % 4 IN (0, 3)
            THEN timestamptz '2023-01-04 00:00:00+00'
                + (order_id % 1095) * interval '1 day'
            ELSE NULL
       END
FROM generate_series(1, 150000) AS order_id
WHERE order_id % 6 IN (3, 4, 5)
ON CONFLICT DO NOTHING;

SELECT setval(pg_get_serial_sequence('retail.customers', 'customer_id'), 50000, true);
SELECT setval(pg_get_serial_sequence('retail.addresses', 'address_id'), 75000, true);
SELECT setval(pg_get_serial_sequence('retail.categories', 'category_id'), 60, true);
SELECT setval(pg_get_serial_sequence('retail.products', 'product_id'), 15000, true);
SELECT setval(pg_get_serial_sequence('retail.warehouses', 'warehouse_id'), 15, true);
SELECT setval(pg_get_serial_sequence('retail.customer_orders', 'order_id'), 150000, true);
SELECT setval(pg_get_serial_sequence('retail.payments', 'payment_id'), 150000, true);
SELECT setval(pg_get_serial_sequence('retail.shipments', 'shipment_id'), 150000, true);

ANALYZE retail.customers;
ANALYZE retail.addresses;
ANALYZE retail.categories;
ANALYZE retail.products;
ANALYZE retail.warehouses;
ANALYZE retail.inventory;
ANALYZE retail.customer_orders;
ANALYZE retail.order_items;
ANALYZE retail.payments;
ANALYZE retail.shipments;
