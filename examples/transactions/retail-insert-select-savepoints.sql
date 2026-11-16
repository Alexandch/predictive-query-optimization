-- Source: pqo_insert_source, schema: retail. Reference: pqo_insert_index_lab.
-- Run through disposable copies; keep source ORDER BY to reproduce generated IDs.
BEGIN ISOLATION LEVEL REPEATABLE READ, READ WRITE;

UPDATE retail.incoming SET quantity = quantity + 1 WHERE incoming_id = 7;

INSERT INTO retail.inventory AS target(sku,warehouse_id,quantity,status)
SELECT i.sku,i.warehouse_id,i.quantity,c.status
FROM retail.incoming AS i
JOIN retail.categories AS c ON c.group_id = i.group_id
WHERE i.warehouse_id = 8 AND i.incoming_id <= 10000
ORDER BY i.incoming_id
ON CONFLICT(sku,warehouse_id) DO UPDATE
SET quantity = target.quantity + EXCLUDED.quantity, status = EXCLUDED.status
RETURNING stock_id,sku,quantity,status;

SAVEPOINT discarded;
INSERT INTO retail.inventory(sku,warehouse_id,quantity)
SELECT i.sku,i.warehouse_id,i.quantity + 100
FROM retail.incoming AS i
WHERE i.warehouse_id = 8 AND i.incoming_id <= 27
ORDER BY i.incoming_id
ON CONFLICT(sku,warehouse_id) DO UPDATE SET quantity = EXCLUDED.quantity
RETURNING stock_id,quantity;
ROLLBACK TO discarded;
RELEASE discarded;

SELECT stock_id,warehouse_id,quantity,status
FROM retail.inventory
WHERE warehouse_id = 8 AND (status = 'active' OR status = 'reserved')
ORDER BY stock_id;

SELECT incoming_id,quantity FROM retail.incoming WHERE incoming_id = 7;
COMMIT;
