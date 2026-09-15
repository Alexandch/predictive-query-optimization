import json
from pathlib import Path
import tempfile
import unittest

from pqo.dqn_features import STATE_FEATURE_NAMES
from pqo.index_actions import IndexAction
from pqo.index_environment import IndexExperimentResult
from pqo.sql_rewrite import (
    SQLRewriteCandidate,
    SQLRewriteEvaluation,
    SQLRewritePlan,
)
from pqo.strategy import (
    STRATEGY_FEATURE_NAMES,
    StrategyKind,
    build_strategy_features,
    collect_strategy_experience,
    choose_strategy_label,
    summarize_strategy_experience,
    train_strategy_classifier,
)
from pqo.strategy_queries import generate_strategy_workload


def rewrite_plan(improvement_ratio=None, absolute_gain_ms=0.0, equivalent=True):
    candidate = SQLRewriteCandidate("or-equality-to-in", "OR to IN", "SELECT 1")
    baseline = 100.0 if improvement_ratio is not None else None
    rewritten = (
        baseline - absolute_gain_ms if baseline is not None else None
    )
    evaluation = SQLRewriteEvaluation(
        candidate=candidate,
        equivalent=equivalent,
        baseline_time_ms=baseline,
        rewritten_time_ms=rewritten,
        improvement_ratio=improvement_ratio,
        accepted=False,
        rejection_reason=None,
    )
    return SQLRewritePlan("SELECT 1", (evaluation,), None, 0.0, 0.0, 0.0, "test")


def index_result(reward, absolute_gain_ms, uses_index=True):
    return IndexExperimentResult(
        action=IndexAction.create("retail", "orders", ("status",)),
        baseline_time_ms=100.0,
        candidate_time_ms=100.0 - absolute_gain_ms,
        improvement_ratio=absolute_gain_ms / 100.0,
        reward=reward,
        candidate_plan_cost=10.0,
        candidate_uses_index=uses_index,
    )


class StrategyTests(unittest.TestCase):
    def test_collector_rejects_sealed_control_query_before_connecting(self):
        from pqo.query_case import QueryCase

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "training schema"):
                collect_strategy_experience(
                    [QueryCase("leak", "SELECT * FROM logistics.shipments")],
                    Path(directory) / "leak.jsonl",
                )

    def test_seed_workload_uses_only_training_schemas(self):
        cases = generate_strategy_workload(
            ordinary_per_domain=1, rewrite_variants=1
        )

        self.assertEqual(len(cases), 10)
        self.assertFalse(any("logistics." in case.sql_text for case in cases))
        self.assertFalse(any("chbenchmark." in case.sql_text for case in cases))
        self.assertEqual(
            sum("COUNT(*)" in case.sql_text for case in cases),
            7,
        )

    def test_feature_contract_contains_rewrite_availability(self):
        sql = "SELECT * FROM retail.orders WHERE status = 'new' OR status = 'paid'"
        features = build_strategy_features(
            sql, [0.0] * len(STATE_FEATURE_NAMES), index_candidate_count=3
        )

        self.assertEqual(len(features), len(STRATEGY_FEATURE_NAMES))
        rule_index = STRATEGY_FEATURE_NAMES.index("rewrite_rule_or-equality-to-in")
        self.assertEqual(features[rule_index], 1.0)

    def test_noop_wins_when_improvement_is_below_absolute_gate(self):
        label = choose_strategy_label(
            [index_result(0.20, 2.0)],
            rewrite_plan(0.30, 3.0),
        )

        self.assertEqual(label, StrategyKind.NOOP)

    def test_rewrite_wins_over_weaker_valid_index(self):
        label = choose_strategy_label(
            [index_result(0.20, 20.0)],
            rewrite_plan(0.40, 40.0),
        )

        self.assertEqual(label, StrategyKind.REWRITE_QUERY)

    def test_index_must_really_appear_in_candidate_plan(self):
        label = choose_strategy_label(
            [index_result(0.40, 40.0, uses_index=False)],
            rewrite_plan(None, equivalent=False),
        )

        self.assertEqual(label, StrategyKind.NOOP)

    def test_trains_with_unseen_template_split_and_writes_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            experience = root / "strategy.jsonl"
            labels = [kind.value for kind in StrategyKind]
            rows = []
            for label_index, label in enumerate(labels):
                for template_index in range(3):
                    for parameter_index in range(4):
                        features = [0.0] * len(STRATEGY_FEATURE_NAMES)
                        features[label_index] = 10.0 + parameter_index
                        rows.append(
                            {
                                "template_id": f"{label}_{template_index}",
                                "query_id": f"{label}_{template_index}_{parameter_index}",
                                "label": label,
                                "strategy_features": features,
                                "strategy_feature_names": list(STRATEGY_FEATURE_NAMES),
                            }
                        )
            experience.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            metrics = train_strategy_classifier(experience, root / "model", seed=7)

            self.assertEqual(metrics.sample_count, 36)
            self.assertTrue(set(metrics.train_templates).isdisjoint(metrics.test_templates))
            self.assertEqual(
                summarize_strategy_experience(experience)["class_counts"],
                {label: 12 for label in labels},
            )
            self.assertTrue((root / "model" / "strategy_selector.joblib").is_file())
            self.assertTrue((root / "model" / "metrics.json").is_file())


if __name__ == "__main__":
    unittest.main()
