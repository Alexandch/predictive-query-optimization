"""Transaction inventory and estimated-plan tab, kept separate from SELECT trials."""
from collections import Counter
import json
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .analysis_control import AnalysisControl
from .transaction_inspection import inspect_transaction
from .transaction_planning import collect_transaction_plans
from .transaction_indexes import collect_transaction_index_candidates
from .transaction_trial_desktop import TransactionTrialTabMixin
from .transaction_workspace import TransactionWorkspaceMixin


_WARNINGS = {
    "Data-changing command: execution requires an isolated test copy":
        "Операция записи: для фактического запуска нужна отдельная тестовая копия БД.",
    "Row locks requested; this is not an ordinary read-only query":
        "Запрошены блокировки строк; это не обычное чтение.",
    "Function effects are unknown; SELECT alone does not establish safety":
        "Побочные эффекты функции неизвестны; SELECT не означает безопасность.",
    "Command structure is not supported by the planning stage":
        "Этот тип команды пока не поддерживается для получения плана.",
    "Unqualified relation names require search_path resolution against the database":
        "Для физических таблиц укажите явные имена схема.таблица.",
    "Unresolved parameters require explicit values before planning":
        "Перед получением плана подставьте конкретные значения параметров.",
}
_STATUSES = {"planned": "план получен", "blocked": "не поддерживается",
             "error": "ошибка плана", "budget_exhausted": "лимит времени"}
_INDEX_STATUSES = {"candidate": "к проверке", "covered": "уже покрыт",
                   "covering_review": "только INCLUDE — спорно"}


