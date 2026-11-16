-- Source: pqo_defaults_source, schema: retail.
-- Replay reference: pqo_defaults_index_lab. Execute only through disposable copies.
BEGIN ISOLATION LEVEL REPEATABLE READ, READ WRITE;
SET TRANSACTION NOT DEFERRABLE;

INSERT INTO retail.orders(customer_id, amount)
VALUES (7, 125.50)
RETURNING order_id, status;

SAVEPOINT before_trial;
INSERT INTO retail.orders DEFAULT VALUES RETURNING order_id;
ROLLBACK TO before_trial;
RELEASE before_trial;

UPDATE retail.orders SET amount = DEFAULT
WHERE order_id = 1
RETURNING order_id, amount;

INSERT INTO retail.audit(order_id, note)
VALUES (60001, 'checked')
RETURNING audit_id;

SELECT order_id, customer_id, amount, status
FROM retail.orders
WHERE customer_id = 7 AND (status = 'new' OR status = 'paid')
ORDER BY order_id;

SELECT audit_id, order_id, note FROM retail.audit ORDER BY audit_id;
COMMIT;
