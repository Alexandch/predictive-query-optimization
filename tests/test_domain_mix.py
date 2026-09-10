import unittest

import pandas as pd

from pqo.domain_mix import (
    _dqn_outer_template_split,
    _select_dqn_pagila_fraction,
    _select_pagila_fraction,
    infer_development_domain,
)


class DomainMixTests(unittest.TestCase):
    def test_infers_only_development_domains(self):
        self.assertEqual(infer_development_domain("route_revenue"), "aviation")
        self.assertEqual(
            infer_development_domain("retail_category_revenue"), "retail"
        )
        self.assertEqual(
            infer_development_domain("pagila_category_revenue"), "pagila"
        )
        self.assertEqual(
            infer_development_domain("negative_retail_price_expression"), "retail"
        )

    def test_pagila_fractions_are_nested(self):
        frame = pd.DataFrame(
            {
                "domain": ["aviation"] + ["pagila"] * 8,
                "template_id": ["route"] + ["pagila_a"] * 4 + ["pagila_b"] * 4,
            }
        )
        indices = frame.index.tolist()

        quarter = set(
            _select_pagila_fraction(frame, indices, fraction=0.25, seed=21)
        )
        half = set(_select_pagila_fraction(frame, indices, fraction=0.5, seed=21))
        full = set(_select_pagila_fraction(frame, indices, fraction=1.0, seed=21))

        self.assertLessEqual(quarter, half)
        self.assertLessEqual(half, full)

    def test_dqn_sampling_keeps_query_groups_nested(self):
        records = []
        for template in ("pagila_a", "pagila_b"):
            for query in range(4):
                for action in range(3):
                    records.append(
                        {
                            "template_id": template,
                            "query_id": f"{template}-{query}",
                            "action": action,
                        }
                    )
        records.append(
            {"template_id": "route", "query_id": "aviation-1", "action": 0}
        )

        quarter = _select_dqn_pagila_fraction(records, fraction=0.25, seed=21)
        half = _select_dqn_pagila_fraction(records, fraction=0.5, seed=21)
        quarter_ids = {record["query_id"] for record in quarter}
        half_ids = {record["query_id"] for record in half}

        self.assertLessEqual(quarter_ids, half_ids)
        for query_id in quarter_ids:
            self.assertEqual(
                sum(record["query_id"] == query_id for record in quarter),
                sum(record["query_id"] == query_id for record in records),
            )

    def test_dqn_outer_split_holds_out_templates_per_domain(self):
        records = [
            {"template_id": f"{prefix}{index}", "query_id": f"q-{prefix}-{index}"}
            for prefix in ("aviation_", "retail_", "pagila_")
            for index in range(5)
        ]
        train, holdout = _dqn_outer_template_split(records, seed=21)

        train_templates = {record["template_id"] for record in train}
        holdout_templates = {record["template_id"] for record in holdout}
        self.assertTrue(train_templates.isdisjoint(holdout_templates))
        self.assertEqual(len(holdout_templates), 3)


if __name__ == "__main__":
    unittest.main()
