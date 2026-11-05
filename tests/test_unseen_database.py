from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from pqo.config import DatabaseSettings
from pqo.unseen_database import DATABASE, ROOT, provision, queries
from pqo.explain import _assert_read_only_query
from pqo.sql_rewrite import generate_sql_rewrites


class UnseenDatabaseTests(unittest.TestCase):
    def test_examples_match_predeclared_queries(self):
        from sqlglot import parse_one
        for filename, key in (("01-index.sql", "enrollment_index"), ("02-rewrite.sql", "rewrite"),
                              ("03-heavy-join.sql", "grouped_join"), ("04-window.sql", "window")):
            sql = (ROOT / "examples/university-unseen" / filename).read_text(encoding="utf-8")
            self.assertEqual(parse_one(sql, read="postgres"), parse_one(queries()[key], read="postgres"))

    def test_existing_database_is_never_overwritten(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (1,)
        with patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value = connection
            with self.assertRaises(FileExistsError):
                provision(DatabaseSettings())
        self.assertEqual(connection.execute.call_count, 1)
        self.assertEqual(connection.execute.call_args.args[1], (DATABASE,))

    def test_read_only_matrix_has_rewrite_and_negative_control(self):
        for sql in queries().values():
            _assert_read_only_query(sql)
        self.assertFalse(generate_sql_rewrites(queries()["primary_key"]))
        self.assertTrue(any(item.rule_id == "count-positive-to-exists"
                            for item in generate_sql_rewrites(queries()["rewrite"])))
