-- Source: pqo_insert_source. Use a separate laboratory reference.
BEGIN ISOLATION LEVEL READ COMMITTED;

SELECT stock_id, quantity
FROM retail.inventory
WHERE stock_id = 1;

UPDATE retail.inventory
SET quantity = quantity + 1
WHERE stock_id = 1
RETURNING stock_id, quantity;

COMMIT;
