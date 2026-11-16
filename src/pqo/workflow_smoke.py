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
                        / "examples/transactions/retail-insert-select-savepoints.sql"
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
                    window.transaction_table.setCurrentCell(6, 0)
                    window.workflow_send_select_button.click()
                    assert window._workflow_select_link[3] == 7
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
                }
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
            310000,
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
