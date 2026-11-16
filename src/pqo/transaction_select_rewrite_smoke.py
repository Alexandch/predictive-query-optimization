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
        defaults=os.getenv('PQO_SMOKE_DEFAULTS_REPLAY')=='1'
        upsert=os.getenv('PQO_SMOKE_UPSERT_REPLAY')=='1'
        insert_select=os.getenv('PQO_SMOKE_INSERT_SELECT_REPLAY')=='1'
        source='pqo_insert_source' if insert_select else 'pqo_upsert_source' if upsert else 'pqo_defaults_source' if defaults else 'pqo_university_unseen'
        reference='pqo_insert_index_lab' if insert_select else 'pqo_upsert_index_lab' if upsert else 'pqo_defaults_index_lab' if defaults else 'pqo_university_index_lab'
        schema='retail' if defaults or upsert or insert_select else 'university'
        if (os.getenv('PQO_SELECT_REWRITE_COPY_CONFIRMED')!='1'
                or os.getenv('PQO_SELECT_REWRITE_REFERENCE')!=reference):
            raise ValueError('Explicit SELECT rewrite opt-in for the known synthetic reference is required')
        preview=os.getenv('PQO_SMOKE_PREVIEW')=='1'
        if preview:
            for font in ('arial.ttf','consola.ttf'):
                QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/font))
            application.setFont(QFont('Arial',10))
        window=window_type()
        settings=window._database_settings()
        if settings.dbname!=source or settings.allowed_schemas!=frozenset({schema}):
            raise ValueError('The known synthetic source and university-only schema are required')
        joined = os.getenv('PQO_SMOKE_JOIN_REPLAY') == '1'
        isolation = os.getenv('PQO_SMOKE_ISOLATION_REPLAY') == '1'
        example = 'university-join-replay.sql' if joined else 'university-select-rewrite.sql'
        if isolation:example='university-isolation-savepoints.sql'
        if defaults:example='retail-defaults-savepoints.sql'
        if upsert:example='retail-upsert-savepoints.sql'
        if insert_select:example='retail-insert-select-savepoints.sql'
        number = 7 if insert_select else 10 if upsert else 9 if defaults else 7 if isolation else 2
        rule = 'or-equality-to-in' if joined or isolation or defaults or upsert or insert_select else 'subquery-order-without-limit'
        text=(resources/'examples/transactions'/example).read_text(encoding='utf-8-sig')
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
                        if item.number==number and item.rule_id==rule)
                    window.transaction_select_rewrite_choice.setCurrentIndex(row)
                    window.transaction_workspace_mode.setCurrentIndex(window.transaction_workspace_mode.findData('analysis'))
                    window.transaction_select_rewrite_expand.setChecked(True)
                    application.processEvents()
                    # Windows can clamp the requested 1030 logical pixels to a
                    # smaller work area at 200% scaling. Check usable area,
                    # rather than a fixed height that exceeds that layout.
                    minimum_editor_height=max(150,int(window.height()*.4))
                    if (window.transaction_sections.count()!=10
                            or window.transaction_select_rewrite_settings.isVisible()
                            or window.transaction_select_rewrite_variant.viewport().height()<minimum_editor_height
                            or not window.transaction_cancel_button.isVisible()
                            or window.transaction_editor.toPlainText()!=text):
                        raise RuntimeError('Expanded transaction workspace geometry/state check failed: '+json.dumps({
                            'sections':window.transaction_sections.count(),
                            'settings_visible':window.transaction_select_rewrite_settings.isVisible(),
                            'editor_height':window.transaction_select_rewrite_variant.viewport().height(),
                            'minimum_editor_height':minimum_editor_height,
                            'cancel_visible':window.transaction_cancel_button.isVisible(),
                            'source_unchanged':window.transaction_editor.toPlainText()==text,
                            'window_height':window.height(),'device_pixel_ratio':window.devicePixelRatioF()}))
                    window.transaction_select_rewrite_expand.setChecked(False)
                    window.transaction_select_rewrite_reference.setText(reference)
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
                if (result.number!=number or not report.correctness_verified or not report.cleanup_verified or report.actions
                        or len(report.samples)!=6 or len(report.created_databases)!=7
                        or any(s.index_size_bytes or s.index_final_size_bytes or s.index_build_ms for s in report.samples)
                        or report.recommendation is None or len(report.recommendation.series)!=2
                        or window.transaction_editor.toPlainText()!=text):
                    raise RuntimeError('SELECT rewrite evidence/source invariant failed')
                if isolation and any(sample.transaction_options.isolation_level!='REPEATABLE READ'
                        or sample.transaction_options.read_only or not any(c.kind=='rollback_to' for c in sample.commands)
                        for sample in report.samples):
                    raise RuntimeError('Isolation/savepoint context not retained')
                if (defaults or upsert or insert_select) and any(len(sample.initial_sequences)!=(1 if insert_select else 2) or sample.initial_sequences==sample.final_sequences
                        for sample in report.samples):
                    raise RuntimeError('Sequence evidence not retained')
                if preview:
                    application.processEvents()
                    window.grab().save(str(artifacts/'transaction-select-rewrite-preview.png'))
                    window.resize(1280,800); application.processEvents()
                    window.transaction_select_rewrite_page.verticalScrollBar().setValue(window.transaction_select_rewrite_page.verticalScrollBar().maximum())
                    application.processEvents()
                    window.grab().save(str(artifacts/'transaction-select-rewrite-preview-small.png'))
                finish(0,f'OK: local queued preparation, side-by-side replacement of SELECT {number} after UPDATE, '
                    'expanded workspace geometry and visible cancellation, '
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
