-- Standalone synthetic lab; provisioner refuses existing source/reference names.
CREATE SCHEMA retail;
CREATE TABLE retail.inventory (
    stock_id bigserial PRIMARY KEY,
    sku text NOT NULL,
    warehouse_id integer NOT NULL DEFAULT 1,
    quantity integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'active',
    UNIQUE (sku,warehouse_id)
);
CREATE TABLE retail.inventory_log (
    log_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stock_id bigint NOT NULL,
    note text NOT NULL DEFAULT 'checked'
);
INSERT INTO retail.inventory(sku,warehouse_id,quantity)
SELECT 'item-'||i, i%10+1, i%100 FROM generate_series(1,30000) AS i;
ANALYZE retail.inventory;
ANALYZE retail.inventory_log;
