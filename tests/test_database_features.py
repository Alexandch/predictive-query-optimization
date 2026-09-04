import unittest

from pqo.database_features import extract_relation_references


class DatabaseFeatureTests(unittest.TestCase):
    def test_extracts_schema_qualified_relations_and_ignores_ctes(self):
        references = extract_relation_references(
            """
            WITH recent AS (
                SELECT flight_id FROM aviation.flights
            )
            SELECT r.flight_id, b.book_ref
            FROM recent AS r
            JOIN aviation.ticket_flights AS tf USING (flight_id)
            JOIN bookings AS b ON b.book_ref = '000001'
            """
        )

        self.assertEqual(
            references,
            (
                ("aviation", "flights"),
                ("aviation", "ticket_flights"),
                ("public", "bookings"),
            ),
        )

    def test_empty_relation_set_for_constant_select(self):
        self.assertEqual(extract_relation_references("SELECT 1"), ())


if __name__ == "__main__":
    unittest.main()
