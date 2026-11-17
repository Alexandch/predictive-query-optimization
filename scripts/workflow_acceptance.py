"""Native Qt workflow acceptance on newly created, owned PostgreSQL fixtures."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg
from psycopg import sql

from pqo.analysis_control import AnalysisControl
from pqo.config import DatabaseSettings
from pqo.transaction_acceptance import AcceptanceFixture, DOMAINS
from pqo.transaction_inspection import inspect_transaction
from pqo.transaction_replay import result_digest, state_digest
from pqo.transaction_replay_guard import prepare_replay, replay_catalog


def save(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def inventory(settings):
    with psycopg.connect(**settings.connection_kwargs(), autocommit=True) as c:
        tables = replay_catalog(
            c,
            settings,
            prepare_replay(
                DOMAINS_BY_SCHEMA[next(iter(settings.allowed_schemas))].index_sql(),
                settings,
            ),
        )
        states = state_digest(
            c, tables, settings, time.monotonic() + 30, AnalysisControl()
        )
        indexes = c.execute(
            "SELECT schemaname,indexname,indexdef FROM pg_catalog.pg_indexes WHERE schemaname=ANY(%s) ORDER BY 1,2",
            (sorted(settings.allowed_schemas),),
        ).fetchall()
    return {"state": [asdict(x) for x in states], "indexes": indexes}


DOMAINS_BY_SCHEMA = {domain.schema: domain for domain in DOMAINS}


def expected_final(settings, domain):
    """Independent expected state: exactly key=1 changes from open to reviewed."""
    answer = {}
    with psycopg.connect(**settings.connection_kwargs(), autocommit=True) as c:
        tables = replay_catalog(
            c, settings, prepare_replay(domain.index_sql(), settings)
        )
        for table in tables:
            digest = hashlib.sha256()
            assert not table.sequences
            digest.update(
                json.dumps(
                    (table.columns, table.primary_key, table.default_signatures, ())
                ).encode()
            )
            statement = sql.SQL(
                "SELECT pg_catalog.row_to_json(t)::text FROM {}.{} t ORDER BY {}"
            ).format(
                sql.Identifier(table.schema),
                sql.Identifier(table.name),
                sql.SQL(",").join(sql.Identifier(k) for k in table.primary_key),
            )
            for (raw,) in c.execute(statement):
                if table.name == domain.table and json.loads(raw)[domain.key] == 1:
                    assert '"status":"open"' in raw
                    raw = raw.replace('"status":"open"', '"status":"reviewed"', 1)
                data = raw.encode()
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
            answer[f"{table.schema}.{table.name}"] = digest.hexdigest()
    return answer


def expected_results(domain, kind):
    first = result_digest([(1, "reviewed")], False)
    if kind == "index":
        answer = {1: first}
        for number, value in enumerate((17, 98, 245, 356, 419, 521, 647, 793), 2):
            amounts = [i % 5000 for i in range(value, domain.rows + 1, 1000)]
            answer[number] = result_digest(
                [(len(amounts), Decimal(sum(amounts)))], False
            )
        return answer
    total = sum(i % 5000 for i in range(1, domain.rows + 1))
    return {
        1: first,
        2: result_digest(
            [
                ("open", domain.rows - 1, Decimal(total - 1)),
                ("reviewed", 1, Decimal(1)),
            ],
            True,
        ),
        3: result_digest([(1, "reviewed")], True),
    }


def gui_worker(output, schema, manifest):
    # Environment/profile are isolated by the parent before importing the GUI.
    from PyQt6.QtCore import Qt, QObject, pyqtSignal
    from PyQt6.QtGui import QFont, QFontDatabase
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from pqo.desktop import MainWindow

    source = DatabaseSettings.from_env()
    owned = json.loads(manifest.read_text(encoding="utf-8"))
    assert source.dbname == owned["database"] and source.dbname.startswith(
        "pqo_mvpfixture_"
    )
    assert (source.host, source.port) == (owned["host"], owned["port"])
    with psycopg.connect(**replace(source, dbname="postgres").connection_kwargs()) as c:
        assert c.execute(
            "SELECT oid,datdba FROM pg_database WHERE datname=%s", (source.dbname,)
        ).fetchone() == tuple(owned["identity"])
    d = DOMAINS_BY_SCHEMA[schema]
    expected_state = expected_final(source, d)
    application = QApplication([])
    for font in ("arial.ttf", "arialbd.ttf", "consola.ttf"):
        QFontDatabase.addApplicationFont(
            str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font)
        )
    application.setFont(QFont("Arial", 10))
    w = MainWindow()
    w.resize(1280, 850)
    w.show()
    application.processEvents()
    w.persist_check.setChecked(False)
    w.rewrite_journal_check.setChecked(False)
    w.threshold_spin.setValue(2)
    w.minimum_gain_spin.setValue(1)
    w.minimum_gain_percent_spin.setValue(5)
    service, model = w._service_database_settings(), w.xgb_model_edit.text()
    errors, checks = [], []
    QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.Yes

    def dialog(*args, **kwargs):
        errors.append(str(args[1:3]))
        return QMessageBox.StandardButton.Ok

    QMessageBox.critical = dialog
    QMessageBox.warning = dialog

    def pump(label):
        print(schema, label, flush=True)
        deadline = time.monotonic() + 360
        cancelled = False
        while w._active_workers or w._query_operation_active:
            application.processEvents()
            time.sleep(0.02)
            if time.monotonic() > deadline and not cancelled:
                cancelled = True
                for control in (
                    w._workflow_copy_control,
                    w._transaction_control,
                    getattr(w, "_deep_control", None),
                ):
                    if control:
                        control.cancel()
            if cancelled and not w._active_workers:
                break
        application.processEvents()
        assert not cancelled, label + " exceeded its budget; cancellation completed"
        assert not errors, errors

    def click(button, label):
        assert button.isEnabled(), (
            label,
            w.transaction_status.text(),
            w.variant_status.text(),
        )
        button.click()
        pump(label)

    def snapshot(name):
        application.processEvents()
        w.grab().save(str(output / (name + ".png")))

    def record(case, report, kind, *, mismatch=False, cancelled=False):
        assert report is not None, (case, w.transaction_status.text())
        save(output / (case + ".json"), asdict(report))
        assert report.cleanup_verified and not report.leftover_databases
        if cancelled:
            assert (
                report.status == "cancelled" and not report.recommendation.recommended
            )
        else:
            assert report.status == "completed" and len(report.samples) == 6
            assert report.initial_states_equal and report.final_states_equal
            assert report.correctness_verified != mismatch
            assert not mismatch or not report.recommendation.recommended
            if mismatch:
                assert "отклонена" in w.transaction_select_rewrite_status.text()
                assert (
                    w.statusBar().currentMessage()
                    == w.transaction_select_rewrite_status.text()
                )
            expected = expected_results(d, kind)
            for sample in report.samples:
                assert {x.table: x.sha256 for x in sample.final_state} == expected_state
                for number, digest in expected.items():
                    actual = sample.commands[number - 1].result_sha256
                    assert (
                        (actual != digest)
                        if mismatch and sample.phase == "B" and number == 2
                        else (actual == digest)
                    ), (case, sample.phase, number)
        with psycopg.connect(
            **replace(source, dbname="postgres").connection_kwargs()
        ) as c:
            assert not c.execute(
                "SELECT datname FROM pg_database WHERE datname=ANY(%s)",
                (list(report.created_databases),),
            ).fetchall()
        checks.append(
            {
                "case": case,
                "status": report.status,
                "decision": report.recommendation.status,
                "reasons": list(report.recommendation.reasons),
                "metrics": [asdict(x) for x in report.recommendation.summary],
            }
        )
        save(output / "checks.json", checks)

    def prepare_index(text):
        w._workflow_open_transaction()
        w.transaction_editor.setPlainText(text)
        click(w.transaction_inspect_button, "inspect transaction")
        click(w.transaction_plan_button, "estimated plans")
        click(w.transaction_index_button, "shared index candidates")
        candidates = w.transaction_index_report.candidates
        row = next(
            i
            for i, item in enumerate(candidates)
            if item.status != "covered"
            and item.action.key_columns == (d.filter_key,)
            and d.value in item.action.include_columns
            and "status" not in item.action.include_columns
        )
        w.transaction_index_table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        assert w._replay_actions() == (candidates[row].action,)
        w.transaction_views.setCurrentIndex(8)
        w.transaction_replay_confirm.setChecked(True)

    try:
        click(w.workflow_connect_button, "connection and schemas")
        w.workflow_copy_name.setText("pqo_uiflow_" + uuid4().hex[:12])
        click(w.workflow_copy_button, "create laboratory through home")
        stand = w._workflow_lab()["stand"]
        save(output / "stand.json", stand)
        working = w._database_settings()
        reference = replace(working, dbname=stand["reference_database"])
        before = inventory(source)
        assert inventory(working) == inventory(reference) == before
        snapshot("laboratory")
        click(w.workflow_query_button, "open individual SQL")
        one = f"SELECT COUNT(*) AS n,SUM({d.value}) AS total FROM {d.relation} WHERE {d.filter_key}=17"
        w.sql_editor.setPlainText(one)
        click(w.analyze_button, "fast ML and preliminary recommendations")
        assert w.last_prediction_ms is not None
        save(
            output / "prediction.json",
            {"predicted_ms": w.last_prediction_ms, "source": w.last_prediction_source},
        )
        click(w.deep_analyze_button, "individual SELECT index test")
        assert w.last_sequential_plan is not None, w.recommendation_text.toPlainText()
        save(output / "individual-index.json", asdict(w.last_sequential_plan))
        assert inventory(working) == before
        snapshot("individual-index")
        index_text = d.index_sql()
        (output / "index-transaction.sql").write_text(index_text, encoding="utf-8")
        prepare_index(index_text)
        click(w.transaction_replay_button, "whole transaction with index")
        record("transaction-index", w.transaction_replay_report, "index")
        assert w.transaction_editor.toPlainText() == index_text
        snapshot("whole-index-result")

        rewrite_text = d.rewrite_sql()
        (output / "rewrite-transaction.sql").write_text(rewrite_text, encoding="utf-8")
        w.transaction_editor.setPlainText(rewrite_text)
        click(w.transaction_inspect_button, "inspect rewrite transaction")
        w.transaction_table.setCurrentCell(1, 0)
        click(w.workflow_send_select_button, "send SELECT to individual analysis")
        click(
            w.rewrite_analyze_button, "automatic SELECT rewriting and real comparison"
        )
        plan = w._rewrite_choice_plan
        assert plan is not None and any(x.equivalent for x in plan.evaluations), (
            w.recommendation_text.toPlainText()
        )
        chosen = next(
            i
            for i, x in enumerate(plan.evaluations)
            if x.candidate.rule_id == "subquery-order-without-limit"
        )
        w.rewrite_candidate_combo.setCurrentIndex(chosen)
        variant = w.rewrite_editor.toPlainText()
        assert variant.strip()
        save(output / "individual-rewrite.json", asdict(plan))
        snapshot("individual-rewrite")
        click(w.workflow_return_select_button, "return variant to transaction")
        w.transaction_select_rewrite_confirm.setChecked(True)
        click(
            w.transaction_select_rewrite_button,
            "whole transaction with SELECT replacement",
        )
        record(
            "transaction-rewrite",
            w.transaction_select_rewrite_report.measurement,
            "rewrite",
        )
        assert w.transaction_editor.toPlainText() == rewrite_text
        snapshot("whole-rewrite-result")

        if schema == "retail":
            wrong = variant.replace(
                "GROUP BY", "WHERE q.status <> 'reviewed' GROUP BY", 1
            )
            assert wrong != variant
            w._workflow_open_tab(0)
            w.rewrite_editor.setPlainText(wrong)
            click(
                w.compare_variant_button,
                "wrong-in-context SELECT on initial working data",
            )
            assert any(x.equivalent for x in w._rewrite_choice_plan.evaluations)
            click(
                w.workflow_return_select_button, "return wrong-in-context replacement"
            )
            w.transaction_select_rewrite_confirm.setChecked(True)
            click(
                w.transaction_select_rewrite_button,
                "must reject replacement after prior UPDATE",
            )
            record(
                "wrong-after-update",
                w.transaction_select_rewrite_report.measurement,
                "rewrite",
                mismatch=True,
            )
            snapshot("wrong-after-update")
            prepare_index(index_text)

            class CancellationRequest(QObject):
                requested = pyqtSignal()

            cancellation = CancellationRequest()
            cancellation.requested.connect(w.transaction_cancel_button.click)
            w.transaction_replay_button.click()
            assert w._transaction_control is not None
            w._transaction_control.progress = lambda message: (
                cancellation.requested.emit()
                if message.startswith("Последовательное выполнение: 2")
                else None
            )
            pump("cancel after the first write")
            record(
                "cancel-after-write",
                w.transaction_replay_report,
                "index",
                cancelled=True,
            )
        assert inventory(source) == inventory(working) == inventory(reference) == before
        assert (
            w._service_database_settings() == service
            and w.xgb_model_edit.text() == model
        )
        save(
            output / "worker-result.json",
            {"status": "passed", "checks": checks, "stand_unchanged": True},
        )
        return 0
    except BaseException as exc:
        save(
            output / "worker-error.json",
            {"error_type": type(exc).__name__, "message": str(exc), "dialogs": errors},
        )
        raise
    finally:
        w.close()
        application.processEvents()


def cleanup_stand(settings, stand):
    if stand is None:
        return
    with psycopg.connect(
        **replace(settings, dbname="postgres").connection_kwargs(), autocommit=True
    ) as c:
        for name, oid, owner in reversed(stand["identities"]):
            assert name in {
                stand["working_database"],
                stand["reference_database"],
            } and name.startswith("pqo_uiflow_")
            actual = c.execute(
                "SELECT oid,datdba FROM pg_database WHERE datname=%s", (name,)
            ).fetchone()
            if actual is not None:
                assert actual == (oid, owner), (
                    "Identity changed; manual inspection required"
                )
                c.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
    with psycopg.connect(
        **replace(settings, dbname="postgres").connection_kwargs()
    ) as check:
        assert not check.execute(
            "SELECT datname FROM pg_database WHERE datname=ANY(%s)",
            ([row[0] for row in stand["identities"]],),
        ).fetchall()


def main():
    if not __debug__:
        raise RuntimeError("Acceptance checks require Python without -O")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-local-laboratory", action="store_true")
    parser.add_argument(
        "--domain",
        choices=tuple(DOMAINS_BY_SCHEMA),
        action="append",
        help="Limit a repeat to selected domains; default: all three",
    )
    parser.add_argument(
        "--worker", choices=tuple(DOMAINS_BY_SCHEMA), help=argparse.SUPPRESS
    )
    parser.add_argument("--manifest", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if not args.confirm_local_laboratory or not args.manifest:
            parser.error("Worker needs owned fixture confirmation")
        return gui_worker(args.output.resolve(), args.worker, args.manifest)
    if not args.confirm_local_laboratory:
        parser.error("Explicit local laboratory confirmation required")
    settings = DatabaseSettings.from_env()
    if settings.host not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Only a local trusted test server is permitted")
    from scripts.clean_profile_acceptance import user_state

    before_user = user_state()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    result = {"status": "running", "mode": "native source GUI", "domains": []}
    save(output / "report.json", result)
    try:
        for d in DOMAINS:
            if args.domain and d.schema not in args.domain:
                continue
            folder = output / d.schema
            folder.mkdir()
            source = replace(settings, allowed_schemas=frozenset({d.schema}))
            fixture = AcceptanceFixture(source, d, folder)
            stand = None
            try:
                fixture.create()
                initial = fixture.fingerprint()
                env = os.environ.copy()
                env.update(
                    QT_QPA_PLATFORM="windows",
                    POSTGRES_DB=fixture.name,
                    PQO_ALLOWED_SCHEMAS=d.schema,
                    PQO_SETTINGS_FILE=str(folder / "settings.ini"),
                    PQO_APP_DATA_ROOT=str(folder / "app-data"),
                )
                with (folder / "worker.log").open("w", encoding="utf-8") as log:
                    process = subprocess.run(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--worker",
                            d.schema,
                            "--output",
                            str(folder),
                            "--manifest",
                            str(fixture.manifest),
                            "--confirm-local-laboratory",
                        ],
                        cwd=ROOT,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                if (folder / "stand.json").exists():
                    stand = json.loads(
                        (folder / "stand.json").read_text(encoding="utf-8")
                    )
                assert process.returncode == 0, (
                    f"{d.schema}: inspect {folder / 'worker.log'}"
                )
                assert fixture.fingerprint() == initial, "Source fixture changed"
                result["domains"].append(
                    json.loads(
                        (folder / "worker-result.json").read_text(encoding="utf-8")
                    )
                )
            finally:
                cleanup_stand(source, stand)
                assert fixture.remove(), "Own fixture cleanup not verified"
                save(output / "report.json", result)
        assert user_state() == before_user, "User settings/model changed"
        result["status"] = "passed"
    except BaseException:
        result["status"] = "error"
        raise
    finally:
        result["user_settings_models_unchanged"] = user_state() == before_user
        save(output / "report.json", result)
    print("Native workflow acceptance:", result["status"], output / "report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
