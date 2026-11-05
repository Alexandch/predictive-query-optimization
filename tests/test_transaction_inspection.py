import unittest
from pqo.transaction_inspection import inspect_transaction
from pqo.explain import _assert_read_only_query


class TransactionInspectionTests(unittest.TestCase):
    def test_mixed_transaction_keeps_order_and_never_permits_execution(self):
        result = inspect_transaction("BEGIN; SELECT ';' AS text; UPDATE university.students SET campus_id=7 WHERE student_id=1; SELECT 2; ROLLBACK;")
        self.assertEqual([item.kind for item in result.statements], ["select", "update", "select"])
        self.assertEqual([item.number for item in result.statements], [1, 2, 3])
        self.assertTrue(result.requires_test_copy)
        self.assertFalse(result.execution_permitted)
        self.assertEqual(result.ending, "rollback")
        self.assertFalse(result.unsupported_commands)

    def test_data_changing_cte_and_select_into_are_not_ordinary_reads(self):
        for sql in ("WITH x AS (DELETE FROM university.students RETURNING *) SELECT * FROM x",
                    "SELECT 1 INTO temp sample"):
            result = inspect_transaction(f"BEGIN; {sql}; COMMIT;")
            self.assertTrue(result.requires_test_copy)
            self.assertTrue(result.statements[0].changes_data)

    def test_unsupported_commands_and_unknown_functions_are_explicit(self):
        for sql in ("CREATE TABLE x(id int)", "SET work_mem='1MB'"):
            result = inspect_transaction(f"BEGIN; {sql}; COMMIT;")
            self.assertEqual(result.unsupported_commands, (1,))
            self.assertFalse(result.execution_permitted)
        self.assertTrue(inspect_transaction("BEGIN; SELECT custom_function(); COMMIT;").requires_test_copy)
        self.assertTrue(inspect_transaction("BEGIN; SELECT * FROM university.students FOR UPDATE; COMMIT;").requires_test_copy)

    def test_read_only_inventory_is_not_execution_permission(self):
        result = inspect_transaction("BEGIN; SELECT 1; SELECT 2; COMMIT;")
        self.assertFalse(result.requires_test_copy)
        self.assertFalse(result.execution_permitted)

    def test_partial_nested_and_chained_transactions_are_rejected(self):
        for sql in ("SELECT 1", "BEGIN; SELECT 1", "BEGIN; COMMIT;",
                    "BEGIN; BEGIN; SELECT 1; COMMIT; COMMIT;",
                    "BEGIN; SELECT 1; COMMIT AND CHAIN;",
                    "BEGIN; SELECT 1; COMMIT AND NO CHAIN;",
                    "BEGIN; SELECT 1; COMMIT; BEGIN; SELECT 2; COMMIT;"):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                inspect_transaction(sql)

    def test_existing_single_query_guard_still_rejects_transactions(self):
        with self.assertRaises(ValueError):
            _assert_read_only_query("BEGIN; SELECT 1; COMMIT;")

    def test_relation_inventory_excludes_cte_and_alias_names(self):
        result = inspect_transaction("BEGIN; WITH recent AS (SELECT s.student_id FROM university.students s) SELECT * FROM recent; COMMIT;")
        self.assertEqual(result.statements[0].relation_names, ("university.students",))
