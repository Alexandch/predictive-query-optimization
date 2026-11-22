-- Source: pqo_insert_source; schema: retail. Use a separate laboratory baseline.
-- SELECT 8: filtering JOIN to EXISTS. SELECT 9: positive IN to EXISTS.
-- Candidate: incoming(warehouse_id) INCLUDE(quantity).
BEGIN ISOLATION LEVEL REPEATABLE READ, READ WRITE;

UPDATE retail.incoming AS i SET quantity = i.quantity + 1
WHERE i.incoming_id = 7 AND EXISTS (
    SELECT 1 FROM retail.categories AS c
    WHERE c.group_id = i.group_id AND c.status IN ('active', 'reserved')
);

INSERT INTO retail.inventory AS target(sku,warehouse_id,quantity,status)
SELECT i.sku,i.warehouse_id,i.quantity,c.status
FROM retail.incoming AS i
JOIN retail.categories AS c ON c.group_id = i.group_id
WHERE i.warehouse_id = 8 AND i.incoming_id <= 10000
  AND i.group_id IN (SELECT g.group_id FROM retail.categories AS g)
ORDER BY i.incoming_id
ON CONFLICT(sku,warehouse_id) DO UPDATE
SET quantity = target.quantity + EXCLUDED.quantity, status = EXCLUDED.status
RETURNING stock_id,sku,quantity,status;

SAVEPOINT discarded;
INSERT INTO retail.inventory(sku,warehouse_id,quantity)
SELECT i.sku,i.warehouse_id,i.quantity + 100 FROM retail.incoming AS i
WHERE i.warehouse_id = 8 AND i.incoming_id <= 27 AND NOT EXISTS (
    SELECT 1 FROM retail.categories AS c
    WHERE c.group_id = i.group_id AND c.status = 'blocked'
)
ORDER BY i.incoming_id
ON CONFLICT(sku,warehouse_id) DO UPDATE SET quantity = EXCLUDED.quantity
RETURNING stock_id,quantity;

DELETE FROM retail.inventory AS target USING retail.incoming AS p
WHERE target.sku = p.sku AND target.warehouse_id = p.warehouse_id
  AND p.warehouse_id = 8 AND p.incoming_id <= 27
  AND EXISTS (SELECT 1 FROM retail.categories AS c WHERE c.group_id = p.group_id)
RETURNING target.stock_id,target.quantity;

ROLLBACK TO discarded;
RELEASE discarded;

SELECT DISTINCT a.stock_id,a.warehouse_id,a.quantity,a.status
FROM retail.inventory AS a
JOIN retail.incoming AS i ON i.warehouse_id = a.warehouse_id
WHERE a.warehouse_id = 8 AND (a.status = 'active' OR a.status = 'reserved')
ORDER BY a.stock_id;

SELECT i.incoming_id,i.quantity FROM retail.incoming AS i
WHERE i.incoming_id = 7 AND i.group_id IN (
    SELECT c.group_id FROM retail.categories AS c
);
COMMIT;
