-- Source: pqo_insert_source; schema: retail. Use a separate laboratory baseline.
-- SELECT 6 offers OR to IN; candidate: incoming(warehouse_id) INCLUDE(quantity).
-- work_mem 64 KiB spills the last sort; 65536 KiB shows operator memory.
BEGIN ISOLATION LEVEL REPEATABLE READ, READ WRITE;
UPDATE retail.incoming SET quantity=quantity+1 WHERE incoming_id=7
RETURNING incoming_id,quantity;
SAVEPOINT discarded;
UPDATE retail.incoming SET quantity=quantity+100 WHERE incoming_id=17
RETURNING incoming_id,quantity;
ROLLBACK TO discarded;
RELEASE discarded;
SELECT incoming_id,warehouse_id,quantity FROM retail.incoming
WHERE warehouse_id=8 OR warehouse_id=9
ORDER BY incoming_id LIMIT 500;
SELECT warehouse_id,COUNT(*) AS rows_count,SUM(quantity) AS total_quantity
FROM retail.incoming GROUP BY warehouse_id ORDER BY warehouse_id;
SELECT incoming_id,quantity FROM retail.incoming
ORDER BY quantity DESC,incoming_id LIMIT 8000;
COMMIT;
