"""Explicit opt-in installed GUI replay check, restricted to synthetic lab DBs."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import traceback

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QMessageBox

from .transaction_indexes import collect_transaction_index_candidates
from .transaction_planning import collect_transaction_plans


def run_replay_smoke(application, window_type, resources, artifacts):
    log = artifacts / 'transaction-replay-smoke-test.log'
    try:
        defaults = os.getenv('PQO_SMOKE_DEFAULTS_REPLAY')=='1'
        upsert = os.getenv('PQO_SMOKE_UPSERT_REPLAY')=='1'
        insert_select = os.getenv('PQO_SMOKE_INSERT_SELECT_REPLAY')=='1'
        source = 'pqo_insert_source' if insert_select else 'pqo_upsert_source' if upsert else 'pqo_defaults_source' if defaults else 'pqo_university_unseen'
        reference = 'pqo_insert_index_lab' if insert_select else 'pqo_upsert_index_lab' if upsert else 'pqo_defaults_index_lab' if defaults else 'pqo_university_index_lab'
        schema = 'retail' if defaults or upsert or insert_select else 'university'
        if (os.getenv('PQO_REPLAY_COPY_CONFIRMED') != '1'
                or os.getenv('PQO_REPLAY_REFERENCE') != reference):
            raise ValueError('Disposable replay smoke needs explicit opt-in for the known synthetic reference')
        preview = os.getenv('PQO_SMOKE_PREVIEW') == '1'
        if preview:
            for font in ('arial.ttf','consola.ttf'):
                QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/font))
            application.setFont(QFont('Arial',10))
        window = window_type()
        settings = window._database_settings()
        if settings.dbname != source or settings.allowed_schemas != frozenset({schema}):
            raise ValueError('Disposable replay smoke requires the known synthetic source and university-only schema')
        example = 'university-join-replay.sql' if os.getenv('PQO_SMOKE_JOIN_REPLAY') == '1' else 'university-disposable-replay.sql'
        if os.getenv('PQO_SMOKE_ISOLATION_REPLAY')=='1':example='university-isolation-savepoints.sql'
        if defaults:example='retail-defaults-savepoints.sql'
        if upsert:example='retail-upsert-savepoints.sql'
        if insert_select:example='retail-insert-select-savepoints.sql'
        text = (resources/'examples/transactions'/example).read_text(encoding='utf-8-sig')
        plans = collect_transaction_plans(text,settings)
        indices = collect_transaction_index_candidates(text,settings,plans=plans)
        window.transaction_editor.setPlainText(text)
        window._transaction_inspected((text,plans.inspection))
        window._transaction_planned((text,settings,plans))
        window._transaction_indexes_ready((text,settings,indices))
        row = next(number for number,item in enumerate(indices.candidates)
            if item.status!='covered' and item.action.key_columns==(('warehouse_id',) if upsert or insert_select else ('customer_id',) if defaults else ('student_id',))
            and (item.action.table_name=='incoming' and 'quantity' in item.action.include_columns if insert_select else 'status' in item.action.include_columns))
        window.transaction_index_table.item(row,0).setCheckState(Qt.CheckState.Checked)
        window.transaction_replay_reference.setText(reference)
        window.transaction_replay_confirm.setChecked(True)
        window.tabs.setCurrentIndex(1)
        window.transaction_views.setCurrentIndex(8)
        window.resize(1740,1030)
        window.show()
        old_question = QMessageBox.question
        QMessageBox.question = lambda *args,**kwargs: QMessageBox.StandardButton.Yes
        try:
            window._start_transaction_replay()
        finally:
            QMessageBox.question = old_question
        if not window._transaction_replay_active:
            raise RuntimeError('Replay GUI did not start: '+window.transaction_replay_status.text())

        def finished():
            if window._active_workers:
                return
            report = window.transaction_replay_report
            if (report is None or not report.correctness_verified or not report.cleanup_verified
                    or len(report.samples)!=6 or len(report.created_databases)!=7
                    or report.recommendation is None or len(report.recommendation.series)!=2
                    or report.recommendation.request_digest != report.request_digest
                    or window.transaction_replay_effect_table.rowCount()!=len(report.recommendation.summary)
                    or window.transaction_editor.toPlainText()!=text or window.transaction_index_report is not indices):
                log.write_text('FAILED: '+window.transaction_replay_status.text()+'\n'
                    +(json.dumps(asdict(report),ensure_ascii=False,indent=2) if report else ''),encoding='utf-8')
                application.exit(3)
                return
            if os.getenv('PQO_SMOKE_ISOLATION_REPLAY')=='1' and any(
                    sample.transaction_options.isolation_level!='REPEATABLE READ' or sample.transaction_options.read_only
                    or not any(command.kind=='rollback_to' for command in sample.commands) for sample in report.samples):
                log.write_text('FAILED: isolation/savepoint context not retained',encoding='utf-8')
                application.exit(3)
                return
            if (defaults or upsert or insert_select) and any(len(sample.initial_sequences)!=(1 if insert_select else 2) or sample.initial_sequences==sample.final_sequences
                    or not any(command.kind=='rollback_to' for command in sample.commands) for sample in report.samples):
                log.write_text('FAILED: sequence/savepoint evidence not retained',encoding='utf-8')
                application.exit(3)
                return
            log.write_text('OK: real queued GUI replay on six fresh clones; two fixed-policy series, sequential DML/SELECT, same initial state, '
                'matching outputs/final data, independent cleanup, mixed assessment/summary, source editor/candidates unchanged; '
                'an honest timing decline is allowed, no production application.\n'
                +json.dumps(asdict(report),ensure_ascii=False,indent=2),encoding='utf-8')
            if preview:
                application.processEvents()
                window.grab().save(str(artifacts/'transaction-replay-preview.png'))
                window.resize(1280,800)
                application.processEvents()
                window.transaction_replay_page.verticalScrollBar().setValue(window.transaction_replay_page.verticalScrollBar().maximum())
                application.processEvents()
                window.grab().save(str(artifacts/'transaction-replay-preview-small.png'))
            application.exit(0)

        timer = QTimer(window)
        timer.timeout.connect(finished)
        timer.start(100)
        # Never hard-exit a process while a DML worker owns scratch databases.
        QTimer.singleShot(310000,window._cancel_transaction_planning)
        return application.exec()
    except Exception:
        log.write_text(traceback.format_exc(),encoding='utf-8')
        return 3
