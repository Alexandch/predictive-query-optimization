import unittest

from pqo.structural_advisor import (
    RecommendationCategory,
    RecommendationPriority,
    StructuralRecommendation,
)
from pqo.structural_validation import (
    StructuralValidationResult,
    _structural_rewrite,
    save_structural_validation,
    validate_structural_recommendation,
)


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        if "to_regclass" in str(sql):
            return _Result((True,))
        return _Result((91,))


def _recommendation(rule_id, *, recommendation_id=7, suggested_sql=None):
    return StructuralRecommendation(
        RecommendationCategory.AGGREGATION,
        rule_id,
        RecommendationPriority.MEDIUM,
        "Title",
        "Evidence",
        "Action",
        "Verify",
        suggested_sql,
        recommendation_id,
    )


class StructuralValidationTests(unittest.TestCase):
    def test_moves_only_nonaggregate_having_terms(self):
        rewritten = _structural_rewrite(
            "SELECT status, count(*) FROM orders GROUP BY status "
            "HAVING status = 'paid' AND count(*) > 5",
            "non-aggregate-having-filter",
        )

        self.assertIn("WHERE", rewritten)
        self.assertIn("status = 'paid'", rewritten)
        self.assertIn("HAVING", rewritten)
        self.assertIn("COUNT(*) > 5", rewritten)

    def test_removes_only_inner_order_without_limit(self):
        rewritten = _structural_rewrite(
            "SELECT * FROM (SELECT * FROM orders ORDER BY created_at) q "
            "ORDER BY q.id",
            "subquery-order-without-limit",
        )

        self.assertNotIn("ORDER BY created_at", rewritten)
        self.assertEqual(rewritten.count("ORDER BY"), 1)
        self.assertIn("q.id", rewritten)

    def test_reports_unsupported_rule_without_connecting(self):
        result = validate_structural_recommendation(
            "SELECT DISTINCT id FROM orders",
            _recommendation("distinct-masks-join-multiplication"),
        )

        self.assertFalse(result.supported)
        self.assertTrue(result.rolled_back)
        self.assertIn("manual_verification", result.details)

    def test_saves_validation_evidence(self):
        result = StructuralValidationResult(
            recommendation_id=7,
            rule_id="non-aggregate-having-filter",
            supported=True,
            validation_method="having-filter-rewrite",
            equivalent=True,
            baseline_time_ms=10.0,
            candidate_time_ms=7.0,
            improvement_ratio=0.3,
            accepted=True,
            rolled_back=True,
            details={"rewritten_sql": "SELECT 1"},
        )
        connection = _Connection()

        saved = save_structural_validation(result, connection=connection)

        self.assertEqual(saved.validation_id, 91)
        parameters = connection.calls[-1][1]
        self.assertEqual(parameters[0:3], (7, "having-filter-rewrite", True))
        self.assertTrue(parameters[8])


if __name__ == "__main__":
    unittest.main()
