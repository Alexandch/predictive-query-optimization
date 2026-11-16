-- Synthetic standalone lab. Provisioner refuses to overwrite an existing DB.
CREATE SCHEMA retail;
CREATE TABLE retail.orders (
    order_id bigserial PRIMARY KEY,
    customer_id integer NOT NULL DEFAULT 0,
    amount numeric(12,2) NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'new',
    created_at timestamp NOT NULL DEFAULT '2026-01-01 00:00:00'::timestamp
);
CREATE TABLE retail.audit (
    audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id bigint NOT NULL,
    note text NOT NULL DEFAULT 'created'
);
INSERT INTO retail.orders(customer_id,amount,status)
SELECT i%200, (i%10000)::numeric/100, 'paid'
FROM generate_series(1,60000) AS i;
ANALYZE retail.orders;
ANALYZE retail.audit;
