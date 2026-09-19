import json

from pqo.prediction import model_error_mae_ms


def test_reads_holdout_mae_next_to_model(tmp_path):
    model = tmp_path / "model.joblib"
    model.write_bytes(b"placeholder")
    (tmp_path / "metrics.json").write_text(
        json.dumps({"parameter_holdout": {"mae_ms": 14.18}}),
        encoding="utf-8",
    )

    assert model_error_mae_ms(model) == 14.18


def test_missing_or_invalid_metrics_do_not_break_prediction(tmp_path):
    model = tmp_path / "model.joblib"
    model.write_bytes(b"placeholder")
    assert model_error_mae_ms(model) is None
    (tmp_path / "metrics.json").write_text("{}", encoding="utf-8")
    assert model_error_mae_ms(model) is None
