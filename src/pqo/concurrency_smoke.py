"""Explicit native acceptance on the known synthetic pqo_insert laboratory."""
from dataclasses import asdict
import json
import os
import traceback

import psycopg
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMessageBox

from . import transaction_concurrency as backend
from .transaction_replay import _kwargs


def run_concurrency_smoke(application, window_type, resources, artifacts):
    log = artifacts / 'concurrency-smoke-test.log'
    window = None
    original_question = QMessageBox.question
    original_scratch = backend.ScratchDatabases
    state = {'phase': 'start', 'runs': [], 'done': False, 'final_rows': None}
    try:
        if os.getenv('PQO_WORKFLOW_COPY_CONFIRMED') != '1':
            raise ValueError('Explicit synthetic copy confirmation is required')
        window = window_type()
        settings = window._database_settings()
        if settings.dbname != 'pqo_insert_source' or settings.allowed_schemas != frozenset({'retail'}):
            raise ValueError('Only the known pqo_insert_source synthetic laboratory is permitted')
        reference = 'pqo_insert_index_lab'
        QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.Yes
        cases = [('lock', 'lock'), ('deadlock', 'deadlock'), ('snapshot', 'snapshot'),
            ('snapshot_rc', 'snapshot'), ('serialization', 'serialization'), ('update_rr', 'lock'), ('cancel', 'lock')]
        window.resize(1280, 900)
        window.show()
        window.tabs.setCurrentIndex(next(i for i in range(window.tabs.count()) if window.tabs.tabText(i) == 'Транзакции'))
        window.transaction_workspace_mode.setCurrentIndex(window.transaction_workspace_mode.findData('analysis'))
        window.transaction_views.setCurrentWidget(window.transaction_concurrency_page)

        def scratch_factory(*args, **kwargs):
            scratch = original_scratch(*args, **kwargs)
            finish = scratch.finish

            def capture_and_finish():
                try:
                    if len(scratch.created) == 2:
                        name = list(scratch.created)[-1]
                        with psycopg.connect(**_kwargs(settings, 'postgres')) as c:
                            identity = c.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s', (name,)).fetchone()
                        if identity != scratch.created[name]:
                            raise RuntimeError('Owned clone identity changed')
                        with psycopg.connect(**_kwargs(settings, name)) as c:
                            state['final_rows'] = c.execute('SELECT incoming_id,quantity FROM retail.incoming WHERE incoming_id IN(1,2) ORDER BY incoming_id').fetchall()
                except Exception as exc:
                    state['capture_error'] = type(exc).__name__
                finally:
                    cleanup = finish()
                return cleanup
            scratch.finish = capture_and_finish
            return scratch
        backend.ScratchDatabases = scratch_factory

        def finish(code, message):
            state['done'] = True
            if window._transaction_control:
                window._transaction_control.cancel()
            if window._active_workers:
                QTimer.singleShot(100, lambda: finish(code, message))
                return
            backend.ScratchDatabases = original_scratch
            QMessageBox.question = original_question
            log.write_text(message, encoding='utf-8')
            window.close()
            application.exit(code)

        def progress(message):
            if state.get('case') == 'cancel' and 'B LOCK' in message:
                window._cancel_transaction_planning()

        def begin():
            name, template = cases[len(state['runs'])]
            state.update(case=name, phase='running', final_rows=None)
            state.pop('capture_error', None)
            window.concurrency_view.setCurrentIndex(window.concurrency_view.findData('split'))
            window.concurrency_example.setCurrentIndex(window.concurrency_example.findData(template))
            window.concurrency_example_button.click()
            for editor in (window.concurrency_a, window.concurrency_b):
                text = editor.toPlainText().replace('university.students', 'retail.incoming').replace('student_id', 'incoming_id').replace('campus_id', 'quantity')
                if name == 'snapshot_rc':
                    text = text.replace('REPEATABLE READ', 'READ COMMITTED')
                if name == 'update_rr':
                    text = text.replace('READ COMMITTED', 'REPEATABLE READ')
                editor.setPlainText(text)
            window.concurrency_reference.setText(reference)
            if name == 'cancel':
                window.concurrency_schedule.setPlainText(window.concurrency_schedule.toPlainText().replace('A END\nWAIT B', 'WAIT B\nA END'))
            window.concurrency_confirm.setChecked(True)
            window.concurrency_run_button.click()
            state['busy_gate'] = (not window.analyze_button.isEnabled() and not window.deep_analyze_button.isEnabled()
                and not window.concurrency_run_button.isEnabled() and window.transaction_cancel_button.isEnabled())
            count = len(window._active_workers)
            window._start_concurrency()
            state['duplicate_start_blocked'] = len(window._active_workers) == count and count == 1
            for worker in tuple(window._active_workers):
                worker.signals.progress.connect(progress)

        def tick():
            if state['done'] or window._active_workers or window._query_operation_active or state['phase'] != 'running':
                return
            try:
                report = window.transaction_concurrency_report
                if report is None:
                    raise RuntimeError('No report: ' + window.concurrency_status.text())
                state['runs'].append({'case': state['case'], 'report': asdict(report), 'final_rows': state['final_rows'],
                    'capture_error': state.get('capture_error'), 'busy_gate': state['busy_gate'],
                    'duplicate_start_blocked': state['duplicate_start_blocked'],
                    'idle_restored': window.analyze_button.isEnabled() and window.concurrency_run_button.isEnabled(),
                    'commands_displayed': window.concurrency_commands.rowCount(), 'locks_displayed': window.concurrency_locks.rowCount()})
                window.concurrency_view.setCurrentIndex(window.concurrency_view.findData('result'))
                if state['case'] in {'snapshot', 'serialization', 'cancel'}:
                    window.concurrency_results.setCurrentWidget(window.concurrency_details)
                application.processEvents()
                window.grab().save(str(artifacts / ('concurrency-' + state['case'] + '.png')))
                if len(state['runs']) == len(cases):
                    finish(0, 'OK\n' + json.dumps({'runs': state['runs']}, ensure_ascii=False, indent=2))
                else:
                    safe_begin()
            except Exception:
                finish(3, traceback.format_exc())

        def safe_begin():
            try:
                begin()
            except Exception:
                finish(3, traceback.format_exc())

        timer = QTimer(window)
        timer.timeout.connect(tick)
        timer.start(75)
        QTimer.singleShot(50, safe_begin)
        QTimer.singleShot(180000, lambda: finish(4, 'Timed out; cancellation and worker cleanup requested') if not state['done'] else None)
        return application.exec()
    except Exception:
        backend.ScratchDatabases = original_scratch
        QMessageBox.question = original_question
        log.write_text(traceback.format_exc(), encoding='utf-8')
        if window:
            window.close()
        return 3
