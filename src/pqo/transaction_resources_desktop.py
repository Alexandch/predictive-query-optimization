"""Separate, provenance-bound resource diagnostics, not replacement timings."""
from dataclasses import asdict
import json
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QHeaderView, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .analysis_control import AnalysisControl
from .transaction_replay import validate_reference
from .transaction_resources import prepare_resource_request, resource_request_digest, run_transaction_resources


TITLES = {'original': 'Исходная', 'indexes': 'С индексами', 'rewrite': 'С заменой SELECT', 'combined': 'Индексы + SELECT'}


def _number(value, divisor=1):
    return '—' if value is None else f'{value / divisor:.3f}' if divisor != 1 else str(value)


def _pair(counters, first, second, divisor=1):
    return _number(counters[first], divisor) + ' / ' + _number(counters[second], divisor)


class TransactionResourcesMixin:
    def _build_resource_controls(self):
        self.transaction_resource_report = None
        self.transaction_resource_page = QScrollArea()
        self.transaction_resource_page.setWidgetResizable(True)
        page = QWidget()
        self.transaction_resource_page.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        note = QLabel('Обычный контроль и отдельный EXPLAIN ANALYZE выполняются на разных свежих копиях. Буферы, temp I/O и WAL — счётчики планов.')
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.resource_mode = QComboBox()
        for label, key in (('Только исходная', 'original'), ('Сравнить с индексами', 'indexes'),
                ('Сравнить с заменой SELECT', 'rewrite'), ('Сравнить с обоими изменениями', 'combined')):
            self.resource_mode.addItem(label, key)
        self.resource_indexes_button = QPushButton('Выбрать индексы')
        self.resource_rewrite_button = QPushButton('Выбрать SELECT')
        row.addWidget(self.resource_mode)
        row.addWidget(self.resource_indexes_button)
        row.addWidget(self.resource_rewrite_button)
        row.addStretch()
        layout.addLayout(row)
        self.resource_results = QTabWidget()
        self.resource_summary = QTableWidget(0, 6)
        self.resource_summary.setHorizontalHeaderLabels(['Вариант', 'Буферы, блоки\nHit / Read', 'Temp I/O, МиБ\nRead / Write', 'WAL планов,\nКиБ', 'Макс. оператор,\nКиБ', 'Новые индексы,\nМиБ'])
        self.resource_commands = QTableWidget(0, 7)
        self.resource_commands.setHorizontalHeaderLabels(['Вариант / №', 'Команда', 'Буферы, блоки\nHit / Read', 'Temp I/O, МиБ\nRead / Write', 'WAL, КиБ', 'Оператор, КиБ', 'EXPLAIN, мс'])
        for table in (self.resource_summary, self.resource_commands):
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.verticalHeader().hide()
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.horizontalHeaderItem(2 if table is self.resource_summary else 3).setToolTip('Объём операций с временными блоками, а не размер уникальных временных файлов.')
            table.horizontalHeaderItem(4 if table is self.resource_summary else 5).setToolTip('Максимум одного отчитавшегося оператора, а не пиковая ОЗУ транзакции. «—» означает отсутствие данных.')
        self.resource_details = QPlainTextEdit()
        self.resource_details.setReadOnly(True)
        self.resource_results.addTab(self.resource_summary, 'Сводка')
        self.resource_results.addTab(self.resource_commands, 'Команды')
        self.resource_results.addTab(self.resource_details, 'Проверки и границы')
        self.resource_results.setMinimumHeight(100)
        layout.addWidget(self.resource_results, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel('Эталон:'))
        self.resource_reference = QLineEdit()
        self.resource_reference.setPlaceholderText('Отдельный лабораторный эталон')
        row.addWidget(self.resource_reference, 1)
        row.addWidget(QLabel('work_mem, КиБ:'))
        self.resource_work_mem = QSpinBox()
        self.resource_work_mem.setRange(0, 65536)
        self.resource_work_mem.setSingleStep(64)
        self.resource_work_mem.setSpecialValueText('Из эталона')
        self.resource_work_mem.setToolTip('0 — фактическая настройка эталона; иначе 64–65536 КиБ. Одинакова в обычных и инструментированных прогонах.')
        row.addWidget(self.resource_work_mem)
        layout.addLayout(row)
        self.resource_confirm = QCheckBox('Разрешаю выполнение SQL и одноразовые копии')
        layout.addWidget(self.resource_confirm)
        row = QHBoxLayout()
        self.resource_run_button = QPushButton('Измерить ресурсы на копиях')
        self.resource_run_button.setObjectName('primaryButton')
        self.resource_save_button = QPushButton('Сохранить JSON')
        row.addWidget(self.resource_run_button, 1)
        row.addWidget(self.resource_save_button)
        layout.addLayout(row)
        self.resource_status = QLabel('Загрузите транзакцию. Выбранный work_mem применяется только к одноразовым подключениям.')
        self.resource_status.setTextFormat(Qt.TextFormat.PlainText)
        self.resource_status.setWordWrap(True)
        layout.addWidget(self.resource_status)
        self.resource_run_button.clicked.connect(self._start_resource_profile)
        self.resource_save_button.clicked.connect(self._save_resource_profile)
        self.resource_indexes_button.clicked.connect(lambda: self.transaction_views.setCurrentIndex(1))
        self.resource_rewrite_button.clicked.connect(lambda: self.transaction_views.setCurrentIndex(9))
        for signal in (self.resource_mode.currentIndexChanged, self.resource_reference.textChanged,
                self.resource_work_mem.valueChanged, self.transaction_select_rewrite_variant.textChanged,
                self.transaction_index_table.itemChanged):
            signal.connect(self._clear_resource_profile)
        self.resource_confirm.toggled.connect(self._sync_resource_controls)

    def _clear_resource_profile(self, *_):
        if not hasattr(self, 'resource_run_button'):
            return
        self.transaction_resource_report = None
        self.resource_summary.setRowCount(0)
        self.resource_commands.setRowCount(0)
        self.resource_details.clear()
        self.resource_status.setText('Профиль не измерен либо сброшен после изменения входных данных.')
        self._sync_resource_controls()

    def _sync_resource_controls(self):
        if not hasattr(self, 'resource_run_button'):
            return
        idle = not self._query_operation_active and not self._active_workers
        for field in (self.resource_mode, self.resource_reference, self.resource_work_mem, self.resource_confirm,
                self.resource_indexes_button, self.resource_rewrite_button):
            field.setEnabled(idle)
        self.resource_run_button.setEnabled(idle and self.resource_confirm.isChecked()
            and bool(self.resource_reference.text().strip()) and bool(self.transaction_editor.toPlainText().strip()))
        self.resource_save_button.setEnabled(idle and self.transaction_resource_report is not None)

    def _resource_snapshot(self):
        text = self._transaction_effective_sql()
        settings = self._database_settings()
        mode = self.resource_mode.currentData()
        actions = self._replay_actions() if mode in {'indexes', 'combined'} else ()
        if mode in {'indexes', 'combined'} and not 1 <= len(actions) <= 2:
            raise ValueError('Отметьте 1–2 CREATE-кандидата в разделе индексов.')
        number, variant = None, None
        if mode in {'rewrite', 'combined'}:
            _, prepared = self._select_rewrite_prepared()
            number, variant = prepared.number, prepared.variant_sql
        memory = self.resource_work_mem.value() or None
        prepare_resource_request(text, settings, actions, number, variant, memory)
        return (self.transaction_editor.toPlainText(), text, settings, mode, actions, number, variant,
            self.resource_reference.text().strip(), memory)

    def _start_resource_profile(self):
        if self._query_operation_active or self._active_workers:
            return
        try:
            current = self._resource_snapshot()
            source, text, settings, mode, actions, number, variant, reference, memory = current
            validate_reference(settings, reference, True, self.resource_confirm.isChecked())
            count = 3 if mode == 'original' else 5
        except ValueError as exc:
            self.resource_status.setText(str(exc))
            return
        if QMessageBox.question(self, 'Профиль ресурсов на свежих копиях',
                f'Сервер {settings.host}:{settings.port}; эталон {reference}. Будут созданы и удалены до {count} собственных БД. '
                'Запись выполняется по одному разу в обычной и инструментированной копиях. '
                'EXPLAIN не возвращает значения SELECT/RETURNING: профиль проверяет их количество и итоговые данные. '
                'Исходная БД не выполняет команды. Продолжить?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        from .desktop import ARTIFACT_ROOT, Worker
        self._clear_resource_profile()
        control = AnalysisControl()
        self._transaction_control = control
        worker = Worker(lambda: (current, run_transaction_resources(text, settings, reference,
            actions=actions, number=number, variant_sql=variant, work_mem_kb=memory,
            test_copy_confirmed=True, disposable_databases_confirmed=True, control=control,
            recovery_directory=ARTIFACT_ROOT / 'disposable-recovery')))
        control.progress = worker.signals.progress.emit
        worker.signals.progress.connect(self.transaction_status.setText)
        worker.signals.progress.connect(self.resource_status.setText)
        worker.signals.succeeded.connect(self._resource_profile_ready)
        worker.signals.failed.connect(self._resource_profile_failed)
        self._set_query_actions_enabled(False)
        self._start_worker(worker)

    def _resource_profile_ready(self, payload):
        snapshot, report = payload
        self._transaction_control = None
        try:
            current = self._resource_snapshot()
            _, text, settings, _, actions, number, variant, reference, memory = snapshot
            expected = resource_request_digest(text, settings, reference, actions, number, variant, memory)
        except ValueError:
            current, expected = None, None
        if snapshot != current or report.request_digest != expected or report.reference_database != snapshot[7]:
            self.resource_status.setText('Устаревший профиль не присоединён. Удаление копий: ' + str(report.cleanup_verified))
        else:
            self.transaction_resource_report = report
            self.resource_summary.setRowCount(len(report.variants))
            rows, details = [], list(report.reasons)
            details.insert(0, 'Фактический work_mem: ' + _number(report.work_mem_kb) + ' КиБ. Число 0 в счётчике отличается от «—» (нет данных).')
            for row, item in enumerate(report.variants):
                profile = item.profile
                values = [TITLES[item.kind], '—', '—', '—', '—', '—']
                if profile:
                    values = [TITLES[item.kind], _pair(profile.counters, 'shared_hit_blocks', 'shared_read_blocks'),
                        _pair(profile.counters, 'temp_read_blocks', 'temp_written_blocks', 1024**2/profile.block_size),
                        _number(profile.counters['wal_bytes'],1024), _number(profile.max_operator_memory_kb),
                        _number(max(item.control.index_final_size_bytes,profile.index_size_bytes),1024**2)]
                    details.append(f'{TITLES[item.kind]}: данные/последовательности совпали с контролем: {profile.states_match_control}; '
                        f'количество выходных строк: {profile.output_counts_match_control}. Значения результатов EXPLAIN не возвращает.')
                    details.append(f'Размер блока: {profile.block_size} байт; крупнейшая дисковая сортировка: {_number(profile.max_sort_disk_kb)} КиБ. '
                        f'Обычный контроль: {item.control.transaction_client_ms:.3f} мс, один прогон; не оценка устойчивого ускорения.')
                    details.append('Shared dirtied / written, блоки: ' + _pair(profile.counters,'shared_dirtied_blocks','shared_written_blocks')
                        + '; WAL records / FPI: ' + _pair(profile.counters,'wal_records','wal_fpi') + '.')
                    for command in profile.commands:
                        r = command.resources
                        data = [f'{TITLES[item.kind]} / {command.number}',command.kind.upper(),'—','—','—','—','—']
                        if r:
                            data = [data[0],data[1],_pair(r.counters,'shared_hit_blocks','shared_read_blocks'),
                                _pair(r.counters,'temp_read_blocks','temp_written_blocks',1024**2/profile.block_size),
                                _number(r.counters['wal_bytes'],1024),_number(r.max_operator_memory_kb),_number(r.execution_ms)]
                        rows.append(data)
                for col, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    cell.setToolTip(value)
                    self.resource_summary.setItem(row,col,cell)
            self.resource_commands.setRowCount(len(rows))
            for row, values in enumerate(rows):
                for col, value in enumerate(values):
                    self.resource_commands.setItem(row,col,QTableWidgetItem(value))
            details.extend(('Эквивалентность обычных вариантов: ' + ('не сравнивались' if report.normal_variants_equivalent is None else 'подтверждена' if report.normal_variants_equivalent else 'НЕ подтверждена'),
                'Удаление копий: ' + str(report.cleanup_verified), 'Остались: ' + ', '.join(report.leftover_databases)))
            self.resource_details.setPlainText('\n'.join(details))
            label = {'completed':'Профиль измерен: данные и число выходных строк совпали с контролем',
                'mismatch':'Контроль не пройден; сравнение ресурсов не подтверждено',
                'error':'Профиль завершился ошибкой', 'cancelled':'Профиль отменён', 'declined':'Профиль не допущен'}[report.status]
            self.resource_status.setText(label + ('. ' + report.reasons[0] if report.status != 'completed' and report.reasons else '')
                + ('. Копии удалены.' if report.cleanup_verified else '. Удаление копий НЕ подтверждено: ' + ', '.join(report.leftover_databases)))
        self.transaction_status.setText(self.resource_status.text())
        self._set_query_actions_enabled(True)

    def _resource_profile_failed(self, message):
        self._transaction_control = None
        self.resource_status.setText('Неожиданная ошибка; удаление копий НЕ подтверждено. ' + message)
        self.transaction_status.setText(self.resource_status.text())
        self._set_query_actions_enabled(True)

    def _show_resources(self):
        self.transaction_details.setPlainText(self.resource_details.toPlainText() or 'Профиль ресурсов пока не получен.')

    def _save_resource_profile(self):
        if self.transaction_resource_report is None:
            return
        filename, _ = QFileDialog.getSaveFileName(self, 'Профиль ресурсов', 'transaction-resources.json', 'JSON (*.json)')
        if filename:
            try:
                Path(filename).write_text(json.dumps(asdict(self.transaction_resource_report), ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError as exc:
                self.resource_status.setText('Не удалось сохранить JSON: ' + str(exc))
