import unittest
from contextlib import nullcontext

from pqo.structural_advisor import (
    RecommendationCategory,
    RecommendationPriority,
    StructuralRecommendation,
)
from pqo.structural_feedback import (
    MeasurementOutcome,
    RecommendationDecision,
    record_recommendation_decision,
    record_recommendation_measurement,
    save_structural_recommendations,
)


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, *, ready=True, update_row=None):
        self.ready = ready
        self.update_row = update_row
        self.calls = []
        self.next_id = 70

    def transaction(self):
        return nullcontext()

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        if "to_regclass" in sql:
            return _Result((self.ready,))
        if "INSERT INTO pqo.structural_recommendation" in sql:
            self.next_id += 1
            return _Result((self.next_id,))
        if "UPDATE pqo.structural_recommendation" in sql:
            return _Result(self.update_row)
        raise AssertionError(sql)


def _recommendation():
    return StructuralRecommendation(
        RecommendationCategory.JOIN,
        "cartesian-join",
        RecommendationPriority.HIGH,
        "Проверить JOIN",
        "Нет ON",
        "Добавить условие",
        "Сравнить EXPLAIN",
    )


class StructuralFeedbackTests(unittest.TestCase):
    def test_persists_recommendation_and_returns_database_id(self):
        connection = _Connection()

        saved = save_structural_recommendations(
            12, (_recommendation(),), connection=connection
        )

        self.assertEqual(saved[0].recommendation_id, 71)
        parameters = connection.calls[-1][1]
        self.assertEqual(parameters[0:4], (12, "join", "cartesian-join", "high"))

    def test_requires_installed_feedback_schema(self):
        with self.assertRaisesRegex(RuntimeError, "007_structural_feedback.sql"):
            save_structural_recommendations(
                12, (_recommendation(),), connection=_Connection(ready=False)
            )

    def test_records_explicit_decision(self):
        connection = _Connection(update_row=(71, "accepted", None, None))

        update = record_recommendation_decision(
            71,
            RecommendationDecision.ACCEPTED,
            "  useful  ",
            connection=connection,
        )

        self.assertEqual(update.status, "accepted")
        self.assertEqual(connection.calls[-1][1], ("accepted", "useful", 71))

    def test_measurement_classifies_improvement(self):
        connection = _Connection(update_row=(71, "accepted", 0.25, "improved"))

        update = record_recommendation_measurement(
            71, 120.0, 90.0, connection=connection
        )

        self.assertEqual(update.measurement_outcome, MeasurementOutcome.IMPROVED)
        self.assertAlmostEqual(update.measured_improvement_ratio, 0.25)
        self.assertAlmostEqual(connection.calls[-1][1][2], 0.25)

    def test_measurement_requires_accepted_recommendation(self):
        connection = _Connection(update_row=None)

        with self.assertRaisesRegex(ValueError, "must be accepted"):
            record_recommendation_measurement(
                71, 10.0, 8.0, connection=connection
            )

    def test_measurement_rejects_invalid_baseline(self):
        with self.assertRaisesRegex(ValueError, "must be positive"):
            record_recommendation_measurement(
                71, 0.0, 0.0, connection=_Connection()
            )


if __name__ == "__main__":
    unittest.main()
