-- Source: pqo_insert_source; schema: retail; use a separate lab baseline.
-- All writes run only on fresh disposable copies from the transaction workspace.
-- SELECT 8 has an OR-to-IN rewrite. Candidate: incoming(warehouse_id) INCLUDE(quantity).
BEGIN ISOLATION LEVEL REPEATABLE READ, READ WRITE;

UPDATE retail.incoming AS i
SET quantity = i.quantity + 1
FROM retail.categories AS c
WHERE i.group_id = c.group_id AND i.incoming_id = 7;

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

DELETE FROM retail.inventory AS target
USING retail.incoming AS i
WHERE target.sku = i.sku AND target.warehouse_id = i.warehouse_id
  AND i.warehouse_id = 8 AND i.incoming_id <= 27
RETURNING target.stock_id,target.quantity;

ROLLBACK TO discarded;
RELEASE discarded;

SELECT stock_id,warehouse_id,quantity,status
FROM retail.inventory
WHERE warehouse_id = 8 AND (status = 'active' OR status = 'reserved')
ORDER BY stock_id;

SELECT incoming_id,quantity FROM retail.incoming WHERE incoming_id = 7;
COMMIT;
