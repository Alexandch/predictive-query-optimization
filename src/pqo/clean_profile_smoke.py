"""Explicit GUI acceptance on a new loopback laboratory cluster, never defaults."""

import json
import os
from pathlib import Path
import time
import traceback

import psycopg
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QMessageBox

from .config import DatabaseSettings
from .connections import ConnectionProfile, PROFILE_KEY, SERVICE_KEY


def _read_config():
    if os.environ.get("PQO_CLEAN_ACCEPTANCE_CONFIRMED") != "1":
        raise ValueError("Explicit clean laboratory opt-in required")
    if os.environ.get("PQO_CLEAN_PHASE", "fresh") not in {"fresh", "reopen"}:
        raise ValueError("Unsupported clean acceptance phase")
    config = json.loads(
        Path(os.environ["PQO_CLEAN_ACCEPTANCE_CONFIG"]).read_text(encoding="utf-8")
    )
    if (
        config["version"] != 1
        or config["host"] != "127.0.0.1"
        or config["port"] in {5432, 55432}
        or config["sources"] != ["pqo_clean_source", "pqo_clean_second"]
        or config["service"] != "pqo_clean_service"
    ):
        raise ValueError("Only the dedicated new loopback fixture is permitted")
    settings_file = Path(os.environ["PQO_SETTINGS_FILE"]).resolve()
    if settings_file != Path(config["settings_file"]).resolve():
        raise ValueError("Isolated settings path mismatch")
    if os.environ.get("PQO_CLEAN_PHASE", "fresh") == "fresh" and settings_file.exists():
        raise ValueError("Fresh acceptance requires a nonexistent settings file")
    return config


