-- Source: pqo_upsert_source, schema: retail. Reference: pqo_upsert_index_lab.
-- Execute through disposable copies; repeatable rows AND sequence state required.
BEGIN ISOLATION LEVEL REPEATABLE READ, READ WRITE;

INSERT INTO retail.inventory(sku, warehouse_id, quantity)
VALUES ('demo-new', 8, 5)
RETURNING stock_id, quantity;

INSERT INTO retail.inventory(sku, warehouse_id, quantity)
VALUES ('demo-new', 8, 99)
ON CONFLICT (sku, warehouse_id) DO NOTHING
RETURNING stock_id;

INSERT INTO retail.inventory AS current_stock(sku, warehouse_id, quantity)
VALUES ('demo-new', 8, 3)
ON CONFLICT (sku, warehouse_id) DO UPDATE
SET quantity = current_stock.quantity + EXCLUDED.quantity
RETURNING stock_id, quantity;

INSERT INTO retail.inventory AS current_stock(sku, warehouse_id, quantity)
VALUES ('demo-new', 8, 0)
ON CONFLICT (sku, warehouse_id) DO UPDATE
SET quantity = EXCLUDED.quantity
WHERE EXCLUDED.quantity > 0
RETURNING stock_id;

SAVEPOINT discarded;
INSERT INTO retail.inventory(sku, warehouse_id, quantity)
VALUES ('demo-new', 8, 100)
ON CONFLICT (sku, warehouse_id) DO UPDATE
SET quantity = EXCLUDED.quantity
RETURNING stock_id, quantity;
ROLLBACK TO discarded;
RELEASE discarded;

INSERT INTO retail.inventory_log(stock_id) VALUES (30001) RETURNING log_id;

SELECT stock_id, warehouse_id, quantity, status
FROM retail.inventory
WHERE warehouse_id = 8 AND (status = 'active' OR status = 'reserved')
ORDER BY stock_id;

SELECT log_id, stock_id, note FROM retail.inventory_log ORDER BY log_id;
COMMIT;
