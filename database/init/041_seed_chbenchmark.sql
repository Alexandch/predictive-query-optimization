-- Deterministic medium-scale data for the CH-benCHmark-compatible schema.
-- 12 entities and approximately 1.2 million rows in total.

INSERT INTO chbenchmark.region (r_regionkey, r_name, r_comment)
SELECT key, (ARRAY['AFRICA', 'AMERICA', 'ASIA', 'EUROPE', 'MIDDLE EAST'])[key + 1],
       'CH benchmark region ' || key
FROM generate_series(0, 4) AS key
ON CONFLICT DO NOTHING;

INSERT INTO chbenchmark.nation (n_nationkey, n_name, n_regionkey, n_comment)
SELECT key, 'NATION-' || lpad(key::text, 2, '0'), key % 5,
       'CH benchmark nation ' || key
FROM generate_series(0, 24) AS key
ON CONFLICT DO NOTHING;

INSERT INTO chbenchmark.supplier (
    su_suppkey, su_name, su_address, su_nationkey,
    su_phone, su_acctbal, su_comment
)
SELECT key,
       'Supplier-' || lpad(key::text, 5, '0'),
       'Address ' || key,
       key % 25,
       lpad((key % 1000000000000000)::text, 15, '0'),
       (-1000 + (key * 37 % 1100000) / 100.0)::numeric(12, 2),
       CASE WHEN key % 97 = 0 THEN 'Customer complaint pending'
            ELSE 'Regular supplier account' END
FROM generate_series(0, 9999) AS key
ON CONFLICT DO NOTHING;

INSERT INTO chbenchmark.warehouse (w_id, w_ytd, w_tax, w_name, w_city, w_state, w_zip)
SELECT key, 300000 + key * 25000, ((key * 17) % 2000)::numeric / 10000,
       'Warehouse-' || key, 'Commerce City ' || key,
       (ARRAY['CA', 'NY', 'TX', 'WA'])[key], '1000' || key || '1111'
FROM generate_series(1, 4) AS key
ON CONFLICT DO NOTHING;

INSERT INTO chbenchmark.district (
    d_w_id, d_id, d_ytd, d_tax, d_next_o_id, d_name, d_city, d_state, d_zip
)
SELECT w_id, d_id, 30000 + d_id * 500,
       ((w_id * 101 + d_id * 23) % 2000)::numeric / 10000,
       3001, 'District-' || d_id, 'District City ' || d_id,
       (ARRAY['CA', 'NY', 'TX', 'WA'])[w_id],
       lpad((100000000 + w_id * 10000 + d_id)::text, 9, '0')
FROM generate_series(1, 4) AS w_id
CROSS JOIN generate_series(1, 10) AS d_id
ON CONFLICT DO NOTHING;

INSERT INTO chbenchmark.customer (
    c_w_id, c_d_id, c_id, c_discount, c_credit, c_last, c_first,
    c_credit_lim, c_balance, c_ytd_payment, c_payment_cnt,
    c_delivery_cnt, c_city, c_state, c_zip, c_phone, c_since, c_data
)
SELECT w_id, d_id, c_id,
       ((c_id * 13 + d_id * 7) % 5000)::numeric / 10000,
       CASE WHEN c_id % 10 = 0 THEN 'BC' ELSE 'GC' END,
       'Last-' || lpad((c_id % 1000)::text, 3, '0'),
       'First-' || c_id,
       50000,
       (-5000 + (c_id * 97 + d_id * 41 + w_id * 19) % 1000000)::numeric / 100,
       ((c_id * 31) % 500000)::numeric / 100,
       1 + c_id % 30,
       c_id % 20,
       'Customer City ' || (c_id % 100),
       (ARRAY['CA', 'NY', 'TX', 'WA', 'FL', 'OH', 'NV'])[(c_id % 7) + 1],
       lpad((100000000 + c_id)::text, 9, '0'),
       ((c_id % 7) + 1)::text || lpad(c_id::text, 15, '0'),
       timestamp '2021-01-01' + (c_id % 1460) * interval '1 day',
       CASE WHEN c_id % 53 = 0 THEN 'Manual review: unusual payment pattern'
            ELSE 'Regular customer profile' END
FROM generate_series(1, 4) AS w_id
CROSS JOIN generate_series(1, 10) AS d_id
CROSS JOIN generate_series(1, 3000) AS c_id
ON CONFLICT DO NOTHING;

INSERT INTO chbenchmark.item (i_id, i_name, i_price, i_data, i_im_id)
SELECT key, 'Item-' || lpad(key::text, 6, '0'),
       (1 + (key * 43 % 50000) / 100.0)::numeric(8, 2),
       CASE WHEN key % 101 = 0 THEN 'PROMO premium seasonal product'
            WHEN key % 47 = 0 THEN 'clearance fragile product'
            ELSE 'standard catalog product ' || (key % 200) END,
       1 + key % 10000
