"""Native resource-profile acceptance on the known synthetic insert laboratory."""
from dataclasses import asdict
import json
import os
import traceback

import psycopg
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import QMessageBox

from . import transaction_resources as backend
from .transaction_replay import _kwargs


def run_resources_smoke(application, window_type, resources, artifacts):
    log = artifacts / 'resources-smoke-test.log'
    window = None
    old_question, old_scratch = QMessageBox.question, backend.ScratchDatabases
    state = {'phase': 'inspect', 'runs': [], 'captures': {}, 'done': False}
    try:
        if os.getenv('PQO_WORKFLOW_COPY_CONFIRMED') != '1':
            raise ValueError('Explicit synthetic copy confirmation is required')
        window = window_type()
        settings = window._database_settings()
        if settings.dbname != 'pqo_insert_source' or settings.allowed_schemas != frozenset({'retail'}):
            raise ValueError('Only known pqo_insert_source synthetic fixture is permitted')
        text = (resources / 'examples/transactions/retail-resource-profile.sql').read_text(encoding='utf-8')
        cases = [('original','original',0),('indexes','indexes',64),('rewrite','rewrite',64),
            ('combined','combined',64),('spill','original',64),('rollback','original',64),('cancel','original',64)]
        QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.Yes
        window.resize(1280,900)
        window.show()
        window.tabs.setCurrentIndex(next(i for i in range(window.tabs.count()) if window.tabs.tabText(i)=='Транзакции'))
        window.transaction_workspace_mode.setCurrentIndex(window.transaction_workspace_mode.findData('analysis'))
        window.transaction_editor.setPlainText(text)

        def factory(*args, **kwargs):
            scratch = old_scratch(*args, **kwargs)
            remove = scratch.remove

            def capture_and_remove(name):
                try:
                    if name in scratch.created and name not in scratch.removed:
                        with psycopg.connect(**_kwargs(settings,'postgres')) as c:
                            row = c.execute('SELECT oid,datdba,datallowconn FROM pg_catalog.pg_database WHERE datname=%s',(name,)).fetchone()
                        if row and tuple(row[:2]) == scratch.created[name] and row[2]:
                            with psycopg.connect(**_kwargs(settings,name)) as c:
                                state['captures'][name] = c.execute('SELECT incoming_id,quantity FROM retail.incoming WHERE incoming_id IN(7,17) ORDER BY incoming_id').fetchall()
                except Exception as exc:
                    state['capture_error'] = type(exc).__name__
                return remove(name)
            scratch.remove = capture_and_remove
            return scratch
        backend.ScratchDatabases = factory

        def finish(code, message):
            state['done'] = True
            if window._transaction_control:
                window._transaction_control.cancel()
            if window._active_workers:
                QTimer.singleShot(100,lambda:finish(code,message))
                return
            QMessageBox.question, backend.ScratchDatabases = old_question, old_scratch
            log.write_text(message,encoding='utf-8')
            window.close()
            application.exit(code)

        def progress(message):
            if state.get('case')=='cancel' and 'инструментированный прогон' in message:
                window._cancel_transaction_planning()

        def begin_case():
            name, mode, memory = cases[len(state['runs'])]
            state.update(case=name,phase='resource')
            if name in {'rollback','cancel'}:
                window.transaction_editor.setPlainText(text.replace('COMMIT;','ROLLBACK;') if name=='rollback' else text)
            window.resource_mode.setCurrentIndex(window.resource_mode.findData(mode))
            window.resource_work_mem.setValue(memory)
            window.resource_reference.setText('pqo_insert_index_lab')
            window.resource_confirm.setChecked(True)
            window.transaction_views.setCurrentWidget(window.transaction_resource_page)
            window.resource_run_button.click()
            state['busy_gate'] = not window.analyze_button.isEnabled() and not window.concurrency_run_button.isEnabled() and window.transaction_cancel_button.isEnabled()
            count = len(window._active_workers)
            window._start_resource_profile()
            state['duplicate_blocked'] = count==1 and len(window._active_workers)==count
            for worker in tuple(window._active_workers):
                worker.signals.progress.connect(progress)

        def tick():
            if state['done'] or window._active_workers or window._query_operation_active:
                return
            try:
                phase = state['phase']
                if phase=='inspect':
                    if window.transaction_inspection is None:raise RuntimeError(window.transaction_status.text())
                    window.transaction_plan_button.click();state['phase']='plans'
                elif phase=='plans':
                    if window.transaction_plan_report is None:raise RuntimeError(window.transaction_status.text())
                    window.transaction_index_button.click();state['phase']='indexes'
                elif phase=='indexes':
                    if window.transaction_index_report is None:raise RuntimeError(window.transaction_status.text())
                    row = next(i for i,x in enumerate(window.transaction_index_report.candidates)
                        if x.status!='covered' and x.action.table_name=='incoming' and x.action.key_columns==('warehouse_id',) and 'quantity' in x.action.include_columns)
                    window.transaction_index_table.item(row,0).setCheckState(Qt.CheckState.Checked)
                    state['actions'] = [asdict(a) for a in window._replay_actions()]
                    window._start_transaction_select_rewrites();state['phase']='rewrites'
                elif phase=='rewrites':
                    row = next(i for i,o in enumerate(window._transaction_select_rewrite_options) if o.number==6 and o.rule_id=='or-equality-to-in')
                    window.transaction_select_rewrite_choice.setCurrentIndex(row)
                    state['variant'] = window.transaction_select_rewrite_variant.toPlainText()
                    begin_case()
                elif phase=='resource':
                    report = window.transaction_resource_report
                    if report is None:raise RuntimeError(window.resource_status.text())
                    state['runs'].append({'case':state['case'],'report':asdict(report),'busy_gate':state['busy_gate'],
                        'duplicate_blocked':state['duplicate_blocked'],'idle_restored':window.resource_run_button.isEnabled(),
                        'summary_rows':window.resource_summary.rowCount(),'command_rows':window.resource_commands.rowCount()})
                    application.processEvents()
                    window.grab().save(str(artifacts/('resources-'+state['case']+'.png')))
                    if len(state['runs'])==len(cases):
                        finish(0,'OK\n'+json.dumps({k:state.get(k) for k in ('runs','captures','capture_error','actions','variant')},ensure_ascii=False,indent=2))
                    else:begin_case()
            except Exception:
                finish(3,traceback.format_exc())

        timer = QTimer(window)
        timer.timeout.connect(tick);timer.start(75)
        window.transaction_inspect_button.click()
        QTimer.singleShot(240000,lambda:finish(4,'Timed out; cancellation and cleanup requested') if not state['done'] else None)
        return application.exec()
    except Exception:
        QMessageBox.question, backend.ScratchDatabases = old_question, old_scratch
        log.write_text(traceback.format_exc(),encoding='utf-8')
        if window:window.close()
        return 3
