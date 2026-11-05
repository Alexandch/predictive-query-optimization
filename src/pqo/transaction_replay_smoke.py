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
        if (os.getenv('PQO_REPLAY_COPY_CONFIRMED') != '1'
                or os.getenv('PQO_REPLAY_REFERENCE') != 'pqo_university_index_lab'):
            raise ValueError('Disposable replay smoke needs explicit opt-in for the known synthetic reference')
        preview = os.getenv('PQO_SMOKE_PREVIEW') == '1'
        if preview:
            for font in ('arial.ttf','consola.ttf'):
                QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/font))
            application.setFont(QFont('Arial',10))
        window = window_type()
        settings = window._database_settings()
        if settings.dbname != 'pqo_university_unseen' or settings.allowed_schemas != frozenset({'university'}):
            raise ValueError('Disposable replay smoke requires the known synthetic source and university-only schema')
        text = (resources/'examples/transactions/university-disposable-replay.sql').read_text(encoding='utf-8-sig')
        plans = collect_transaction_plans(text,settings)
        indices = collect_transaction_index_candidates(text,settings,plans=plans)
        window.transaction_editor.setPlainText(text)
        window._transaction_inspected((text,plans.inspection))
        window._transaction_planned((text,settings,plans))
        window._transaction_indexes_ready((text,settings,indices))
        row = next(number for number,item in enumerate(indices.candidates)
            if item.status!='covered' and item.action.key_columns==('student_id',) and 'status' in item.action.include_columns)
        window.transaction_index_table.item(row,0).setCheckState(Qt.CheckState.Checked)
        window.transaction_replay_reference.setText('pqo_university_index_lab')
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
