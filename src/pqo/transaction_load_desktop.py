"""Multi-client workload tab for the current transaction."""
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path

from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .analysis_control import AnalysisControl
from .transaction_load import LoadOptions, run_transaction_load
from .transaction_replay import validate_reference


class TransactionLoadMixin:
    def _build_load_controls(self):
        self.transaction_load_report = None
        self.transaction_load_page = QScrollArea()
        self.transaction_load_page.setWidgetResizable(True)
        page = QWidget()
        self.transaction_load_page.setWidget(page)
        layout = QVBoxLayout(page)
        intro = QLabel('Клиенты одновременно выполняют SQL из редактора транзакции на одной копии. '
                       'Для сравнения изоляции каждый вариант получает свою свежую копию.')
        intro.setWordWrap(True)
        layout.addWidget(intro)

        fields = QFormLayout()
        self.load_reference = QLineEdit()
        fields.addRow('Лабораторный эталон', self.load_reference)
        for name, label, minimum, maximum, default in (
            ('load_clients', 'Клиентов', 2, 12, 4),
            ('load_repetitions', 'Транзакций на клиента', 1, 20, 4),
            ('load_timeout', 'Тайм-аут команды, мс', 100, 10000, 5000),
        ):
            field = QSpinBox()
            field.setRange(minimum, maximum)
            field.setValue(default)
            setattr(self, name, field)
            fields.addRow(label, field)
            field.valueChanged.connect(self._clear_transaction_load)
        self.load_isolation = QComboBox()
        self.load_isolation.addItem('Только исходный уровень', None)
        for level in ('READ COMMITTED', 'REPEATABLE READ', 'SERIALIZABLE', 'READ UNCOMMITTED'):
            self.load_isolation.addItem(level, level)
        fields.addRow('Сравнить с', self.load_isolation)
        layout.addLayout(fields)

        self.load_confirm = QCheckBox('Разрешаю выполнение транзакций на одноразовых копиях')
        layout.addWidget(self.load_confirm)
        buttons = QHBoxLayout()
        self.load_run_button = QPushButton('Измерить нагрузку на копии')
        self.load_run_button.setObjectName('primaryButton')
        self.load_save_button = QPushButton('Сохранить JSON')
        buttons.addWidget(self.load_run_button, 1)
        buttons.addWidget(self.load_save_button)
        layout.addLayout(buttons)
        self.load_status = QLabel('Подготовьте SQL и лабораторный эталон.')
        self.load_status.setWordWrap(True)
        layout.addWidget(self.load_status)

        self.load_table = QTableWidget(0, 7)
        self.load_table.setHorizontalHeaderLabels([
            'Изоляция', 'COMMIT', 'ROLLBACK', 'Ошибки', 'Завершено/с', 'p50, мс', 'p95, мс',
        ])
        self.load_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.load_table.verticalHeader().hide()
        self.load_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.load_table, 1)
        self.load_details = QPlainTextEdit()
        self.load_details.setReadOnly(True)
        layout.addWidget(self.load_details, 1)
        self.load_reference.textChanged.connect(self._clear_transaction_load)
        self.load_isolation.currentIndexChanged.connect(self._clear_transaction_load)
        self.load_confirm.toggled.connect(self._sync_load_controls)
        self.load_run_button.clicked.connect(self._start_transaction_load)
        self.load_save_button.clicked.connect(self._save_transaction_load)

    def _clear_transaction_load(self, *_):
        if not hasattr(self, 'load_table'):
            return
        self.transaction_load_report = None
        self.load_table.setRowCount(0)
        self.load_details.clear()
        self.load_status.setText('Задайте параметры и запустите прогон нагрузки.')
        self._sync_load_controls()

    def _sync_load_controls(self):
        if not hasattr(self, 'load_table'):
            return
        idle = not self._active_workers and not self._query_operation_active
        for field in (self.load_reference, self.load_clients, self.load_repetitions,
                      self.load_timeout, self.load_isolation, self.load_confirm):
            field.setEnabled(idle)
        self.load_run_button.setEnabled(idle and self.load_confirm.isChecked()
            and bool(self.load_reference.text().strip()) and bool(self.transaction_editor.toPlainText().strip()))
        self.load_save_button.setEnabled(idle and self.transaction_load_report is not None)

    def _load_snapshot(self):
        return (self._transaction_effective_sql(), self._database_settings(), self.load_reference.text().strip(),
            LoadOptions(self.load_clients.value(), self.load_repetitions.value(), self.load_timeout.value(),
                        self.load_isolation.currentData()))

    def _start_transaction_load(self):
        if self._active_workers or self._query_operation_active:
            return
        try:
            snapshot = self._load_snapshot()
            _, settings, reference, options = snapshot
            validate_reference(settings, reference, True, self.load_confirm.isChecked())
        except ValueError as exc:
            self.load_status.setText(str(exc))
            return
        confirmation = QMessageBox.question(self, 'Нагрузка на одноразовых копиях',
            f'{settings.host}:{settings.port}, эталон {reference}.\n'
            f'До {options.clients} клиентов и {options.clients * options.repetitions} транзакций на вариант. '
            'Исходная транзакция выполняется с её COMMIT или ROLLBACK на копиях. Начать?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        from .desktop import ARTIFACT_ROOT, Worker

        self._clear_transaction_load()
        control = AnalysisControl()
        worker = Worker(lambda: (snapshot, run_transaction_load(*snapshot,
            test_copy_confirmed=True, disposable_databases_confirmed=True, control=control,
            recovery_directory=ARTIFACT_ROOT / 'disposable-recovery')))
        self._transaction_control = control
        control.progress = worker.signals.progress.emit
        worker.signals.progress.connect(self.load_status.setText)
        worker.signals.progress.connect(self.transaction_status.setText)
        worker.signals.succeeded.connect(self._transaction_load_ready)
        worker.signals.failed.connect(self._transaction_load_failed)
        self._set_query_actions_enabled(False)
        self._start_worker(worker)

    def _transaction_load_ready(self, payload):
        snapshot, report = payload
        self._transaction_control = None
        try:
            current = self._load_snapshot()
        except ValueError:
            current = None
        if current != snapshot:
            self._clear_transaction_load()
            message = f'Настройки изменились. Удаление копий: {"подтверждено" if report.cleanup_verified else "не подтверждено"}.'
        else:
            self.transaction_load_report = report
            self.load_table.setRowCount(len(report.phases))
            lines = list(report.reasons)
            for row, phase in enumerate(report.phases):
                numeric = ('—' if value is None else f'{value:.2f}'
                           for value in (phase.completed_per_second, phase.p50_ms, phase.p95_ms))
                values = (phase.isolation, str(phase.committed), str(phase.rolled_back), str(phase.failed), *numeric)
                for column, value in enumerate(values):
                    self.load_table.setItem(row, column, QTableWidgetItem(value))
                states = Counter(a.sqlstate or 'не указан' for a in phase.attempts if a.outcome == 'failed')
                if states:
                    lines.append(f'{phase.isolation} · ' + ', '.join(f'{code}: {count}' for code, count in sorted(states.items())))
            if len(report.phases) == 2:
                first, second = report.phases
                same = (first.final_state, first.final_sequences) == (second.final_state, second.final_sequences)
                lines.append('Итоговые состояния совпали.' if same else 'Итоговые состояния различаются.')
            lines.append('p50 и p95 включают завершившиеся транзакции. Конфликты и тайм-ауты считаются отдельно.')
            lines.append('Подходящий уровень изоляции определяется требованиями к согласованности данных.')
            if report.leftover_databases:
                lines.append('Оставшиеся копии: ' + ', '.join(report.leftover_databases))
            self.load_details.setPlainText('\n'.join(lines))
            status = {'completed': 'Прогон завершён', 'cancelled': 'Прогон отменён',
                      'declined': 'Прогон не допущен', 'error': 'Прогон завершился ошибкой'}[report.status]
            message = f'{status}. Удаление копий: {"подтверждено" if report.cleanup_verified else "не подтверждено"}.'
        self.load_status.setText(message)
        self.transaction_status.setText(message)
        self._set_query_actions_enabled(True)

    def _transaction_load_failed(self, message):
        self._transaction_control = None
        self.load_status.setText(message)
        self.transaction_status.setText(message)
        self._set_query_actions_enabled(True)

    def _save_transaction_load(self):
        if self.transaction_load_report is None:
            return
        filename, _ = QFileDialog.getSaveFileName(self, 'Отчёт нагрузки', 'transaction-load.json', 'JSON (*.json)')
        if filename:
            try:
                Path(filename).write_text(json.dumps(asdict(self.transaction_load_report), ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError as exc:
                self.load_status.setText('Не удалось сохранить отчёт: ' + str(exc))