def run_clean_profile_smoke(application, window_type, resources, artifacts):
    """Drive actual controls/workers; the parent owns all fixture lifecycle."""
    artifacts.mkdir(parents=True, exist_ok=True)
    output = artifacts / "clean-profile-smoke.json"
    window = None
    report = {
        "status": "running",
        "cases": [],
        "phase": os.environ.get("PQO_CLEAN_PHASE", "fresh"),
    }
    secret = os.environ.get("PQO_CLEAN_ACCEPTANCE_PASSWORD", "")
    reader_secret = os.environ.get("PQO_CLEAN_READER_PASSWORD", "")

    def save():
        # Never write passwords/DSNs, table rows or user SQL, including errors.
        text = json.dumps(report, ensure_ascii=False, indent=2)
        for value in (secret, reader_secret):
            if value:
                text = text.replace(value, "[redacted]")
        output.write_text(text, encoding="utf-8")

    try:
        config = _read_config()
        if not secret:
            raise ValueError(
                "Fixture password is required in environment, not settings"
            )
        base = DatabaseSettings(
            host=config["host"],
            port=config["port"],
            dbname=config["sources"][0],
            user=config["user"],
            password=secret,
            sslmode="verify-full",
            sslrootcert=config["certificate"],
            allowed_schemas=frozenset({"warehouse", "support"}),
        )
        # A random identity created by the parent must exist in both sources.
        for database in config["sources"]:
            with psycopg.connect(
                **{**base.connection_kwargs(), "dbname": database}
            ) as conn:
                conn.execute("SET TRANSACTION READ ONLY")
                if conn.execute(
                    "SELECT token FROM support.acceptance_identity"
                ).fetchone() != (config["token"],):
                    raise ValueError(
                        "Fixture identity mismatch; no GUI actions started"
                    )
                if not conn.execute(
                    "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"
                ).fetchone()[0]:
                    raise ValueError("TLS connection not established")
        report["cases"].append("verify_full_tls_and_fixture_identity")
        for name in ("arial.ttf", "arialbd.ttf", "consola.ttf"):
            QFontDatabase.addApplicationFont(
                str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name)
            )
        application.setFont(QFont("Arial", 10))
        window = window_type()
        window.resize(1560, 1000)
        window.show()
        expected_model = resources / "models/xgboost/trial/xgboost_query_time.joblib"
        if Path(window.xgb_model_edit.text()).resolve() != expected_model.resolve():
            raise ValueError("The validated packaged plan-v1 is not the clean default")
        report["cases"].append("packaged_plan_v1_default")
        dialogs = []
        old_question, old_warning, old_critical = (
            QMessageBox.question,
            QMessageBox.warning,
            QMessageBox.critical,
        )
        QMessageBox.question = lambda *args, **kwargs: QMessageBox.StandardButton.Yes
        QMessageBox.warning = lambda *args, **kwargs: dialogs.append("warning")
        QMessageBox.critical = lambda *args, **kwargs: dialogs.append("critical")
        state = {
            "step": 0,
            "busy": False,
            "started": time.monotonic(),
            "ids": [],
            "done": False,
        }

        def finish(code, message=None):
            timer.stop()
            state["done"] = True
            report["status"] = "passed" if code == 0 else "failed"
            if message:
                report["error"] = message
            save()
            QMessageBox.question, QMessageBox.warning, QMessageBox.critical = (
                old_question,
                old_warning,
                old_critical,
            )
            window.close()
            application.exit(code)

        def require(condition, message):
            if not condition:
                raise AssertionError(message)

        def history_check(expected_count):
            require(
                len(window.history_records) == expected_count,
                "Service history row count mismatch",
            )
            names = {r.target_context["database"] for r in window.history_records}
            require(names <= set(config["sources"]), "Unexpected history source")
            if expected_count > 1:
                require(
                    names == set(config["sources"]),
                    "History failed to keep both sources",
                )

        def select_profile(database, *, reader=False):
            from dataclasses import replace

            target = replace(
                base,
                dbname=database,
                user=config["reader"] if reader else config["user"],
            )
            profile = next(
                (
                    p
                    for p in window._connection_profiles
                    if p.database == database and p.user == target.user
                ),
                None,
            )
            if profile is None:
                profile = ConnectionProfile.from_settings(
                    "Read only" if reader else "Second source", target
                )
                window._connection_profiles.append(profile)
            window._fill_profile_combos()
            window.active_database_combo.setCurrentIndex(
                window.active_database_combo.findData(profile.id)
            )
            window.password_edit.setText(reader_secret if reader else secret)

        def start_prediction():
            window.sql_editor.setPlainText(
                "SELECT bucket, SUM(amount) AS total FROM warehouse.orders GROUP BY bucket ORDER BY bucket"
            )
            window.persist_check.setChecked(True)
            window.analyze_button.click()
            require(bool(window._active_workers), "Prediction worker did not start")

        def check_prediction(*, persisted=True):
            require(
                window.last_prediction_ms is not None
                and window.last_prediction_ms >= 0,
                "No prediction",
            )
            require(
                (window.last_query_run_id is not None) == persisted,
                "Unexpected persistence result",
            )
            require(window.analyze_button.isEnabled(), "GUI not unlocked")
            if persisted:
                state["ids"].append(window.last_query_run_id)

        def tick():
            if state["done"] or state["busy"] or window._active_workers:
                return
            state["busy"] = True
            try:
                require(
                    time.monotonic() - state["started"] < 180,
                    "Acceptance exceeded budget",
                )
                require(not dialogs, "Unexpected GUI error/warning dialog")
                step = state["step"]
                state["step"] += 1
                if report["phase"] == "reopen":
                    if step == 0:
                        require(
                            window.database_edit.text() == config["sources"][1],
                            "Active profile not restored",
                        )
                        require(
                            window._database_settings().allowed_schemas
                            == frozenset({"warehouse", "support"}),
                            "Schemas not restored",
                        )
                        require(
                            window.service_database_edit.text() == config["service"],
                            "Service profile not restored",
                        )
                        require(
                            window.password_edit.text()
                            == window.service_password_edit.text()
                            == "",
                            "Password was restored",
                        )
                        require(
                            window.last_query_run_id is None,
                            "Stale source analysis restored",
                        )
                        require(
                            len(window._connection_profiles) == 3,
                            "Connection profiles not restored",
                        )
                        report["cases"].append(
                            "reopened_profiles_schemas_model_without_passwords_or_stale_result"
                        )
                        window.service_password_edit.setText(secret)
                        window._refresh_history()
                    else:
                        history_check(3)
                        report["cases"].append("central_history_after_process_restart")
                        window.tabs.setCurrentIndex(2)
                        application.processEvents()
                        window.grab().save(str(artifacts / "history-after-restart.png"))
                        finish(0)
                    return
                if step == 0:
                    require(
                        not window.preferences.allKeys(),
                        "Clean profile read old settings",
                    )
                    require(
                        not window.last_query_run_id
                        and not window._calibration_path().exists(),
                        "Old analysis/calibration inherited",
                    )
                    window.service_database_edit.setText(config["service"])
                    window.service_password_edit.setText(secret)
                    window.source_ssl_combo.setCurrentIndex(
                        window.source_ssl_combo.findData("verify-full")
                    )
                    window.source_cert_edit.setText(config["certificate"])
                    window.service_ssl_combo.setCurrentIndex(
                        window.service_ssl_combo.findData("verify-full")
                    )
                    window.service_cert_edit.setText(config["certificate"])
                    report["cases"].append("empty_ini_and_no_previous_calibration")
                    window.schema_load_button.click()
                elif step == 1:
                    names = {
                        window.source_schema_list.item(i).data(Qt.ItemDataRole.UserRole)
                        for i in range(window.source_schema_list.count())
                    }
                    require(
                        names == {"public", "warehouse", "support"},
                        "Source schemas not discovered",
                    )
                    window.allowed_schemas_edit.setText("warehouse,support")
                    report["cases"].append(
                        "multiple_source_schemas_without_service_objects"
                    )
                    window.service_create_button.click()
                elif step == 2:
                    require(
                        "создана" in window.service_status.text(),
                        "GUI service DB creation failed",
                    )
                    report["cases"].append("service_database_created_by_gui")
                    window.service_prepare_button.click()
                elif step == 3:
                    require(
                        "подготовлена" in window.service_status.text(),
                        "GUI service migrations failed",
                    )
                    report["cases"].append("service_schema_prepared_by_gui")
                    window._save_settings()
                    start_prediction()
                elif step == 4:
                    check_prediction()
                    report["cases"].append(
                        "fresh_prediction_and_service_history_first_source"
                    )
                    window._refresh_history()
                elif step == 5:
                    history_check(1)
                    select_profile(config["sources"][1])
                    require(
                        window.last_query_run_id is None
                        and window.verified_rewrite_sql is None,
                        "Source switch kept stale results",
                    )
                    start_prediction()
                elif step == 6:
                    check_prediction()
                    require(len(set(state["ids"])) == 2, "History IDs not distinct")
                    window._refresh_history()
                elif step == 7:
                    history_check(2)
                    report["cases"].append(
                        "second_source_switch_and_attributed_central_history"
                    )
                    window.service_database_edit.setText("pqo_clean_missing")
                    start_prediction()
                elif step == 8:
                    check_prediction(persisted=False)
                    require(
                        "сохран" in window.statusBar().currentMessage().lower(),
                        "No service-outage warning",
                    )
                    report["cases"].append(
                        "service_outage_retains_prediction_without_source_fallback"
                    )
                    window.service_database_edit.setText(config["service"])
                    select_profile(config["sources"][1], reader=True)
                    start_prediction()
                elif step == 9:
                    check_prediction()
                    report["cases"].append(
                        "readonly_source_with_separate_writable_service"
                    )
                    window.sql_editor.setPlainText(
                        "SELECT q.bucket,COUNT(*) AS n,SUM(q.amount) AS total FROM "
                        "(SELECT bucket,amount FROM warehouse.orders ORDER BY amount DESC,id) AS q "
                        "GROUP BY q.bucket ORDER BY q.bucket"
                    )
                    window.rewrite_journal_check.setChecked(False)
                    # Force the factual check even for this small fixture. Gain
                    # policy stays 5 ms AND 5%; a timing refusal remains valid.
                    window.threshold_spin.setValue(0)
                    window.rewrite_analyze_button.click()
                elif step == 10:
                    plan = window._rewrite_choice_plan
                    report["rewrite_result"] = {
                        "reason": plan.terminal_reason if plan else None,
                        "equivalent": sum(e.equivalent for e in plan.evaluations)
                        if plan
                        else 0,
                        "recommended": bool(plan and plan.recommended),
                    }
                    require(
                        plan is not None
                        and any(e.equivalent for e in plan.evaluations),
                        "Readonly SQL rewrite did not complete",
                    )
                    window.sql_diff_view.refresh()
                    require(
                        bool(window.sql_diff_view.variant_marks),
                        "SQL line diff missing",
                    )
                    report["cases"].append(
                        "readonly_equivalence_check_and_inline_sql_diff"
                    )
                    window.grab().save(str(artifacts / "rewrite-readonly.png"))
                    select_profile(config["sources"][1])
                    window._save_settings()
                    window.preferences.sync()
                    require(
                        window.preferences.value(PROFILE_KEY)
                        and window.preferences.value(SERVICE_KEY),
                        "Profiles not saved",
                    )
                    raw = Path(window.preferences.fileName()).read_text(
                        encoding="utf-8"
                    )
                    require(
                        secret not in raw and reader_secret not in raw,
                        "Secret in settings",
                    )
                    report["cases"].append("saved_profiles_exclude_both_passwords")
                    finish(0)
            except Exception:
                finish(3, traceback.format_exc())
            finally:
                state["busy"] = False

        timer = QTimer(window)
        timer.timeout.connect(tick)
        timer.start(50)
        return application.exec()
    except Exception:
        report["status"] = "failed"
        report["error"] = traceback.format_exc()
        save()
        if window is not None:
            window.close()
        return 3
