import json

from pqo.final_experiment import build_summary, load_sealed_metrics, write_results


def test_load_sealed_metrics_requires_all_files(tmp_path):
    try:
        load_sealed_metrics(tmp_path)
    except FileNotFoundError as error:
        assert "Required sealed metric" in str(error)
    else:
        raise AssertionError("missing controls must fail the final experiment")


def test_summary_and_exports_preserve_negative_result(tmp_path):
    rows = [
        {
            "case_id": "case",
            "category": "sort",
            "rule_id": "rule",
            "priority": "medium",
            "automatic_validation": True,
            "supported": True,
            "equivalent": True,
            "baseline_time_ms": 1.0,
            "candidate_time_ms": 1.2,
            "improvement_ratio": -0.2,
            "accepted": False,
            "rolled_back": True,
            "validation_method": "rewrite",
            "artifact_creation_time_ms": None,
        }
    ]
    summary = build_summary({"control": {"r2": -0.5}}, rows, repetitions=3)
    write_results(summary, tmp_path)

    saved = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert saved["training_performed"] is False
    assert saved["sealed_metrics"]["control"]["r2"] == -0.5
    assert saved["structural_validation"]["accepted_count"] == 0
    assert "case,sort,rule" in (tmp_path / "structural_results.csv").read_text(
        encoding="utf-8-sig"
    )
