"""Two-session laboratory workspace; independent SQL and explicit scheduling."""
from dataclasses import asdict
import json
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .analysis_control import AnalysisControl
from .app_paths import resource_root
from .transaction_concurrency import concurrency_request_digest, run_concurrent_transactions
from .transaction_replay import validate_reference


class TransactionConcurrencyMixin:
    def _build_concurrency_controls(self):
        self.transaction_concurrency_report = None
        self.transaction_concurrency_page = page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel('Две сессии на общей одноразовой копии. Задайте SQL и порядок шагов; результаты показывают блокировки и конфликты.')
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.concurrency_example = QComboBox()
        for title, key in (('Ожидание записи', 'lock'), ('Взаимоблокировка', 'deadlock'),
                ('Повторное чтение', 'snapshot'), ('Конфликт SERIALIZABLE', 'serialization')):
            self.concurrency_example.addItem(title, key)
        self.concurrency_example_button = QPushButton('Пример university')
        self.concurrency_import_button = QPushButton('Текущий SQL → A')
        self.concurrency_view = QComboBox()
        for title, value in (('SQL + результат', 'split'), ('Только SQL', 'sql'), ('Результат', 'result')):
            self.concurrency_view.addItem(title, value)
        row.addWidget(self.concurrency_example)
        row.addWidget(self.concurrency_example_button)
        row.addWidget(self.concurrency_import_button)
        row.addWidget(self.concurrency_view)
        row.addStretch()
        layout.addLayout(row)
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        editors = QSplitter(Qt.Orientation.Horizontal)
        self.concurrency_a, self.concurrency_b = QPlainTextEdit(), QPlainTextEdit()
        self.concurrency_load_buttons = []
        for label, editor in (('Сессия A', self.concurrency_a), ('Сессия B', self.concurrency_b)):
            box = QGroupBox(label)
            body = QVBoxLayout(box)
            editor.setFont(QFont('Consolas', 10))
            editor.setMinimumHeight(120)
            editor.setPlaceholderText('BEGIN …; SELECT …; UPDATE …; COMMIT;')
            body.addWidget(editor)
            button = QPushButton('Открыть SQL-файл')
            button.clicked.connect(lambda _=False, target=editor: self._load_concurrency_sql(target))
            self.concurrency_load_buttons.append(button)
            body.addWidget(button)
            editors.addWidget(box)
        self.concurrency_editors = editors
        split.addWidget(editors)
        lower = QSplitter(Qt.Orientation.Horizontal)
        self.concurrency_lower = lower
        box = QGroupBox('Расписание')
        body = QVBoxLayout(box)
        help_text = QLabel('A 1 — выполнить команду №1\nB 2 & — запустить в фоне\nWAIT B LOCK — дождаться блокировки\nWAIT B — дождаться результата\nA END — COMMIT/ROLLBACK из SQL')
        help_text.setWordWrap(True)
        body.addWidget(help_text)
        self.concurrency_schedule = QPlainTextEdit()
        self.concurrency_schedule.setFont(QFont('Consolas', 10))
        self.concurrency_schedule.setMinimumHeight(100)
        body.addWidget(self.concurrency_schedule, 1)
        lower.addWidget(box)
        self.concurrency_results = QTabWidget()
        self.concurrency_commands = QTableWidget(0, 7)
        self.concurrency_commands.setHorizontalHeaderLabels(['Сессия / шаг', 'Команда', 'Результат', 'мс', 'Строк', 'SQLSTATE', 'Хеш результата'])
        self.concurrency_locks = QTableWidget(0, 5)
        self.concurrency_locks.setHorizontalHeaderLabels(['Сессия / шаг', 'Ожидание', 'Блокирует', 'Наблюдений', 'Интервал, мс'])
        for table in (self.concurrency_commands, self.concurrency_locks):
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.verticalHeader().hide()
            table.horizontalHeader().setStretchLastSection(True)
        self.concurrency_details = QPlainTextEdit()
        self.concurrency_details.setReadOnly(True)
        self.concurrency_results.addTab(self.concurrency_commands, 'Команды')
        self.concurrency_results.addTab(self.concurrency_locks, 'Блокировки')
        self.concurrency_results.addTab(self.concurrency_details, 'Итог и данные')
        lower.addWidget(self.concurrency_results)
        lower.setStretchFactor(1, 2)
        split.addWidget(lower)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setHandleWidth(8)
        lower.setHandleWidth(8)
        editors.setHandleWidth(8)
        self.concurrency_splitter = split
        self.concurrency_scroll = QScrollArea()
        self.concurrency_scroll.setWidgetResizable(True)
        self.concurrency_scroll.setMinimumSize(0, 0)
        self.concurrency_scroll.setWidget(split)
        layout.addWidget(self.concurrency_scroll, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel('Эталон:'))
        self.concurrency_reference = QLineEdit()
        self.concurrency_reference.setPlaceholderText('Отдельный лабораторный эталон')
        row.addWidget(self.concurrency_reference, 1)
        row.addWidget(QLabel('Лимит, мс:'))
        self.concurrency_timeout = QSpinBox()
        self.concurrency_timeout.setRange(500, 10000)
        self.concurrency_timeout.setValue(5000)
        row.addWidget(self.concurrency_timeout)
        self.concurrency_confirm = QCheckBox('Разрешаю одноразовые копии')
        row.addWidget(self.concurrency_confirm)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.concurrency_run_button = QPushButton('Проверить две сессии на копии')
        self.concurrency_run_button.setObjectName('primaryButton')
        self.concurrency_save_button = QPushButton('Сохранить результат JSON')
        row.addWidget(self.concurrency_run_button, 1)
        row.addWidget(self.concurrency_save_button)
        layout.addLayout(row)
        self.concurrency_status = QLabel('Загрузите два SQL-блока и расписание. Уровни изоляции задаются в BEGIN каждой сессии.')
        self.concurrency_status.setTextFormat(Qt.TextFormat.PlainText)
        self.concurrency_status.setWordWrap(True)
        layout.addWidget(self.concurrency_status)
        self.concurrency_example_button.clicked.connect(self._load_concurrency_example)
        self.concurrency_import_button.clicked.connect(self._import_concurrency_a)
        self.concurrency_run_button.clicked.connect(self._start_concurrency)
        self.concurrency_save_button.clicked.connect(self._save_concurrency)
        self.concurrency_view.currentIndexChanged.connect(self._concurrency_view_changed)
        for editor in (self.concurrency_a, self.concurrency_b, self.concurrency_schedule, self.concurrency_reference):
            editor.textChanged.connect(self._clear_concurrency)
        self.concurrency_timeout.valueChanged.connect(self._clear_concurrency)
        self.concurrency_confirm.toggled.connect(self._sync_concurrency_controls)

    def _concurrency_view_changed(self):
        mode = self.concurrency_view.currentData()
        self.concurrency_editors.setVisible(mode != 'result')
        self.concurrency_lower.setVisible(mode != 'sql')

    def _load_concurrency_sql(self, editor):
        filename, _ = QFileDialog.getOpenFileName(self, 'SQL сессии', '', 'SQL (*.sql)')
        if filename:
            try:
                p = Path(filename)
                if p.stat().st_size > 200000:
                    raise ValueError('SQL-файл слишком большой.')
                editor.setPlainText(p.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError) as exc:
                self.concurrency_status.setText(str(exc))

    def _import_concurrency_a(self):
        try:
            self.concurrency_a.setPlainText(self._transaction_effective_sql())
        except ValueError as exc:
            self.concurrency_status.setText(str(exc))

    def _load_concurrency_example(self):
        try:
            path = resource_root() / 'examples/transactions/university-concurrency.json'
            example = json.loads(path.read_text(encoding='utf-8'))[self.concurrency_example.currentData()]
            if any(e.toPlainText().strip() for e in (self.concurrency_a, self.concurrency_b, self.concurrency_schedule)):
                if QMessageBox.question(self, 'Загрузить пример', 'Заменить тексты A, B и расписание примером для схемы university?',
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                    return
            self.concurrency_a.setPlainText(example['sql_a'])
            self.concurrency_b.setPlainText(example['sql_b'])
            self.concurrency_schedule.setPlainText(example['schedule'])
            self.concurrency_status.setText(example['description'])
        except (OSError, ValueError, KeyError) as exc:
            self.concurrency_status.setText('Пример не загружен: ' + str(exc))

    def _clear_concurrency(self, *_):
        if not hasattr(self, 'concurrency_run_button'):
            return
        self.transaction_concurrency_report = None
        self.concurrency_commands.setRowCount(0)
        self.concurrency_locks.setRowCount(0)
        self.concurrency_details.clear()
        self.concurrency_status.setText('Входные данные изменены; требуется новый прогон двух сессий.')
        self._sync_concurrency_controls()

    def _sync_concurrency_controls(self):
        if not hasattr(self, 'concurrency_run_button'):
            return
        idle = not self._query_operation_active and not self._active_workers
        for editor in (self.concurrency_a, self.concurrency_b, self.concurrency_schedule):
            editor.setReadOnly(not idle)
        for button in self.concurrency_load_buttons:
            button.setEnabled(idle)
        for widget in (self.concurrency_reference, self.concurrency_timeout, self.concurrency_confirm,
                self.concurrency_example, self.concurrency_example_button, self.concurrency_import_button):
            widget.setEnabled(idle)
        self.concurrency_run_button.setEnabled(idle and self.concurrency_confirm.isChecked()
            and bool(self.concurrency_reference.text().strip())
            and all(e.toPlainText().strip() for e in (self.concurrency_a, self.concurrency_b, self.concurrency_schedule)))
        self.concurrency_save_button.setEnabled(idle and self.transaction_concurrency_report is not None)

    def _concurrency_snapshot(self):
        return (self.concurrency_a.toPlainText(), self.concurrency_b.toPlainText(), self.concurrency_schedule.toPlainText(),
            self._database_settings(), self.concurrency_reference.text().strip(), self.concurrency_timeout.value())

    def _start_concurrency(self):
        if self._query_operation_active or self._active_workers:
            return
        try:
            snapshot = self._concurrency_snapshot()
            a, b, schedule, settings, reference, limit = snapshot
            validate_reference(settings, reference, True, self.concurrency_confirm.isChecked())
            concurrency_request_digest(a, b, schedule, settings, reference, limit)
        except ValueError as exc:
            self.concurrency_status.setText(str(exc))
            return
        if QMessageBox.question(self, 'Две сессии на одноразовой копии',
                f'Сервер {settings.host}:{settings.port}; эталон {reference}.\n'
                f'A и B исполнят SQL на общей новой копии, включая COMMIT. Будут созданы и удалены две собственные БД. '
                f'Лимит команды {limit} мс; общий бюджет 120 с. Продолжить?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        from .desktop import ARTIFACT_ROOT, Worker
        self._clear_concurrency()
        control = AnalysisControl()
        worker = Worker(lambda: (snapshot, run_concurrent_transactions(a, b, schedule, settings, reference,
            statement_ms=limit, test_copy_confirmed=True, disposable_databases_confirmed=True,
            control=control, recovery_directory=ARTIFACT_ROOT / 'disposable-recovery')))
        self._transaction_control = control
        control.progress = worker.signals.progress.emit
        worker.signals.progress.connect(self.transaction_status.setText)
        worker.signals.progress.connect(self.concurrency_status.setText)
        worker.signals.succeeded.connect(self._concurrency_ready)
        worker.signals.failed.connect(self._concurrency_failed)
        self._set_query_actions_enabled(False)
        self._start_worker(worker)

    def _concurrency_ready(self, payload):
        snapshot, report = payload
        self._transaction_control = None
        try:
            current = self._concurrency_snapshot()
            a, b, schedule, settings, reference, limit = snapshot
            expected = concurrency_request_digest(a, b, schedule, settings, reference, limit)
        except ValueError:
            current = None
            expected = None
        if snapshot != current or report.request_digest != expected or report.reference_database != snapshot[4]:
            self.concurrency_status.setText('Устаревший результат не присоединён. Удаление копий: ' + str(report.cleanup_verified))
        else:
            self.transaction_concurrency_report = report
            values = []
            statuses = {'completed': 'выполнено', 'skipped': 'пропущено после отмены', 'conflict': 'конфликт', 'error': 'ошибка'}
            for session in report.sessions:
                for c in session.commands:
                    values.append((f'{session.label} / {c.number}', c.kind.upper(), statuses[c.status],
                        f'{c.client_ms:.2f}', str(c.row_count), c.sqlstate or '—', (c.result_sha256 or '—')[:12]))
            self.concurrency_commands.setRowCount(len(values))
            for row, columns in enumerate(values):
                for col, value in enumerate(columns):
                    self.concurrency_commands.setItem(row, col, QTableWidgetItem(value))
            self.concurrency_locks.setRowCount(len(report.locks))
            for row, item in enumerate(report.locks):
                for col, value in enumerate((f'{item.session} / {item.command}', item.event, ', '.join(item.blockers),
                        str(item.samples), f'{item.first_ms:.1f}–{item.last_ms:.1f}')):
                    self.concurrency_locks.setItem(row, col, QTableWidgetItem(value))
            lines = []
            outcomes = {'committed': 'COMMIT выполнен', 'rolled_back': 'ROLLBACK выполнен',
                'aborted': 'отменена после ошибки', 'interrupted': 'прервана; подключение закрыто с откатом'}
            for session in report.sessions:
                lines.append(f'{session.label}: {outcomes[session.outcome]}; от BEGIN до завершения {session.client_ms:.2f} мс.')
                if session.transaction_options:
                    lines.append(session.transaction_options.description)
                for command in session.commands:
                    if command.result_sha256:
                        lines.append(f'{session.label}/{command.number}: {command.row_count} строк; хеш {command.result_sha256}')
            for table in report.final_state:
                lines.append(f'Итог {table.table}: {table.rows} строк; хеш {table.sha256}')
            for sequence in report.final_sequences:
                lines.append(f'Итог последовательности {sequence.sequence}: хеш {sequence.sha256}')
            lines.extend(report.reasons)
            lines.extend(('Итоговые таблицы и последовательности зафиксированы: ' + str(bool(report.final_state)),
                'Сохранены хеши результатов; соответствие бизнес-правилам и эквивалентность изоляций не проверялись.',
                'Интервалы блокировок — точки наблюдения, а не точная сумма времени ожидания.',
                'Удаление копий: ' + str(report.cleanup_verified), 'Оставшиеся копии: ' + ', '.join(report.leftover_databases)))
            self.concurrency_details.setPlainText('\n'.join(lines))
            title = {'completed': 'Сценарий выполнен', 'error': 'Сценарий завершился ошибкой',
                'cancelled': 'Сценарий отменён', 'declined': 'Сценарий не допущен'}[report.status]
            states = sorted({c.sqlstate for s in report.sessions for c in s.commands if c.sqlstate})
            self.concurrency_status.setText(title + ('. SQLSTATE: ' + ', '.join(states) if states else '')
                + ('. ' + report.reasons[0] if report.status != 'completed' and report.reasons else '')
                + ('. Копии удалены.' if report.cleanup_verified else '. Удаление копий НЕ подтверждено: ' + ', '.join(report.leftover_databases)))
            if report.locks:
                self.concurrency_results.setCurrentWidget(self.concurrency_locks)
        self.transaction_status.setText(self.concurrency_status.text())
        self._set_query_actions_enabled(True)

    def _concurrency_failed(self, message):
        self._transaction_control = None
        self.concurrency_status.setText('Неожиданная ошибка; удаление копий НЕ подтверждено. ' + message)
        self.transaction_status.setText(self.concurrency_status.text())
        self._set_query_actions_enabled(True)

    def _show_concurrency(self):
        self.transaction_details.setPlainText(self.concurrency_details.toPlainText() or 'Две сессии: результата пока нет.')

    def _save_concurrency(self):
        if self.transaction_concurrency_report is None:
            return
        filename, _ = QFileDialog.getSaveFileName(self, 'Результат двух сессий', 'concurrency-result.json', 'JSON (*.json)')
        if filename:
            try:
                Path(filename).write_text(json.dumps(asdict(self.transaction_concurrency_report), ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError as exc:
                self.concurrency_status.setText('Не удалось сохранить результат: ' + str(exc))
