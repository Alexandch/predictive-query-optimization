import unittest

from pqo.sequential_environment import (
    SequentialIndexEnvironment,
    _plan_index_names,
)


class SequentialEnvironmentTests(unittest.TestCase):
    def test_validates_budget_steps_and_warmup(self):
        with self.assertRaises(ValueError):
            SequentialIndexEnvironment(max_steps=0)
        with self.assertRaises(ValueError):
            SequentialIndexEnvironment(storage_budget_bytes=0)
        with self.assertRaises(ValueError):
            SequentialIndexEnvironment(warmup_runs=-1)

    def test_extracts_index_names_from_nested_json_plan(self):
        plan = [
            {
                "Plan": {
                    "Node Type": "Nested Loop",
                    "Plans": [
                        {"Node Type": "Index Scan", "Index Name": "pqo_seq_a"},
                        {
                            "Node Type": "Bitmap Index Scan",
                            "Index Name": "pqo_seq_b",
                        },
                    ],
                }
            }
        ]

        self.assertEqual(_plan_index_names(plan), {"pqo_seq_a", "pqo_seq_b"})


if __name__ == "__main__":
    unittest.main()
