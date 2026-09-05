import json
import tempfile
import unittest
from pathlib import Path

from pqo.dqn_negative_cli import normalize_unused_index_rewards, summarize
from pqo.dqn_negative_queries import DQNNegativeQueryGenerator
from pqo.explain import _assert_read_only_query
from pqo.index_actions import IndexActionKind, generate_index_actions
from pqo.logistics_control_queries import LogisticsControlQueryGenerator
from pqo.production_queries import ProductionQueryGenerator
from pqo.query_generator import AviationQueryGenerator
from pqo.retail_control_queries import RetailControlQueryGenerator
from pqo.retail_queries import RetailQueryGenerator


class DQNNegativeQueryTests(unittest.TestCase):
    def test_templates_are_training_only_read_only_and_have_candidates(self):
        generator = DQNNegativeQueryGenerator(seed=1)
        cases = generator.generate_one_per_template()
        known = {
            *AviationQueryGenerator.TEMPLATE_IDS,
            *RetailQueryGenerator.TEMPLATE_IDS,
            *ProductionQueryGenerator.TEMPLATE_IDS,
            *RetailControlQueryGenerator.TEMPLATE_IDS,
            *LogisticsControlQueryGenerator.TEMPLATE_IDS,
        }

        self.assertEqual(len(cases), 16)
        self.assertTrue(known.isdisjoint(generator.template_ids))
        for case in cases:
            self.assertEqual(_assert_read_only_query(case.sql_text), case.sql_text)
            actions = generate_index_actions(
                case.sql_text,
                allowed_schemas=frozenset({"aviation", "retail"}),
            )
            self.assertTrue(any(action.kind is IndexActionKind.CREATE for action in actions))
            self.assertNotIn("logistics.", case.sql_text.lower())

    def test_summary_counts_negative_actions_and_noop_groups(self):
        rows = (
            '{"query_id":"a","template_id":"t","action":{"kind":"noop"},"reward":0}\n'
            '{"query_id":"a","template_id":"t","action":{"kind":"create"},"reward":-0.2}\n'
            '{"query_id":"b","template_id":"u","action":{"kind":"noop"},"reward":0}\n'
            '{"query_id":"b","template_id":"u","action":{"kind":"create"},"reward":0.3}\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experience.jsonl"
            path.write_text(rows, encoding="utf-8")
            metrics = summarize(path)

        self.assertEqual(metrics["query_count"], 2)
        self.assertEqual(metrics["noop_best_count"], 1)
        self.assertEqual(metrics["negative_action_count"], 1)

    def test_normalizes_only_indexes_absent_from_execution_plan(self):
        rows = (
            '{"action":{"kind":"noop"},"reward":0,"candidate_uses_index":false}\n'
            '{"action":{"kind":"create","key_columns":["a"],"include_columns":["b"]},'
            '"reward":0.4,"candidate_uses_index":false}\n'
            '{"action":{"kind":"create","key_columns":["a"],"include_columns":[]},'
            '"reward":0.3,"candidate_uses_index":true}\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "raw.jsonl"
            output = Path(directory) / "normalized.jsonl"
            source.write_text(rows, encoding="utf-8")
            self.assertEqual(normalize_unused_index_rewards(source, output), 3)
            normalized = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(normalized[0]["reward"], 0)
        self.assertAlmostEqual(normalized[1]["reward"], -0.015)
        self.assertEqual(normalized[1]["measured_reward"], 0.4)
        self.assertEqual(normalized[2]["reward"], 0.3)


if __name__ == "__main__":
    unittest.main()