class TransactionTabMixin(TransactionWorkspaceMixin, TransactionTrialTabMixin):
    def _build_transaction_tab(self):
        self.transaction_inspection = None
        self.transaction_plan_report = None
        self.transaction_index_report = None
        self._transaction_control = None
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("Транзакции: индексы и переписывание SELECT на копиях")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        full_note = (
            "Вставьте один блок BEGIN … COMMIT/ROLLBACK. Разбор выполняется локально. "
            "Планы и подбор кандидатов не запускают команды и не создают индексы. "
            "SELECT-проба выполняется отдельно на подтверждённой тестовой копии. "
            "Реальная запись доступна отдельно в «Выполнение на копиях» и «Переписывание SELECT»: ограниченные режимы с новыми одноразовыми БД."
        )
        note = QLabel('Разбор и планы не запускают команды. Фактические пробы — на тестовых копиях; запись и COMMIT — только на новых одноразовых БД.')
        note.setToolTip(full_note)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.transaction_editor = QPlainTextEdit()
        self.transaction_editor.setFont(QFont("Consolas", 11))
        self.transaction_editor.setPlainText("BEGIN;\nSELECT 1;\nCOMMIT;")
        actions = QHBoxLayout()
        self.transaction_load_button = QPushButton("Открыть SQL-файл")
        self.transaction_inspect_button = QPushButton("Разобрать без подключения к БД")
        self.transaction_inspect_button.setObjectName("primaryButton")
        self.transaction_plan_button = QPushButton("Получить оценочные планы")
        self.transaction_cancel_button = QPushButton("Отменить анализ транзакции")
        for button in (self.transaction_load_button, self.transaction_inspect_button,
                       self.transaction_plan_button, self.transaction_cancel_button):
            actions.addWidget(button)
        self.transaction_status = QLabel("Транзакция не запускалась. Начните с локального разбора.")
        self.transaction_status.setTextFormat(Qt.TextFormat.PlainText)
        self.transaction_status.setWordWrap(True)
        self.transaction_table = QTableWidget(0, 7)
        self.transaction_table.setHorizontalHeaderLabels(
            ["№", "Команда", "Запись", "Таблицы", "Проверка", "Узел", "Стоимость (не мс)"]
        )
        self.transaction_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.transaction_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.transaction_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.transaction_table.verticalHeader().setVisible(False)
        self.transaction_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.transaction_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.transaction_details = QPlainTextEdit()
        self.transaction_details.setReadOnly(True)
        self.transaction_details.setFont(QFont("Consolas", 10))
        self.transaction_index_button = QPushButton("Подобрать общие индексы (без создания)")
        self.transaction_index_summary = QLabel("Сначала получите оценочные планы команд.")
        self.transaction_index_summary.setWordWrap(True)
        self.transaction_index_summary.setTextFormat(Qt.TextFormat.PlainText)
        index_actions = QHBoxLayout()
        index_actions.addWidget(self.transaction_index_button)
        index_actions.addWidget(self.transaction_index_summary, 1)
        self.transaction_index_table = QTableWidget(0, 6)
        self.transaction_index_table.setHorizontalHeaderLabels(
            ["Ключи / таблица", "INCLUDE", "SELECT №", "Поиск в DML №", "Запись №", "Статус"])
        self.transaction_index_table.horizontalHeaderItem(0).setToolTip(
            "SELECT-кандидаты отмечаются для сравнения чтений. Флажки кандидатов только для DML доступны для сценарной оценки и отдельного выполнения на одноразовых копиях; обычные SELECT-пробы их не измеряют.")
        self.transaction_index_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.transaction_index_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.transaction_index_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.transaction_index_table.verticalHeader().setVisible(False)
        self.transaction_index_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.transaction_index_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.transaction_index_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.transaction_index_table.setColumnWidth(1, 160)
        trial_actions = self._build_transaction_trial_controls()
        comparison_actions = self._build_transaction_comparison_controls()
        self._build_transaction_parameter_controls()
        self._build_transaction_holdout_controls()
        self._build_transaction_write_controls()
        self._build_transaction_replay_controls()
        self._build_transaction_select_rewrite_controls()
        self.transaction_views = QTabWidget()
        self.transaction_views.addTab(self.transaction_table, "Команды и планы")
        self.transaction_views.addTab(self.transaction_index_table, "Индексные кандидаты")
        self.transaction_views.addTab(self.transaction_trial_table, "Проба на копии")
        self.transaction_views.addTab(self.transaction_frequency_table, "Повторы SELECT")
        self.transaction_views.addTab(self.transaction_compare_table, "Сравнение индексов")
        self.transaction_views.addTab(self.transaction_parameter_page, "Параметры SQL")
        self.transaction_views.addTab(self.transaction_holdout_page, "Контроль SELECT")
        self.transaction_views.addTab(self.transaction_write_page, "Запись и индексы")
        self.transaction_views.addTab(self.transaction_replay_page, "Выполнение на копиях")
        self.transaction_views.addTab(self.transaction_select_rewrite_page, "Переписывание SELECT")
        layout.addLayout(actions)
        self._build_transaction_workspace(layout, index_actions, trial_actions, comparison_actions)
        layout.addWidget(self.transaction_status)
        full_limits = (
            "Планы относятся к исходному состоянию БД: последующие команды не видят изменений предыдущих. "
            "Стоимость PostgreSQL — не время в миллисекундах. Обычные индексные SELECT-пробы не выполняют DML. "
            "Полная запись и COMMIT доступны отдельно в «Выполнение на копиях» и «Переписывание SELECT» для ограниченного SQL на новых одноразовых БД. "
            "Применения к исходной БД нет. SQL автоматически не сохраняется."
        )
        limits = QLabel('Планы относятся к исходному состоянию; стоимость PostgreSQL — не миллисекунды. Применения к исходной БД нет. SQL автоматически не сохраняется.')
        limits.setToolTip(full_limits)
        limits.setWordWrap(True)
        limits.setObjectName("mutedLabel")
        layout.addWidget(limits)
        self.transaction_load_button.clicked.connect(self._load_transaction_file)
        self.transaction_inspect_button.clicked.connect(self._start_transaction_inspection)
        self.transaction_plan_button.clicked.connect(self._start_transaction_planning)
        self.transaction_cancel_button.clicked.connect(self._cancel_transaction_planning)
        self.transaction_index_button.clicked.connect(self._start_transaction_indexes)
        self.transaction_table.itemSelectionChanged.connect(self._show_transaction_statement)
        self.transaction_index_table.itemSelectionChanged.connect(self._transaction_index_selection_changed)
        self.transaction_views.currentChanged.connect(self._show_transaction_view)
        self.transaction_editor.textChanged.connect(self._invalidate_transaction)
        for editor in (self.host_edit, self.database_edit, self.user_edit,
                       self.password_edit, self.allowed_schemas_edit):
            editor.textChanged.connect(self._invalidate_transaction_plans)
        self.port_spin.valueChanged.connect(self._invalidate_transaction_plans)
        self.tabs.insertTab(1, page, "Транзакции")
        self._sync_transaction_controls()

    def _sync_transaction_controls(self):
        if not hasattr(self, "transaction_editor"):
            return
        idle = not self._query_operation_active and not self._active_workers
        self.transaction_editor.setReadOnly(not idle)
        self.transaction_load_button.setEnabled(idle)
        self.transaction_inspect_button.setEnabled(idle and bool(self.transaction_editor.toPlainText().strip()))
        self.transaction_plan_button.setEnabled(idle and self.transaction_inspection is not None)
        self.transaction_index_button.setEnabled(idle and self.transaction_plan_report is not None
            and any(plan.status == "planned" for plan in self.transaction_plan_report.statements))
        self.transaction_cancel_button.setEnabled(
            self._transaction_control is not None and not self._transaction_control.cancelled.is_set()
        )
        self._sync_transaction_trial_controls()
        self._sync_transaction_parameter_controls()
        self._sync_transaction_replay_controls()
        self._sync_transaction_select_rewrite_controls()

    def _invalidate_transaction(self):
        self.transaction_inspection = None
        self.transaction_plan_report = None
        self._clear_transaction_indexes()
        self._clear_transaction_parameters()
        self.transaction_table.setRowCount(0)
        self.transaction_frequency_table.setRowCount(0)
        self.transaction_details.clear()
        self.transaction_status.setText("Текст изменён. Выполните локальный разбор заново; прежние планы сброшены.")
        self._sync_transaction_controls()

    def _invalidate_transaction_plans(self):
        if self.transaction_plan_report is not None:
            self.transaction_plan_report = None
            self._clear_transaction_indexes()
            self._render_transaction_inventory()
            self.transaction_status.setText("Подключение изменено. Прежние планы сброшены; запросите их заново.")
            self._sync_transaction_controls()

    def _load_transaction_file(self):
        if self._query_operation_active:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Открыть транзакцию", "", "SQL (*.sql);;Все файлы (*)")
        if not filename:
            return
        try:
            path = Path(filename)
            if path.stat().st_size > 2_000_000:
                raise ValueError("Файл слишком большой (лимит 2 МБ).")
            text = path.read_text(encoding="utf-8-sig")
            if len(text) > 500_000:
                raise ValueError("Лимит текста — 500000 символов.")
            self.transaction_editor.setPlainText(text)
        except (OSError, UnicodeError, ValueError) as exc:
            self.transaction_status.setText(f"Не удалось открыть файл: {exc}")

    def _start_transaction_inspection(self):
        if self._query_operation_active or self._active_workers:
            self.transaction_status.setText("Дождитесь завершения текущей фоновой операции.")
            return
        from .desktop import Worker
        text = self.transaction_editor.toPlainText()
        self._invalidate_transaction()
        self._set_query_actions_enabled(False)
        self.transaction_status.setText("Локальный разбор команд; подключения к БД нет…")
        worker = Worker(lambda: (text, inspect_transaction(text)))
        worker.signals.succeeded.connect(self._transaction_inspected)
        worker.signals.failed.connect(self._transaction_failed)
        self._start_worker(worker)

    def _transaction_inspected(self, payload):
        text, inspection = payload
        if text == self.transaction_editor.toPlainText():
            self.transaction_inspection = inspection
            self._render_transaction_parameter_inputs()
            self._render_transaction_inventory()
            self._transaction_reveal_analysis()
            writes = sum(item.changes_data for item in inspection.statements)
            self.transaction_status.setText(
                f"Разобрано команд: {len(inspection.statements)}; с записью: {writes}; "
                f"завершение: {inspection.ending.upper()}. Ничего не выполнялось. "
                + (self._transaction_parameter_problem if self._transaction_parameter_problem else
                    "Заполните и примените типы/значения во вкладке «Параметры SQL»." if self._transaction_parameter_slots
                    else "Теперь можно отдельно получить оценочные планы.")
            )
        self._set_query_actions_enabled(True)

    def _start_transaction_planning(self):
        if self._query_operation_active or self._active_workers or self.transaction_inspection is None:
            return
        try:
            settings = self._database_settings()
            effective = self._transaction_effective_sql()
        except ValueError as exc:
            self.transaction_status.setText(str(exc))
            return
        answer = QMessageBox.question(self, "Оценочные планы транзакции",
            f"База: {settings.host}:{settings.port}/{settings.dbname}\n"
            "Будут запрошены только EXPLAIN без ANALYZE в режиме READ ONLY. "
            "Команды записи не выполняются, но планирование берёт блокировки и нагружает сервер. "
            "Это не песочница для неизвестных функций/расширений БД. Рекомендуется тестовая копия.\nПродолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        from .desktop import Worker
        text = self.transaction_editor.toPlainText()
        self.transaction_plan_report = None
        self._clear_transaction_indexes()
        self._render_transaction_inventory()
        control = AnalysisControl()
        worker = Worker(lambda: (text, settings, effective, collect_transaction_plans(effective, settings, control=control)))
        control.progress = worker.signals.progress.emit
        self._transaction_control = control
        worker.signals.progress.connect(self.transaction_status.setText)
        worker.signals.succeeded.connect(self._transaction_planned)
        worker.signals.failed.connect(self._transaction_failed)
        self._set_query_actions_enabled(False)
        self.transaction_status.setText("Подключение для оценочных планов; транзакция не запускается…")
        self._start_worker(worker)

    def _transaction_planned(self, payload):
        if len(payload) == 3:
            text, settings, report = payload
            effective = text
        else:
            text, settings, effective, report = payload
        self._transaction_control = None
        try:
            current_settings = self._database_settings()
        except ValueError:
            current_settings = None
        if not self._transaction_snapshot_current(text, effective) or settings != current_settings:
            self.transaction_status.setText("Текст или подключение изменены. Устаревшие планы не показаны.")
        else:
            self.transaction_plan_report = report
            self._render_transaction_inventory()
            self._transaction_reveal_analysis()
            planned = sum(item.status == "planned" for item in report.statements)
            self.transaction_status.setText(
                f"База {report.database_name}: планов получено {planned}/{len(report.statements)}. "
                "Остальные причины указаны по командам. Транзакция не запускалась, данные не изменялись."
            )
        self._set_query_actions_enabled(True)

    def _transaction_failed(self, message):
        if self._transaction_select_rewrite_active:
            self._transaction_select_rewrite_failed(message)
            return
        if self._transaction_replay_active:
            self._transaction_replay_failed(message)
            return
        if self._transaction_write_active:
            self._transaction_write_failed(message)
            return
        if self._transaction_holdout_active:
            self._transaction_holdout_failed(message)
            return
        if self._transaction_comparison_active:
            self._transaction_comparison_failed(message)
            return
        if self._transaction_trial_active:
            self._transaction_index_trial_failed(message)
            return
        cancelled = self._transaction_control is not None and self._transaction_control.cancelled.is_set()
        self._transaction_control = None
        self.transaction_status.setText(
            "Анализ транзакции отменён. Команды транзакции не выполнялись."
            if cancelled else f"Операция не завершена; команды транзакции не выполнялись. {message}"
        )
        self._set_query_actions_enabled(True)
        self.statusBar().showMessage("Разбор/получение планов транзакции завершено без запуска команд.")

    def _cancel_transaction_planning(self):
        if self._transaction_control is not None:
            self._transaction_control.cancel()
            self.transaction_status.setText("Отмена анализа транзакции…")
            self._sync_transaction_controls()

    def _render_transaction_inventory(self):
        self._render_transaction_frequencies()
        inspection = self.transaction_inspection
        self.transaction_table.setRowCount(0)
        self.transaction_details.clear()
        if inspection is None:
            return
        plans = self.transaction_plan_report.statements if self.transaction_plan_report else ()
        self.transaction_table.setRowCount(len(inspection.statements))
        for row, item in enumerate(inspection.statements):
            plan = plans[row] if plans else None
            values = [str(item.number), item.kind.upper(), "да" if item.changes_data else "нет",
                ", ".join(item.relation_names) or "—",
                _STATUSES[plan.status] if plan else "не запрошен",
                str(plan.root.get("Node Type", "—")) if plan else "—",
                f"{plan.root['Total Cost']:,.2f}" if plan and "Total Cost" in plan.root else "—"]
            for column, value in enumerate(values):
                self.transaction_table.setItem(row, column, QTableWidgetItem(value))
        self.transaction_table.selectRow(0)
        self._show_transaction_statement()

    def _show_transaction_statement(self):
        if self.transaction_views.currentIndex() != 0:
            return
        row = self.transaction_table.currentRow()
        inspection = self.transaction_inspection
        if inspection is None or not 0 <= row < len(inspection.statements):
            return
        item = inspection.statements[row]
        lines = [f"Команда {item.number}: {item.kind.upper()}", item.sql, "", "Замечания:"]
        lines.extend("Параметры этой команды применены явно; план относится к указанным значениям."
            if warning == "Unresolved parameters require explicit values before planning" and self._transaction_binding is not None
            else _WARNINGS.get(warning, warning) for warning in item.warnings)
        if not item.warnings:
            lines.append("Специальных замечаний локального разбора нет. Это не разрешение на запуск.")
        counts = Counter(name for command in inspection.statements for name in command.relation_names)
        shared = [f"{name} ({count} команд)" for name, count in sorted(counts.items()) if count > 1]
        if shared:
            lines.extend(("", "Таблицы в нескольких командах (не рекомендация индекса):", *shared))
        if self.transaction_plan_report is not None:
            if self._transaction_binding is not None:
                lines.extend(("", "SQL со значениями для этой команды:", self._transaction_binding.inspection.statements[row].sql))
            plan = self.transaction_plan_report.statements[row]
            lines.extend(("", plan.explanation))
            if plan.plan_json:
                lines.extend(("План исходного состояния; не результат последовательного выполнения.",
                              json.dumps(plan.plan_json, ensure_ascii=False, indent=2)))
        self.transaction_details.setPlainText("\n".join(lines))

    def _clear_transaction_indexes(self):
        self._clear_transaction_select_rewrites()
        self._clear_transaction_replay()
        self._clear_transaction_write(reset_inputs=True)
        self._clear_transaction_trial()
        self.transaction_index_report = None
        self.transaction_index_table.setRowCount(0)
        self.transaction_index_summary.setText("Индексные кандидаты не рассчитаны; нужны актуальные планы.")
        self.transaction_views.setCurrentIndex(0)

    def _show_transaction_view(self, index):
        if index == 0:
            self._show_transaction_statement()
        elif index == 1:
            self._show_transaction_index()
        elif index == 2:
            self._show_transaction_trial()
        elif index == 3:
            self._show_transaction_frequencies()
        elif index == 4:
            self._show_transaction_comparison()
        elif index == 5:
            self._show_transaction_parameters()
        elif index == 6:
            self._show_transaction_holdout()
        elif index == 7:
            self._show_transaction_write()
        elif index == 8:
            self._show_transaction_replay()
        elif index == 9:
            self._show_transaction_select_rewrite()

    def _start_transaction_indexes(self):
        if self._query_operation_active or self._active_workers or self.transaction_plan_report is None:
            return
        try:
            settings = self._database_settings()
            effective = self._transaction_effective_sql()
        except ValueError as exc:
            self.transaction_status.setText(str(exc))
            return
        answer = QMessageBox.question(self, "Индексные кандидаты транзакции",
            f"База: {settings.host}:{settings.port}/{settings.dbname}\n"
            "Будут прочитаны только системные каталоги: столбцы, индексы и счётчики таблиц. "
            "Индексы не создаются, команды транзакции не выполняются. "
            "Результат — кандидаты для дальнейшей проверки, не доказанное ускорение.\nПродолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        from .desktop import Worker
        text = self.transaction_editor.toPlainText()
        plans = self.transaction_plan_report
        self._clear_transaction_indexes()
        control = AnalysisControl()
        worker = Worker(lambda: (text, settings, effective,
            collect_transaction_index_candidates(effective, settings, plans=plans, control=control)))
        control.progress = worker.signals.progress.emit
        self._transaction_control = control
        worker.signals.progress.connect(self.transaction_status.setText)
        worker.signals.succeeded.connect(self._transaction_indexes_ready)
        worker.signals.failed.connect(self._transaction_failed)
        self._set_query_actions_enabled(False)
        self.transaction_status.setText("Чтение метаданных и группировка кандидатов; без создания индексов…")
        self._start_worker(worker)

    def _transaction_indexes_ready(self, payload):
        if len(payload) == 3:
            text, settings, report = payload
            effective = text
        else:
            text, settings, effective, report = payload
        self._transaction_control = None
        try:
            current_settings = self._database_settings()
        except ValueError:
            current_settings = None
        if not self._transaction_snapshot_current(text, effective) or settings != current_settings:
            self.transaction_status.setText("Текст или подключение изменены. Устаревшие индексные кандидаты не показаны.")
        else:
            self._clear_transaction_trial()
            self.transaction_index_report = report
            self._render_transaction_indexes()
            self._transaction_reveal_analysis()
            self._render_transaction_write_inputs()
            self.transaction_status.setText("Индексные кандидаты сгруппированы. Индексы не создавались; ускорение и цена записи не измерялись.")
        self._set_query_actions_enabled(True)

    def _render_transaction_indexes(self):
        report = self.transaction_index_report
        self.transaction_index_table.setRowCount(0)
        if report is None:
            return
        self.transaction_index_table.setRowCount(len(report.candidates))
        for row, item in enumerate(report.candidates):
            action = item.action
            values = [f"{', '.join(action.key_columns)}\n{action.schema_name}.{action.table_name}",
                ", ".join(action.include_columns) or "—",
                ", ".join(str(use.statement_number) for use in item.read_uses) or "—",
                ", ".join(str(use.statement_number) for use in item.write_lookup_uses) or "—",
                ", ".join(str(write.statement_number) for write in item.writes) or "нет в тексте",
                self._transaction_candidate_status(item)]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                if column == 0 and item.status != "covered":
                    cell.setFlags(cell.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    cell.setCheckState(Qt.CheckState.Unchecked)
                self.transaction_index_table.setItem(row, column, cell)
        self.transaction_index_table.resizeRowsToContents()
        self._refresh_transaction_trial_statuses()
        new = sum(item.status == "candidate" for item in report.candidates)
        covered = sum(item.status == "covered" for item in report.candidates)
        read_candidates = sum(item.status != "covered" and bool(item.read_uses) for item in report.candidates)
        self.transaction_index_summary.setText(
            f"К проверке: {new}; уже покрыты: {covered}; всего вариантов: {len(report.candidates)}. "
            f"С применением в SELECT: {read_candidates}. "
            + ("Достигнут лимит 128 вариантов. " if report.truncated else "")
            + "Это не рейтинг доказанного ускорения.")
        self.transaction_views.setCurrentIndex(1)
        if report.candidates:
            self.transaction_index_table.selectRow(0)
        self._show_transaction_index()

    def _show_transaction_index(self):
        if self.transaction_views.currentIndex() != 1:
            return
        report = self.transaction_index_report
        if report is None:
            self.transaction_details.setPlainText("Индексные кандидаты не рассчитаны.")
            return
        row = self.transaction_index_table.currentRow()
        if not 0 <= row < len(report.candidates):
            self.transaction_details.setPlainText("Кандидатов нет.\n\n" + "\n\n".join(report.notes))
            return
        item = report.candidates[row]
        trial = self._transaction_index_trials.get(item.action)
        from .desktop import _format_index_action
        lines = ["КАНДИДАТ — РЕЗУЛЬТАТ ПРОБЫ SELECT НА КОПИИ" if trial else
            "СТРУКТУРНЫЙ КАНДИДАТ — НЕ ПРОВЕРЕН УСКОРЕНИЕМ", _format_index_action(item.action),
            "", "Структурный статус: " + _INDEX_STATUSES[item.status],
            "Текущий статус: " + self._transaction_candidate_status(item),
            "Самостоятельные SELECT №: " + (", ".join(str(use.statement_number) for use in item.read_uses) or "нет"),
            "Поиск внутри команд записи/DML №: " + (", ".join(str(use.statement_number) for use in item.write_lookup_uses) or "нет")]
        if not item.read_uses:
            lines.append("Проба SELECT недоступна: поиск в UPDATE/DELETE и изменяющих CTE не измеряется. "
                "Это не означает, что индекс бесполезен для записи.")
        if trial is not None:
            lines.extend(("", f"Проба на копии {trial.copy_database}: {self._transaction_trial_status(trial)}.",
                "Польза и цена записи не измерялись. Вывод относится только к измеренным SELECT, не всей транзакции.",
                "Откат подтверждён." if trial.rollback_verified else "ВНИМАНИЕ: ОТКАТ НЕ ПОДТВЕРЖДЁН."))
            if trial.readings:
                lines.append(f"Сумма медиан SELECT: {trial.baseline_sum_ms:.3f} → {trial.indexed_sum_ms:.3f} мс.")
            lines.extend(trial.reasons)
        if item.covered_by:
            lines.append("Подходящие существующие индексы: " + ", ".join(item.covered_by) + ". Новый дубликат не предлагается.")
        elif item.key_covered_by:
            lines.append("Ключи уже покрыты: " + ", ".join(item.key_covered_by)
                + ". Только ради INCLUDE новый индекс может оказаться неоправданным.")
        lines.extend(("", "Потенциальные применения (не подтверждение использования):"))
        for use in item.uses:
            kind = use.statement_kind.upper() + (" / команда с записью" if use.changes_data else " / самостоятельное чтение")
            lines.append(f"Команда {use.statement_number} ({kind}): {use.reason}")
            if use.observed_scan_nodes:
                lines.append("Узлы исходного плана: " + ", ".join(use.observed_scan_nodes))
        lines.extend(("", "Операции записи в эту таблицу:"))
        if not item.writes:
            lines.append("В данном тексте не найдены. Это не означает отсутствия записей в рабочей нагрузке.")
        for write in item.writes:
            lines.append(f"Команда {write.statement_number} ({write.kind.upper()}): {write.explanation}")
        relation = item.relation
        lines.extend(("", f"Таблица: оценочно {relation.estimated_rows:,.0f} строк; размер отношения {relation.size_bytes / 1024 / 1024:.2f} МиБ (для секционированных таблиц не сумма секций).",
            f"Счётчики таблицы после сброса статистики: INSERT={relation.inserts}, UPDATE={relation.updates}, DELETE={relation.deletes}, HOT UPDATE={relation.hot_updates}.",
            "Это не частоты и не число операций/строк данной транзакции. " + (
                f"Размер пробного индекса на копии: {trial.index_size_bytes / 1024 / 1024:.2f} МиБ."
                if trial is not None and trial.index_size_bytes else "Размер нового индекса не измерен."),
            "", *report.notes))
        self.transaction_details.setPlainText("\n".join(lines))
