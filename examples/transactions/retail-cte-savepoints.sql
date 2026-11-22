-- Source: pqo_insert_source; schema: retail. Use a separate laboratory baseline.
-- SELECT 8 offers NOT MATERIALIZED. Candidate: incoming(warehouse_id) INCLUDE(quantity).
BEGIN ISOLATION LEVEL REPEATABLE READ, READ WRITE;

WITH source_groups AS (
    SELECT group_id,status FROM retail.categories
    WHERE status = 'active' OR status = 'reserved'
)
UPDATE retail.incoming AS i
SET quantity = i.quantity + 1
FROM source_groups AS c
WHERE i.group_id = c.group_id AND i.incoming_id = 7;

WITH source_rows AS (
    SELECT i.sku,i.warehouse_id,i.quantity,c.status,i.incoming_id
    FROM retail.incoming AS i
    JOIN retail.categories AS c ON c.group_id = i.group_id
)
INSERT INTO retail.inventory AS target(sku,warehouse_id,quantity,status)
SELECT sku,warehouse_id,quantity,status FROM source_rows
WHERE warehouse_id = 8 AND incoming_id <= 10000
ORDER BY incoming_id
ON CONFLICT(sku,warehouse_id) DO UPDATE
SET quantity = target.quantity + EXCLUDED.quantity, status = EXCLUDED.status
RETURNING stock_id,sku,quantity,status;

SAVEPOINT discarded;
WITH pending AS NOT MATERIALIZED (
    SELECT sku,warehouse_id,quantity,incoming_id FROM retail.incoming
    WHERE warehouse_id = 8 AND incoming_id <= 27
)
INSERT INTO retail.inventory(sku,warehouse_id,quantity)
SELECT sku,warehouse_id,quantity + 100 FROM pending ORDER BY incoming_id
ON CONFLICT(sku,warehouse_id) DO UPDATE SET quantity = EXCLUDED.quantity
RETURNING stock_id,quantity;

WITH pending AS (
    SELECT sku,warehouse_id FROM retail.incoming
    WHERE warehouse_id = 8 AND incoming_id <= 27
)
DELETE FROM retail.inventory AS target USING pending AS p
WHERE target.sku = p.sku AND target.warehouse_id = p.warehouse_id
RETURNING target.stock_id,target.quantity;

ROLLBACK TO discarded;
RELEASE discarded;

WITH chosen AS MATERIALIZED (
    SELECT stock_id,warehouse_id,quantity,status FROM retail.inventory
)
SELECT a.stock_id,a.warehouse_id,a.quantity,a.status
FROM chosen AS a JOIN chosen AS b ON a.stock_id = b.stock_id
WHERE a.warehouse_id = 8 AND (a.status = 'active' OR a.status = 'reserved')
ORDER BY a.stock_id;

SELECT incoming_id,quantity FROM retail.incoming WHERE incoming_id = 7;
COMMIT;
