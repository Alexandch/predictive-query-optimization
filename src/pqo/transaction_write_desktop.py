"""Explicit DML scenario assumptions and structural write exposure; no DML replay."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .analysis_control import AnalysisControl
from .transaction_comparison import source_context_digest, sql_digest
from .transaction_replay_desktop import TransactionReplayTabMixin
from .transaction_write_pressure import WriteAssumption, _plan_input_rows, assess_write_pressure


_BUDGET_STATES = {"not_set": "лимит не задан", "exceeded": "сценарный лимит превышен",
    "within_scenario": "в пределах заданного сценарного лимита", "unknown": "данных для проверки лимита недостаточно"}
_HOT_STATES = {"not_update": "не UPDATE", "unknown": "зависимости HOT неполны",
    "new_columns_in_set": "SET затрагивает новые для индексации столбцы",
    "existing_columns_in_set": "SET затрагивает уже индексируемые столбцы",
    "no_direct_overlap": "нет прямого пересечения SET с известными индексами; HOT не гарантирован"}


def _optional_count(text, label):
    value = text.strip()
    if not value:
        return None
    if any(char not in "0123456789" for char in value):
        raise ValueError(label + ": нужно неотрицательное целое число либо пустое поле.")
    return int(value)


class TransactionWriteTabMixin(TransactionReplayTabMixin):
    def _build_transaction_write_controls(self):
        self.transaction_write_report = None
        self._transaction_write_active = False
        self.transaction_write_page = QScrollArea()
        self.transaction_write_page.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        self.transaction_write_page.setWidget(content)
        note = QLabel("Структурный сценарий, без выполнения SQL. Строки задайте явно; пусто = неизвестно, 0 = явное допущение. "
            "Единица — строка × новый индекс, не миллисекунды и не число операций B-tree.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.transaction_write_source = QComboBox()
        self.transaction_write_source.addItem("Вариант выбранной строки сравнения + его замер чтений", "comparison")
        self.transaction_write_source.addItem("Отмеченные кандидаты, включая DML (без замера набора)", "checked")
        layout.addWidget(self.transaction_write_source)
        row = QHBoxLayout()
        row.addWidget(QLabel("Сценарный лимит:"))
        self.transaction_write_budget = QLineEdit()
        self.transaction_write_budget.setMaxLength(16)
        self.transaction_write_budget.setPlaceholderText("Не задан; единицы строка × индекс")
        row.addWidget(self.transaction_write_budget, 1)
        self.transaction_write_button = QPushButton("Оценить запись без выполнения")
        row.addWidget(self.transaction_write_button)
        layout.addLayout(row)
        self.transaction_write_status = QLabel("Сначала получите кандидатов. Число строк не берётся автоматически из EXPLAIN.")
        self.transaction_write_status.setTextFormat(Qt.TextFormat.PlainText)
        self.transaction_write_status.setWordWrap(True)
        layout.addWidget(self.transaction_write_status)
        self.transaction_write_table = QTableWidget(0, 7)
        self.transaction_write_table.setHorizontalHeaderLabels(
            ["DML №", "Тип", "Таблицы", "Повторов", "Строк/вызов", "Поток EXPLAIN", "Строка × индекс"])
        self.transaction_write_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.transaction_write_table.verticalHeader().setVisible(False)
        self.transaction_write_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.transaction_write_table.setMinimumHeight(160)
        layout.addWidget(self.transaction_write_table)
        self.transaction_write_button.clicked.connect(self._start_transaction_write)
        self.transaction_write_source.currentIndexChanged.connect(self._transaction_write_input_changed)
        self.transaction_write_budget.textChanged.connect(self._transaction_write_input_changed)
        self.transaction_compare_table.itemSelectionChanged.connect(self._transaction_write_input_changed)
        self.transaction_index_table.itemChanged.connect(self._transaction_write_input_changed)

    def _write_actions_and_trial(self):
        if self.transaction_write_source.currentData() == "checked":
            report = self.transaction_index_report
            return (tuple(item.action for row, item in enumerate(report.candidates)
                if item.status != "covered" and self.transaction_index_table.item(row, 0) is not None
                and self.transaction_index_table.item(row, 0).checkState() == Qt.CheckState.Checked) if report else ()), None
        comparison = self.transaction_compare_report
        row = self.transaction_compare_table.currentRow()
        if comparison is None or not 0 <= row < len(comparison.trials):
            return (), None
        trial = comparison.trials[row]
        return (trial.actions, trial) if trial.status == "measured" and trial.rollback_verified else ((), None)

    def _write_assumptions_input(self, *, commit=False):
        values = []
        for row in range(self.transaction_write_table.rowCount()):
            spin = self.transaction_write_table.cellWidget(row, 3)
            if commit:
                spin.interpretText()
            values.append(WriteAssumption(int(self.transaction_write_table.item(row, 0).text()), spin.value(),
                _optional_count(self.transaction_write_table.cellWidget(row, 4).text(), "Строки")))
        return tuple(values)

    def _render_transaction_write_inputs(self):
        self._clear_transaction_write(reset_inputs=True)
        plans = self.transaction_plan_report
        if plans is None:
            return
        writes = [item for item in plans.inspection.statements if item.changes_data]
        by_number = {plan.number: plan for plan in plans.statements}
        self.transaction_write_table.setRowCount(len(writes))
        for row, item in enumerate(writes):
            hint = _plan_input_rows(by_number.get(item.number), item.kind)
            for column, value in ((0, str(item.number)), (1, item.kind.upper()), (2, ", ".join(item.relation_names)),
                    (5, f"{hint:,.0f}" if hint is not None else "неизвестно"), (6, "—")):
                cell = QTableWidgetItem(value)
                cell.setToolTip("Оценочный вход ModifyTable, не число изменённых строк." if column == 5 else value)
                self.transaction_write_table.setItem(row, column, cell)
            spin = QSpinBox()
            spin.setRange(1, 10000)
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(self._transaction_write_input_changed)
            self.transaction_write_table.setCellWidget(row, 3, spin)
            rows = QLineEdit()
            rows.setMaxLength(10)
            rows.setPlaceholderText("Неизвестно")
            rows.textChanged.connect(self._transaction_write_input_changed)
            self.transaction_write_table.setCellWidget(row, 4, rows)
        self._sync_transaction_write_controls()

    def _clear_transaction_write(self, *, reset_inputs=False):
        if not hasattr(self, "transaction_write_page"):
            return
        self.transaction_write_report = None
        if reset_inputs:
            self.transaction_write_table.setRowCount(0)
        else:
            for row in range(self.transaction_write_table.rowCount()):
                self.transaction_write_table.setItem(row, 6, QTableWidgetItem("—"))
        self.transaction_write_status.setText("Оценка не рассчитана для текущего набора и допущений. Неизвестные строки не считаются нулём.")
        self._show_transaction_write()

    def _transaction_write_input_changed(self, *_):
        if not hasattr(self, "transaction_write_page"):
            return
        self._clear_transaction_write()
        self._sync_transaction_write_controls()

    def _sync_transaction_write_controls(self):
        if not hasattr(self, "transaction_write_page"):
            return
        idle = not self._query_operation_active and not self._active_workers
        actions, _ = self._write_actions_and_trial()
        self.transaction_write_table.setEnabled(idle)
        self.transaction_write_source.setEnabled(idle)
        self.transaction_write_budget.setEnabled(idle)
        self.transaction_write_button.setEnabled(idle and self.transaction_plan_report is not None
            and self.transaction_index_report is not None and 1 <= len(actions) <= 3)

    def _start_transaction_write(self):
        if self._query_operation_active or self._active_workers:
            return
        try:
            source, effective = self.transaction_editor.toPlainText(), self._transaction_effective_sql()
            settings = self._database_settings()
            plans, indices = self.transaction_plan_report, self.transaction_index_report
            actions, trial = self._write_actions_and_trial()
            if plans is None or indices is None or not actions:
                raise ValueError("Нужны актуальные планы/кандидаты и выбранный набор индексов.")
            choice = self.transaction_write_source.currentData()
            if trial is not None and (self.transaction_compare_report.development_sql_digest != sql_digest(effective)
                    or self.transaction_compare_report.source_context_digest != source_context_digest(settings)):
                raise ValueError("Замер чтений не связан с текущим контекстом; повторите сравнение.")
            assumptions = self._write_assumptions_input(commit=True)
            budget = _optional_count(self.transaction_write_budget.text(), "Лимит")
        except ValueError as exc:
            self.transaction_write_status.setText(str(exc))
            return
        from .desktop import Worker
        self._clear_transaction_write()
        self._transaction_write_active = True
        control = AnalysisControl()
        worker = Worker(lambda: (source, effective, settings, plans, indices, actions, assumptions, budget, choice, trial,
            assess_write_pressure(effective, plans, indices, actions, assumptions=assumptions,
                budget=budget, read_trial=trial, control=control)))
        self._transaction_control = control
        control.progress = worker.signals.progress.emit
        worker.signals.progress.connect(self.transaction_status.setText)
        worker.signals.succeeded.connect(self._transaction_write_ready)
        worker.signals.failed.connect(self._transaction_write_failed)
        self._set_query_actions_enabled(False)
        self.transaction_status.setText("Локальная оценка сценария записи; подключения/SQL/DDL нет…")
        self._start_worker(worker)

    def _transaction_write_ready(self, payload):
        source, effective, settings, plans, indices, actions, assumptions, budget, choice, trial, report = payload
        self._transaction_write_active = False
        self._transaction_control = None
        try:
            current = (self._database_settings(), self._write_actions_and_trial(), self._write_assumptions_input(),
                _optional_count(self.transaction_write_budget.text(), "Лимит"))
        except ValueError:
            current = None
        if (not self._transaction_snapshot_current(source, effective) or current != (settings, (actions, trial), assumptions, budget)
                or plans is not self.transaction_plan_report or indices is not self.transaction_index_report
                or choice != self.transaction_write_source.currentData()):
            self.transaction_status.setText("Устаревшая оценка записи не показана. SQL не выполнялся.")
        else:
            self.transaction_write_report = report
            for row, item in enumerate(report.rows):
                self.transaction_write_table.setItem(row, 6, QTableWidgetItem(f"{item.exposure:,}" if item.exposure is not None else "неизвестно"))
            self.transaction_write_status.setText(f"Известная часть: {report.known_exposure:,} строка × индекс; "
                f"неизвестных команд: {len(report.unknown_commands)}; {_BUDGET_STATES[report.budget_state]}. "
                + ("Структурный охват неполный. " if not report.scope_complete else "")
                + "Чистый выигрыш по времени не вычислен; DML не выполнялся.")
            self.transaction_status.setText(self.transaction_write_status.text())
            self.transaction_views.setCurrentIndex(7)
            self._show_transaction_write()
        self._set_query_actions_enabled(True)

    def _transaction_write_failed(self, message):
        self._transaction_write_active = False
        self._transaction_control = None
        self.transaction_write_status.setText("Локальная оценка записи не завершена/отменена. SQL и DDL не выполнялись. " + message)
        self.transaction_status.setText(self.transaction_write_status.text())
        self._set_query_actions_enabled(True)

    def _show_transaction_write(self):
        if not hasattr(self, "transaction_views") or self.transaction_views.currentIndex() != 7:
            return
        from .desktop import _format_index_action
        report = self.transaction_write_report
        if report is None:
            self.transaction_details.setPlainText("СЦЕНАРИЙ ЗАПИСИ — БЕЗ ЗАПУСКА SQL\nВыберите набор и задайте допущения по строкам/повторам. "
                "Пусто означает неизвестно, не 0. Поток EXPLAIN справочный. Накопленная доля HOT не прогноз будущего HOT.")
            return
        lines = ["ЗАПИСЬ И ИНДЕКСЫ — СТРУКТУРНЫЙ СЦЕНАРИЙ, НЕ ЗАМЕР", *map(_format_index_action, report.actions),
            f"Известная часть: {report.known_exposure:,}; неизвестные команды: {report.unknown_commands or 'нет'}.",
            f"Лимит: {report.budget if report.budget is not None else 'не задан'}; {_BUDGET_STATES[report.budget_state]}.",
            "Структурный охват: " + ("прямые записи учтены при заданных допущениях." if report.scope_complete else "неполный; побочные записи не исключены.")]
        if report.read_trial is not None:
            trial = report.read_trial
            lines.extend((f"Замер SELECT выбранной пробы на копии {trial.copy_database}: {trial.weighted_baseline_ms:.3f} → {trial.weighted_indexed_ms:.3f} мс; "
                f"выигрыш {trial.weighted_gain_ms:.3f} мс, веса {dict(trial.frequencies)}.",
                "Подтверждение этой пробы чтений: " + ("да." if trial.accepted_for_reads else "нет."),
                f"Размер набора на копии: {trial.index_size_bytes / 1024 / 1024:.2f} МиБ; построение {trial.build_time_ms:.1f} мс.",
                "Это отдельная проба, не гарантированный/чистый выигрыш транзакции. Чтения и запись имеют разные единицы и не вычитаются."))
        else:
            lines.append("Замера чтений для этого набора нет; три отмеченных кандидата не наследуют замер пары.")
        for item in report.rows:
            lines.extend(("", f"Команда {item.number}: {item.kind.upper()}, таблицы {item.targets or 'не разрешены'}.",
                f"Повторов {item.repetitions}; строк/вызов {item.rows_per_call if item.rows_per_call is not None else 'неизвестно'}; новых индексов {item.index_count}.",
                f"Строка × индекс: {item.exposure if item.exposure is not None else 'неизвестно'}; поток плана {item.plan_input_rows if item.plan_input_rows is not None else 'неизвестно'} (не изменённые строки).",
                f"SET: ключи {item.key_columns_in_set or 'нет прямого пересечения'}, INCLUDE {item.include_columns_in_set or 'нет прямого пересечения'}.",
                "HOT: " + _HOT_STATES[item.hot_status] + ".",
                f"Новые столбцы ограничения HOT относительно известных существующих индексов: {item.incremental_hot_columns if item.incremental_hot_columns is not None else 'не установлены'}.",
                "Накопленная доля HOT: " + (f"{item.historical_hot_percent:.1f}% (не вероятность будущего HOT)." if item.historical_hot_percent is not None else "нет сопоставимых счётчиков."),
                *item.limitations))
        lines.extend(("", *report.notes))
        self.transaction_details.setPlainText("\n".join(lines))
