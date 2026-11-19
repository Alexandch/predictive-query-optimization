"""Explicit native guided-workflow acceptance; known local synthetic source only."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import traceback
from uuid import uuid4

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QMessageBox

from .sql_rewrite import generate_sql_rewrites


def run_workflow_smoke(application, window_type, resources, artifacts):
    log = artifacts / "workflow-smoke-test.log"
    try:
        if os.getenv("PQO_WORKFLOW_COPY_CONFIRMED") != "1":
            raise ValueError("Explicit laboratory-copy opt-in required")
        for name in ("arial.ttf", "arialbd.ttf", "consola.ttf"):
            QFontDatabase.addApplicationFont(
                str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name)
            )
        application.setFont(QFont("Arial", 10))
        window = window_type()
        original = window._database_settings()
        if (
            original.dbname != "pqo_insert_source"
            or original.allowed_schemas != frozenset({"retail"})
        ):
            raise ValueError(
                "Only known pqo_insert_source synthetic fixture is permitted"
            )
        service = window._service_database_settings()
        model = window.xgb_model_edit.text()
        joined_dml = os.getenv('PQO_SMOKE_JOINED_DML') == '1'
        select_number = 8 if joined_dml else 7
        window.resize(1180, 760)
        window.show()
        application.processEvents()
        window.grab().save(str(artifacts / "workflow-home.png"))
        name = "pqo_wflow_" + uuid4().hex[:12]
        state = {"phase": "connect", "done": False, "stand": None}
        old_question = QMessageBox.question
        QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.Yes
        window.workflow_connect_button.click()

        def finish(code, message):
            state["done"] = True
            QMessageBox.question = old_question
            log.write_text(message, encoding="utf-8")
            application.exit(code)

        def tick():
            if state["done"] or window._active_workers:
                return
            try:
                phase = state["phase"]
                if phase == "connect":
                    assert window.workflow_copy_button.isEnabled(), (
                        window.workflow_status.text()
                    )
                    window._workflow_open_settings()
                    application.processEvents()
                    window.grab().save(str(artifacts / "workflow-connection.png"))
                    window._workflow_show_home()
                    window.workflow_copy_name.setText(name)
                    window.workflow_copy_button.click()
                    assert window._workflow_copy_control is not None, (
                        window.workflow_status.text()
                    )
                    state["phase"] = "copy"
                    return
                if phase == "copy":
                    lab = window._workflow_lab()
                    assert lab is not None, window.workflow_status.text()
                    state["stand"] = lab["stand"]
                    assert window._database_settings().dbname == name
                    assert window.transaction_replay_reference.text() == name + "_base"
                    assert (
                        window._service_database_settings() == service
                        and window.xgb_model_edit.text() == model
                    )
                    assert (
                        window.workflow_query_button.isEnabled()
                        and window.workflow_transaction_button.isEnabled()
                    )
                    application.processEvents()
                    window.grab().save(str(artifacts / "workflow-ready.png"))
                    window.workflow_transaction_button.click()
                    text = (
                        resources
                        / ('examples/transactions/retail-joined-dml-savepoints.sql' if joined_dml
                            else 'examples/transactions/retail-insert-select-savepoints.sql')
                    ).read_text(encoding="utf-8-sig")
                    state["text"] = text
                    window.transaction_editor.setPlainText(text)
                    window.transaction_inspect_button.click()
                    state["phase"] = "inspect"
                    return
                if phase == "inspect":
                    assert window.transaction_inspection is not None, (
                        window.transaction_status.text()
                    )
                    window.transaction_table.setCurrentCell(select_number - 1, 0)
                    window.workflow_send_select_button.click()
                    assert window._workflow_select_link[3] == select_number
                    candidate = next(
                        x
                        for x in generate_sql_rewrites(window.sql_editor.toPlainText())
                        if x.rule_id == "or-equality-to-in"
                    )
                    window.rewrite_editor.setPlainText(candidate.sql_text)
                    window.compare_variant_button.click()
                    state["phase"] = "single"
                    return
                if phase == "single":
                    assert window._rewrite_choice_plan is not None, (
                        window.variant_status.text()
                    )
                    assert any(
                        x.equivalent for x in window._rewrite_choice_plan.evaluations
                    )
                    application.processEvents()
                    window.grab().save(str(artifacts / "workflow-single-query.png"))
                    window.workflow_return_select_button.click()
                    assert window.transaction_views.currentIndex() == 9, (
                        window.variant_status.text()
                    )
                    assert window.transaction_editor.toPlainText() == state["text"]
                    window.transaction_select_rewrite_confirm.setChecked(True)
                    window.transaction_select_rewrite_button.click()
                    assert window._transaction_select_rewrite_active == "replay", (
                        window.transaction_select_rewrite_status.text()
                    )
                    state["phase"] = "transaction"
                    return
                if phase == "optimization-plans":
                    assert window.transaction_plan_report is not None
                    window.transaction_index_button.click()
                    state["phase"] = "optimization-indexes"
                    return
                if phase == "optimization-indexes":
                    assert window.transaction_index_report is not None
                    from PyQt6.QtCore import Qt

                    row = next(
                        i
                        for i, x in enumerate(
                            window.transaction_index_report.candidates
                        )
                        if x.status != "covered"
                        and x.action.table_name == "incoming"
                        and x.action.key_columns == ("warehouse_id",)
                        and "quantity" in x.action.include_columns
                    )
                    window.transaction_index_table.item(row, 0).setCheckState(
                        Qt.CheckState.Checked
                    )
                    window.transaction_select_rewrite_generate.click()
                    state["phase"] = "optimization-options"
                    return
                if phase == "optimization-options":
                    row = next(
                        i
                        for i, x in enumerate(
                            window._transaction_select_rewrite_options
                        )
                        if x.number == select_number and x.rule_id == "manual"
                    )
                    window.transaction_select_rewrite_choice.setCurrentIndex(row)
                    window.transaction_select_rewrite_variant.setPlainText(
                        state["variant"]
                    )
                    window.transaction_optimization_confirm.setChecked(True)
                    window.transaction_views.setCurrentIndex(10)
                    window.transaction_optimization_button.click()
                    assert window._transaction_optimization_active, (
                        window.transaction_optimization_status.text()
                    )
                    state["phase"] = "optimization-run"
                    return
                if phase == "optimization-run":
                    optimization = window.transaction_optimization_report
                    assert optimization is not None, (
                        window.transaction_optimization_status.text()
                    )
                    assert (
                        optimization.comparable
                        and optimization.cleanup_verified
                        and len(optimization.observations) == 14
                    )
                    assert len(optimization.created_databases) == 15 and all(
                        v.measurement.correctness_verified
                        for v in optimization.variants
                    )
                    report = state["rewrite_measurement"]
                    expected = report.samples[0]
                    for observation in optimization.observations:
                        sample = observation.sample
                        assert (
                            sample.final_state == expected.final_state
                            and sample.final_sequences == expected.final_sequences
                        )
                        assert [x.result_sha256 for x in sample.commands] == [
                            x.result_sha256 for x in expected.commands
                        ]
                    state["optimization"] = asdict(optimization)
                else:
                    result = window.transaction_select_rewrite_report
                    assert result is not None, (
                        window.transaction_select_rewrite_status.text()
                    )
                    report = result.measurement
                assert report.correctness_verified and report.cleanup_verified
                assert len(report.samples) == 6 and len(report.created_databases) == 7
                assert report.reference_database == name + "_base"
                assert window.transaction_editor.toPlainText() == state["text"]
                assert (
                    window._service_database_settings() == service
                    and window.xgb_model_edit.text() == model
                )
                if (
                    os.getenv("PQO_SMOKE_OPTIMIZATION") == "1"
                    and phase == "transaction"
                ):
                    state["rewrite_measurement"] = report
                    state["variant"] = (
                        window.transaction_select_rewrite_variant.toPlainText()
                    )
                    window.transaction_plan_button.click()
                    state["phase"] = "optimization-plans"
                    return
                application.processEvents()
                window.grab().save(str(artifacts / "workflow-transaction-result.png"))
                window._workflow_show_home()
                window.resize(980, 680)
                application.processEvents()
                window.grab().save(str(artifacts / "workflow-home-small.png"))
                payload = {
                    "stand": state["stand"],
                    "measurement": asdict(report),
                    "source_unchanged_by_workflow": True,
                    "joined_dml": joined_dml,
                }
                if "optimization" in state:
                    payload["optimization"] = state["optimization"]
                finish(
                    0,
                    "OK: real copy from source, baseline + working DB, individual SELECT comparison, "
                    "return variant, full sequential transaction on six disposable copies, matching data/results, "
                    "unchanged source SQL/service/models. The two prepared laboratory DBs are retained for independent verification.\n"
                    + json.dumps(payload, ensure_ascii=False, indent=2),
                )
            except Exception:
                finish(
                    3,
                    traceback.format_exc()
                    + "\n"
                    + json.dumps({"stand": state.get("stand")}, ensure_ascii=False),
                )

        timer = QTimer(window)
        timer.timeout.connect(tick)
        timer.start(100)
        # Cooperative cancellation; never hard-exit while a DB worker runs.
        QTimer.singleShot(
            910000 if os.getenv("PQO_SMOKE_OPTIMIZATION") == "1" else 310000,
            lambda: (
                window._workflow_copy_control.cancel()
                if window._workflow_copy_control
                else window._cancel_transaction_planning()
            ),
        )
        return application.exec()
    except Exception:
        log.write_text(traceback.format_exc(), encoding="utf-8")
        return 3
