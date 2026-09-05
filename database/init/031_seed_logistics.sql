INSERT INTO logistics.suppliers (
    supplier_id, supplier_name, country_code, reliability_score,
    contract_since, is_active
)
SELECT supplier_id,
       'Supplier ' || supplier_id,
       (ARRAY['BY', 'PL', 'DE', 'CN', 'TR', 'LT', 'LV', 'KZ'])[(supplier_id % 8) + 1],
       (700 + supplier_id % 301)::numeric / 1000,
       DATE '2018-01-01' + (supplier_id % 2500),
       supplier_id % 37 <> 0
FROM generate_series(1, 5000) AS supplier_id
ON CONFLICT DO NOTHING;

INSERT INTO logistics.facilities (
    facility_id, facility_name, facility_type, region, capacity_units
)
SELECT facility_id,
       'Facility ' || facility_id,
       (ARRAY['warehouse', 'hub', 'store', 'port'])[(facility_id % 4) + 1],
       'Logistics Region ' || (facility_id % 12),
       100000 + facility_id * 20000
FROM generate_series(1, 30) AS facility_id
ON CONFLICT DO NOTHING;

INSERT INTO logistics.items (
    item_id, supplier_id, sku, item_name, item_class,
    unit_weight_kg, unit_value, is_active
)
SELECT item_id,
       ((item_id * 19 - 1) % 5000) + 1,
       'L-SKU-' || lpad(item_id::text, 8, '0'),
       'Logistics Item ' || item_id,
       (ARRAY['standard', 'fragile', 'cold', 'hazardous'])[(item_id % 4) + 1],
       (100 + item_id * 17 % 250000)::numeric / 1000,
       (10 + (item_id * 43 % 300000) / 100.0)::numeric(12, 2),
       item_id % 53 <> 0
FROM generate_series(1, 20000) AS item_id
ON CONFLICT DO NOTHING;

INSERT INTO logistics.carriers (
    carrier_id, carrier_name, service_level, base_rate
)
SELECT carrier_id,
       'Carrier ' || carrier_id,
       (ARRAY['economy', 'standard', 'express'])[(carrier_id % 3) + 1],
       (20 + carrier_id * 7.5)::numeric(10, 2)
FROM generate_series(1, 20) AS carrier_id
ON CONFLICT DO NOTHING;

INSERT INTO logistics.purchase_orders (
    purchase_order_id, supplier_id, destination_facility_id,
    order_status, priority, ordered_at, expected_at, total_value
)
SELECT purchase_order_id,
       ((purchase_order_id * 23 - 1) % 5000) + 1,
       ((purchase_order_id * 7 - 1) % 30) + 1,
       (ARRAY['draft', 'confirmed', 'in_transit', 'received', 'cancelled'])[
           (purchase_order_id % 5) + 1
       ],
       (ARRAY['low', 'normal', 'high', 'critical'])[(purchase_order_id % 4) + 1],
       timestamptz '2023-01-01 00:00:00+00'
           + (purchase_order_id % 1095) * interval '1 day'
           + (purchase_order_id % 86400) * interval '1 second',
       timestamptz '2023-01-03 00:00:00+00'
           + (purchase_order_id % 1095) * interval '1 day'
           + (purchase_order_id % 14) * interval '1 day',
       (100 + (purchase_order_id * 113 % 2000000) / 100.0)::numeric(14, 2)
FROM generate_series(1, 120000) AS purchase_order_id
ON CONFLICT DO NOTHING;

INSERT INTO logistics.purchase_order_lines (
    purchase_order_id, line_no, item_id, ordered_quantity,
    received_quantity, unit_cost
)
SELECT purchase_order_id,
       line_no,
       ((purchase_order_id * 31 + line_no * 107 - 1) % 20000) + 1,
       5 + ((purchase_order_id + line_no * 3) % 200),
       CASE WHEN purchase_order_id % 5 = 3
            THEN 5 + ((purchase_order_id + line_no * 3) % 200)
            ELSE (purchase_order_id + line_no) % (6 + ((purchase_order_id + line_no * 3) % 200))
       END,
       (5 + ((purchase_order_id * 31 + line_no * 107) * 43 % 300000) / 100.0)::numeric(12, 2)
FROM generate_series(1, 120000) AS purchase_order_id
CROSS JOIN generate_series(1, 4) AS line_no
ON CONFLICT DO NOTHING;

