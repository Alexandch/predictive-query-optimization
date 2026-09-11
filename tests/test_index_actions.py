import unittest

from pqo.index_actions import (
    IndexAction,
    IndexActionKind,
    generate_index_actions,
    generate_sequential_index_actions,
)
from pqo.index_environment import IndexExperimentEnvironment


class IndexActionTests(unittest.TestCase):
    def test_generates_single_composite_and_covering_candidates(self):
        actions = generate_index_actions(
            """
            SELECT f.flight_id, f.status, a.airport_name
            FROM aviation.flights AS f
            JOIN aviation.airports AS a
              ON a.airport_code = f.departure_airport
            WHERE f.status = 'Scheduled'
            ORDER BY f.scheduled_departure
            """
        )

        self.assertEqual(actions[0].kind, IndexActionKind.NOOP)
        self.assertIn(
            IndexAction.create("aviation", "flights", ("status",)),
            actions,
        )
        self.assertTrue(
            any(
                action.table_name == "flights"
                and len(action.key_columns) > 1
                for action in actions
            )
        )
        self.assertTrue(any(action.include_columns for action in actions))

    def test_rejects_unsafe_identifier(self):
        with self.assertRaises(ValueError):
            IndexAction.create("aviation", "flights; DROP TABLE x", ("status",))

    def test_sequential_actions_use_stop_instead_of_noop(self):
        actions = generate_sequential_index_actions(
            "SELECT * FROM aviation.flights WHERE status = 'Scheduled'"
        )

        self.assertEqual(actions[0], IndexAction.stop())
        self.assertNotIn(IndexAction.noop(), actions)
        self.assertTrue(any(action.kind is IndexActionKind.CREATE for action in actions))

    def test_stop_rejects_index_fields(self):
        with self.assertRaises(ValueError):
            IndexAction(IndexActionKind.STOP, "aviation")

    def test_one_step_environment_rejects_sequential_stop(self):
        with self.assertRaisesRegex(ValueError, "only NOOP and CREATE"):
            IndexExperimentEnvironment().evaluate("SELECT 1", IndexAction.stop())

    def test_does_not_generate_actions_outside_allowlist(self):
        actions = generate_index_actions(
            "SELECT * FROM public.users WHERE email = 'a@example.com'"
        )
        self.assertEqual(actions, (IndexAction.noop(),))

    def test_does_not_treat_order_alias_as_physical_column(self):
        actions = generate_index_actions(
            """
            SELECT scheduled_departure::date AS flight_date, COUNT(*) AS total
            FROM aviation.flights
            WHERE departure_airport = 'MSQ'
            GROUP BY scheduled_departure::date
            ORDER BY flight_date
            """
        )
        indexed_columns = {
            column
            for action in actions
            for column in action.key_columns + action.include_columns
        }
        self.assertNotIn("flight_date", indexed_columns)
        self.assertNotIn("total", indexed_columns)

    def test_does_not_treat_cte_output_as_include_column(self):
        actions = generate_index_actions(
            """
            WITH route_counts AS (
                SELECT departure_airport, COUNT(*) AS flight_count
                FROM aviation.flights
                WHERE status = 'Scheduled'
                GROUP BY departure_airport
            )
            SELECT departure_airport, flight_count
            FROM route_counts
            ORDER BY flight_count DESC
            """
        )
        indexed_columns = {
            column
            for action in actions
            for column in action.key_columns + action.include_columns
        }
        self.assertNotIn("flight_count", indexed_columns)

    def test_does_not_index_cte_declared_or_computed_columns(self):
        actions = generate_index_actions(
            """
            WITH RECURSIVE calendar(day) AS (
                SELECT DATE '2025-01-01'
                UNION ALL SELECT day + 1 FROM calendar
                WHERE day < DATE '2025-01-10'
            ), traffic AS (
                SELECT scheduled_departure::date AS day, COUNT(*) AS flights
                FROM aviation.flights GROUP BY scheduled_departure::date
            )
            SELECT c.day, COALESCE(t.flights, 0) AS daily_flights
            FROM calendar c LEFT JOIN traffic t USING (day)
            ORDER BY c.day
            """
        )
        indexed_columns = {
            column
            for action in actions
            for column in action.key_columns + action.include_columns
        }
        self.assertNotIn("day", indexed_columns)
        self.assertNotIn("flights", indexed_columns)
        self.assertNotIn("daily_flights", indexed_columns)


if __name__ == "__main__":
    unittest.main()
