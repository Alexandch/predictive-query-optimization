import unittest

import pandas as pd

from pqo.domain_mix import _select_pagila_fraction, infer_development_domain


class DomainMixTests(unittest.TestCase):
    def test_infers_only_development_domains(self):
        self.assertEqual(infer_development_domain("route_revenue"), "aviation")
        self.assertEqual(
            infer_development_domain("retail_category_revenue"), "retail"
        )
        self.assertEqual(
            infer_development_domain("pagila_category_revenue"), "pagila"
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


if __name__ == "__main__":
    unittest.main()
