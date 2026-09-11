import json
from pathlib import Path
import tempfile
import unittest

from pqo.index_actions import IndexAction
from pqo.sequential_rollout import _choose_model_action, _unique_cases


class SequentialRolloutTests(unittest.TestCase):
    def test_threshold_selects_stop_or_best_create(self):
        stop = IndexAction.stop()
        first = IndexAction.create("public", "orders", ("customer_id",))
        second = IndexAction.create("public", "orders", ("created_at",))
        actions = [stop, first, second]

        self.assertEqual(_choose_model_action(actions, [0.0, 0.2, 0.3], 0.4), stop)
        self.assertEqual(_choose_model_action(actions, [0.0, 0.5, 0.6], 0.4), second)

    def test_unique_cases_deduplicates_repeated_transitions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.jsonl"
            rows = [
                {"episode_id": "e", "template_id": "t", "sql_text": "SELECT 1"},
                {"episode_id": "e", "template_id": "t", "sql_text": "SELECT 1"},
            ]
            path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )

            self.assertEqual(len(_unique_cases(path)), 1)


if __name__ == "__main__":
    unittest.main()