FROM generate_series(1, 30000) AS key
ON CONFLICT DO NOTHING;

INSERT INTO chbenchmark.stock (
    s_w_id, s_i_id, s_quantity, s_ytd, s_order_cnt,
    s_remote_cnt, s_data
)
SELECT w_id, i_id,
       10 + (w_id * 17 + i_id * 29) % 191,
       ((i_id * 13 + w_id * 71) % 500000)::numeric / 100,
       (i_id * 7 + w_id) % 500,
       (i_id + w_id) % 40,
       CASE WHEN i_id % 89 = 0 THEN 'priority replenishment required'
            ELSE 'standard stock record' END
FROM generate_series(1, 4) AS w_id
CROSS JOIN generate_series(1, 30000) AS i_id
ON CONFLICT DO NOTHING;

INSERT INTO chbenchmark.oorder (
    o_w_id, o_d_id, o_id, o_c_id, o_carrier_id,
    o_ol_cnt, o_all_local, o_entry_d
)
SELECT w_id, d_id, o_id,
       ((o_id * 37 + d_id * 13 - 1) % 3000) + 1,
       CASE WHEN o_id > 2100 THEN NULL ELSE 1 + o_id % 10 END,
       5, o_id % 17 <> 0,
       timestamp '2023-01-01'
           + ((o_id * 7 + d_id * 11 + w_id * 17) % 1095) * interval '1 day'
           + (o_id % 86400) * interval '1 second'
FROM generate_series(1, 4) AS w_id
CROSS JOIN generate_series(1, 10) AS d_id
CROSS JOIN generate_series(1, 3000) AS o_id
ON CONFLICT DO NOTHING;

INSERT INTO chbenchmark.new_order (no_w_id, no_d_id, no_o_id)
SELECT w_id, d_id, o_id
FROM generate_series(1, 4) AS w_id
CROSS JOIN generate_series(1, 10) AS d_id
CROSS JOIN generate_series(2101, 3000) AS o_id
ON CONFLICT DO NOTHING;

INSERT INTO chbenchmark.order_line (
    ol_w_id, ol_d_id, ol_o_id, ol_number, ol_i_id,
    ol_delivery_d, ol_amount, ol_supply_w_id, ol_quantity, ol_dist_info
)
SELECT w_id, d_id, o_id, line_no,
       ((o_id * 131 + d_id * 53 + w_id * 19 + line_no * 997 - 1) % 30000) + 1,
       CASE WHEN o_id > 2100 THEN NULL
            ELSE timestamp '2023-01-02'
                + ((o_id * 7 + d_id * 11 + w_id * 17) % 1095) * interval '1 day'
                + (line_no * 3) * interval '1 hour' END,
       (1 + (o_id * 17 + line_no * 31 + d_id * 7) % 50000)::numeric / 100,
       CASE WHEN o_id % 17 = 0 THEN 1 + w_id % 4 ELSE w_id END,
       1 + (o_id + line_no) % 10,
       'District distribution ' || d_id
FROM generate_series(1, 4) AS w_id
CROSS JOIN generate_series(1, 10) AS d_id
CROSS JOIN generate_series(1, 3000) AS o_id
CROSS JOIN generate_series(1, 5) AS line_no
ON CONFLICT DO NOTHING;

INSERT INTO chbenchmark.history (
    h_id, h_c_id, h_c_d_id, h_c_w_id, h_d_id,
    h_w_id, h_date, h_amount, h_data
)
SELECT ((w_id - 1) * 30000 + (d_id - 1) * 3000 + c_id)::bigint,
       c_id, d_id, w_id, d_id, w_id,
       timestamp '2023-01-01' + (c_id % 1095) * interval '1 day',
       (10 + c_id % 5000)::numeric / 10,
       'Payment history ' || (c_id % 100)
FROM generate_series(1, 4) AS w_id
CROSS JOIN generate_series(1, 10) AS d_id
CROSS JOIN generate_series(1, 3000) AS c_id
ON CONFLICT DO NOTHING;

ANALYZE chbenchmark.region;
ANALYZE chbenchmark.nation;
ANALYZE chbenchmark.supplier;
ANALYZE chbenchmark.warehouse;
ANALYZE chbenchmark.district;
ANALYZE chbenchmark.customer;
ANALYZE chbenchmark.item;
ANALYZE chbenchmark.stock;
ANALYZE chbenchmark.oorder;
ANALYZE chbenchmark.new_order;
ANALYZE chbenchmark.order_line;
ANALYZE chbenchmark.history;
