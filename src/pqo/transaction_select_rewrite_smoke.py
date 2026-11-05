"""Explicit opt-in queued GUI check; synthetic source/reference only, no source SQL."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import traceback

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont,QFontDatabase
from PyQt6.QtWidgets import QMessageBox


def run_select_rewrite_smoke(application,window_type,resources,artifacts):
    log=artifacts/'transaction-select-rewrite-smoke-test.log'
    try:
        if (os.getenv('PQO_SELECT_REWRITE_COPY_CONFIRMED')!='1'
                or os.getenv('PQO_SELECT_REWRITE_REFERENCE')!='pqo_university_index_lab'):
            raise ValueError('Explicit SELECT rewrite opt-in for the known synthetic reference is required')
        preview=os.getenv('PQO_SMOKE_PREVIEW')=='1'
        if preview:
            for font in ('arial.ttf','consola.ttf'):
                QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/font))
            application.setFont(QFont('Arial',10))
        window=window_type()
        settings=window._database_settings()
        if settings.dbname!='pqo_university_unseen' or settings.allowed_schemas!=frozenset({'university'}):
            raise ValueError('The known synthetic source and university-only schema are required')
        text=(resources/'examples/transactions/university-select-rewrite.sql').read_text(encoding='utf-8-sig')
        window.transaction_editor.setPlainText(text)
        window.tabs.setCurrentIndex(1); window.transaction_views.setCurrentIndex(9)
        window.resize(1740,1030); window.show()
        window._start_transaction_select_rewrites()
        state={'phase':'generate','done':False}

        def finish(code,message):
            state['done']=True
            log.write_text(message,encoding='utf-8')
            application.exit(code)

        def tick():
            if state['done'] or window._active_workers: return
            try:
                if state['phase']=='generate':
                    row=next(n for n,item in enumerate(window._transaction_select_rewrite_options)
                        if item.number==2 and item.rule_id=='subquery-order-without-limit')
                    window.transaction_select_rewrite_choice.setCurrentIndex(row)
                    window.transaction_select_rewrite_reference.setText('pqo_university_index_lab')
                    window.transaction_select_rewrite_confirm.setChecked(True)
                    old_question=QMessageBox.question
                    QMessageBox.question=lambda *args,**kwargs: QMessageBox.StandardButton.Yes
                    try: window._start_transaction_select_rewrite_trial()
                    finally: QMessageBox.question=old_question
                    if window._transaction_select_rewrite_active!='replay':
                        raise RuntimeError('GUI replay did not start: '+window.transaction_select_rewrite_status.text())
                    state['phase']='replay'
                    return
                result=window.transaction_select_rewrite_report
                if result is None: raise RuntimeError('No attached result: '+window.transaction_select_rewrite_status.text())
                report=result.measurement
                if (result.number!=2 or not report.correctness_verified or not report.cleanup_verified or report.actions
                        or len(report.samples)!=6 or len(report.created_databases)!=7
                        or any(s.index_size_bytes or s.index_final_size_bytes or s.index_build_ms for s in report.samples)
                        or report.recommendation is None or len(report.recommendation.series)!=2
                        or window.transaction_editor.toPlainText()!=text):
                    raise RuntimeError('SELECT rewrite evidence/source invariant failed')
                if preview:
                    application.processEvents()
                    window.grab().save(str(artifacts/'transaction-select-rewrite-preview.png'))
                    window.resize(1280,800); application.processEvents()
                    window.transaction_select_rewrite_page.verticalScrollBar().setValue(window.transaction_select_rewrite_page.verticalScrollBar().maximum())
                    application.processEvents()
                    window.grab().save(str(artifacts/'transaction-select-rewrite-preview-small.png'))
                finish(0,'OK: local queued preparation, side-by-side replacement of SELECT 2 after UPDATE, '
                    'full sequential A1/B/A2 on six fresh clones, matching every output/final state, '
                    'seven databases removed, no new indexes/source editor changes; timing refusal allowed.\n'
                    +json.dumps(asdict(result),ensure_ascii=False,indent=2))
            except Exception:
                finish(3,traceback.format_exc())
        timer=QTimer(window); timer.timeout.connect(tick); timer.start(100)
        # Do not hard-exit while a DML worker owns disposable databases.
        QTimer.singleShot(310000,window._cancel_transaction_planning)
        return application.exec()
    except Exception:
        log.write_text(traceback.format_exc(),encoding='utf-8')
        return 3