INSERT INTO logistics.shipments (
    shipment_id, purchase_order_id, carrier_id, origin_facility_id,
    destination_facility_id, shipment_status, tracking_code,
    shipped_at, promised_at, delivered_at, shipping_cost
)
SELECT shipment_id,
       ((shipment_id - 1) % 120000) + 1,
       ((shipment_id - 1) % 20) + 1,
       ((shipment_id - 1) % 30) + 1,
       (shipment_id % 30) + 1,
       (ARRAY['created', 'picked_up', 'in_transit', 'delivered', 'exception'])[
           (shipment_id % 5) + 1
       ],
       'LOG-' || lpad(shipment_id::text, 14, '0'),
       CASE WHEN shipment_id % 5 = 0 THEN NULL
            ELSE timestamptz '2023-01-02 00:00:00+00'
                + (shipment_id % 1095) * interval '1 day'
       END,
       timestamptz '2023-01-05 00:00:00+00'
           + (shipment_id % 1095) * interval '1 day'
           + (shipment_id % 7) * interval '1 day',
       CASE WHEN shipment_id % 5 = 3
            THEN timestamptz '2023-01-04 00:00:00+00'
                + (shipment_id % 1095) * interval '1 day'
                + (shipment_id % 10) * interval '1 day'
            ELSE NULL
       END,
       (15 + (shipment_id * 29 % 100000) / 100.0)::numeric(12, 2)
FROM generate_series(1, 150000) AS shipment_id
ON CONFLICT DO NOTHING;

INSERT INTO logistics.shipment_items (shipment_id, line_no, item_id, quantity)
SELECT shipment_id,
       line_no,
       ((shipment_id * 41 + line_no * 131 - 1) % 20000) + 1,
       1 + ((shipment_id + line_no) % 80)
FROM generate_series(1, 150000) AS shipment_id
CROSS JOIN generate_series(1, 3) AS line_no
ON CONFLICT DO NOTHING;

INSERT INTO logistics.tracking_events (
    tracking_event_id, shipment_id, facility_id, event_type,
    event_time, event_payload
)
SELECT (shipment_id - 1) * 5 + event_no,
       shipment_id,
       ((shipment_id + event_no - 2) % 30) + 1,
       (ARRAY['label_created', 'departed', 'arrived', 'customs', 'delivered', 'exception'])[
           ((shipment_id + event_no) % 6) + 1
       ],
       timestamptz '2023-01-01 00:00:00+00'
           + (shipment_id % 1095) * interval '1 day'
           + event_no * interval '6 hours',
       jsonb_build_object('sequence', event_no, 'temperature', 2 + shipment_id % 18)
FROM generate_series(1, 150000) AS shipment_id
CROSS JOIN generate_series(1, 5) AS event_no
ON CONFLICT DO NOTHING;

INSERT INTO logistics.stock_movements (
    movement_id, facility_id, item_id, movement_type,
    quantity_delta, reference_code, occurred_at
)
SELECT movement_id,
       ((movement_id * 11 - 1) % 30) + 1,
       ((movement_id * 37 - 1) % 20000) + 1,
       (ARRAY['receipt', 'dispatch', 'transfer', 'adjustment'])[(movement_id % 4) + 1],
       CASE WHEN movement_id % 4 IN (1, 3)
            THEN 1 + movement_id % 100
            ELSE -(1 + movement_id % 80)
       END,
       'MOVE-' || lpad(movement_id::text, 12, '0'),
       timestamptz '2023-01-01 00:00:00+00'
           + (movement_id % 1095) * interval '1 day'
           + (movement_id % 86400) * interval '1 second'
FROM generate_series(1, 300000) AS movement_id
ON CONFLICT DO NOTHING;

SELECT setval(pg_get_serial_sequence('logistics.suppliers', 'supplier_id'), 5000, true);
SELECT setval(pg_get_serial_sequence('logistics.facilities', 'facility_id'), 30, true);
SELECT setval(pg_get_serial_sequence('logistics.items', 'item_id'), 20000, true);
SELECT setval(pg_get_serial_sequence('logistics.carriers', 'carrier_id'), 20, true);
SELECT setval(pg_get_serial_sequence('logistics.purchase_orders', 'purchase_order_id'), 120000, true);
SELECT setval(pg_get_serial_sequence('logistics.shipments', 'shipment_id'), 150000, true);
SELECT setval(pg_get_serial_sequence('logistics.tracking_events', 'tracking_event_id'), 750000, true);
SELECT setval(pg_get_serial_sequence('logistics.stock_movements', 'movement_id'), 300000, true);

ANALYZE logistics.suppliers;
ANALYZE logistics.facilities;
ANALYZE logistics.items;
ANALYZE logistics.purchase_orders;
ANALYZE logistics.purchase_order_lines;
ANALYZE logistics.carriers;
ANALYZE logistics.shipments;
ANALYZE logistics.shipment_items;
ANALYZE logistics.tracking_events;
ANALYZE logistics.stock_movements;
