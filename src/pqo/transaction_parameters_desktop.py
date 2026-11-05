"""In-memory explicit positional parameter values, no editor replacement or SQL execution."""
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .transaction_parameters import (
    PARAMETER_TYPES, ParameterValue, bind_transaction_parameters, discover_transaction_parameters,
)


class TransactionParameterTabMixin:
    def _build_transaction_parameter_controls(self):
        self._transaction_binding = None
        self._transaction_parameter_slots = ()
        self._transaction_parameter_problem = None
        self.transaction_parameter_page = QWidget()
        layout = QVBoxLayout(self.transaction_parameter_page)
        note = QLabel("$1, $2, … задаются отдельно для каждой команды. Значения хранятся только в памяти. "
            "Пустое поле text — пустая строка; NULL отмечается явно. Подстановка не запускает SQL и не меняет исходный редактор.")
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.transaction_parameter_button = QPushButton("Применить параметры локально")
        self.transaction_parameter_status = QLabel("После локального разбора здесь появятся параметры.")
        self.transaction_parameter_status.setWordWrap(True)
        row.addWidget(self.transaction_parameter_button)
        row.addWidget(self.transaction_parameter_status, 1)
        layout.addLayout(row)
        self.transaction_parameter_table = QTableWidget(0, 5)
        self.transaction_parameter_table.setHorizontalHeaderLabels(["Команда №", "Параметр", "Тип PostgreSQL", "NULL", "Значение"])
        self.transaction_parameter_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.transaction_parameter_table.verticalHeader().setVisible(False)
        self.transaction_parameter_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.transaction_parameter_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.transaction_parameter_table)
        self.transaction_parameter_button.clicked.connect(self._apply_transaction_parameters)

    def _clear_transaction_parameters(self):
        self._transaction_binding = None
        self._transaction_parameter_slots = ()
        self._transaction_parameter_problem = None
        self.transaction_parameter_table.setRowCount(0)
        self.transaction_parameter_status.setText("Выполните локальный разбор для заполнения параметров.")

    def _render_transaction_parameter_inputs(self):
        self._clear_transaction_parameters()
        try:
            slots = discover_transaction_parameters(self.transaction_editor.toPlainText())
        except ValueError as exc:
            self._transaction_parameter_problem = str(exc)
            self.transaction_parameter_status.setText(str(exc))
            return
        self._transaction_parameter_slots = slots
        self.transaction_parameter_table.setRowCount(len(slots))
        for row, slot in enumerate(slots):
            self.transaction_parameter_table.setItem(row, 0, QTableWidgetItem(str(slot.statement_number)))
            self.transaction_parameter_table.setItem(row, 1, QTableWidgetItem(f"${slot.position}"))
            kind = QComboBox()
            kind.addItem("Выберите тип", None)
            for type_name in PARAMETER_TYPES:
                kind.addItem(type_name, type_name)
            kind.currentIndexChanged.connect(self._transaction_parameters_changed)
            self.transaction_parameter_table.setCellWidget(row, 2, kind)
            null = QCheckBox()
            null.setProperty("parameter_row", row)
            null.toggled.connect(self._transaction_parameter_null_changed)
            self.transaction_parameter_table.setCellWidget(row, 3, null)
            value = QLineEdit()
            value.setMaxLength(4000)
            value.setPlaceholderText("Значение без SQL-кавычек; для text пустое поле = пустая строка")
            value.textChanged.connect(self._transaction_parameters_changed)
            self.transaction_parameter_table.setCellWidget(row, 4, value)
        self.transaction_parameter_status.setText(
            f"Параметров: {len(slots)}. Задайте типы/значения и примените локально." if slots else "Позиционных параметров нет.")

    def _transaction_parameter_null_changed(self, checked):
        sender = self.sender()
        if sender is None:
            return
        row = sender.property("parameter_row")
        value = self.transaction_parameter_table.cellWidget(row, 4)
        value.blockSignals(True)
        if checked:
            value.clear()
        value.setEnabled(not checked)
        value.blockSignals(False)
        self._transaction_parameters_changed()

    def _transaction_parameters_changed(self, *_):
        self._transaction_binding = None
        view = self.transaction_views.currentIndex()
        had_plans = self.transaction_plan_report is not None
        self.transaction_plan_report = None
        self._clear_transaction_indexes()
        if had_plans:
            self._render_transaction_inventory()
        self.transaction_views.setCurrentIndex(view)
        self.transaction_parameter_status.setText("Типы/значения изменены. Примените параметры заново; прежние планы и пробы сброшены.")
        self.transaction_status.setText("Параметры изменены. Прежние планы/кандидаты/пробы сброшены; примените новые значения локально.")
        if view == 5:
            self._show_transaction_parameters()
        self._sync_transaction_controls()

    def _apply_transaction_parameters(self):
        if self._query_operation_active or self._active_workers:
            return
        try:
            values = []
            for row, slot in enumerate(self._transaction_parameter_slots):
                type_name = self.transaction_parameter_table.cellWidget(row, 2).currentData()
                if type_name is None:
                    raise ValueError(f"Команда {slot.statement_number}, ${slot.position}: выберите тип PostgreSQL.")
                values.append(ParameterValue(slot.statement_number, slot.position, type_name,
                    self.transaction_parameter_table.cellWidget(row, 4).text(),
                    self.transaction_parameter_table.cellWidget(row, 3).isChecked()))
            bound = bind_transaction_parameters(self.transaction_editor.toPlainText(), values)
        except ValueError as exc:
            self.transaction_parameter_status.setText(str(exc))
            return
        self._transaction_binding = bound
        self.transaction_parameter_status.setText(
            f"Применено локально: {len(bound.parameters)} параметров. SQL не запускался; это сценарий указанных значений, не production generic-plan.")
        self.transaction_status.setText("Параметры применены локально; SQL не запускался. Теперь можно получить оценочные планы для этих значений.")
        self.transaction_views.setCurrentIndex(5)
        self._show_transaction_parameters()
        self._sync_transaction_controls()

    def _transaction_effective_sql(self):
        text = self.transaction_editor.toPlainText()
        if self._transaction_parameter_problem:
            raise ValueError(self._transaction_parameter_problem)
        if self._transaction_parameter_slots:
            if self._transaction_binding is None or self._transaction_binding.source_sql != text:
                raise ValueError("Задайте типы/значения во вкладке «Параметры SQL» и примените их локально.")
            return self._transaction_binding.sql_text
        return text

    def _transaction_snapshot_current(self, source_text, effective_sql):
        try:
            return source_text == self.transaction_editor.toPlainText() and effective_sql == self._transaction_effective_sql()
        except ValueError:
            return False

    def _sync_transaction_parameter_controls(self):
        if not hasattr(self, "transaction_parameter_table"):
            return
        idle = not self._query_operation_active and not self._active_workers
        self.transaction_parameter_table.setEnabled(idle)
        self.transaction_parameter_button.setEnabled(idle and bool(self._transaction_parameter_slots)
            and self._transaction_parameter_problem is None)
        ready = self._transaction_parameter_problem is None and (
            not self._transaction_parameter_slots or self._transaction_binding is not None)
        self.transaction_plan_button.setEnabled(idle and self.transaction_inspection is not None and ready)

    def _show_transaction_parameters(self):
        if self.transaction_views.currentIndex() != 5:
            return
        lines = ["ТИПИЗИРОВАННЫЕ ПАРАМЕТРЫ — ЛОКАЛЬНЫЙ СЦЕНАРИЙ ЗНАЧЕНИЙ",
            "$1 имеет отдельное значение в каждой команде. Исходный текст слева не меняется.",
            "Значения не сохраняются в настройки/историю и не используются для обучения. SQL здесь не запускается.",
            "Оценка/пробы относятся только к указанным значениям, не к generic-plan production PREPARE.",
            "Поддерживаются только встроенные скалярные типы. Имена таблиц/столбцов, массивы, :name и ? не поддерживаются.", ""]
        if self._transaction_binding is not None:
            lines.extend(("SQL СО ЗНАЧЕНИЯМИ (может содержать чувствительные данные):", self._transaction_binding.sql_text))
        else:
            lines.append(self._transaction_parameter_problem or "Параметры ещё не применены. Заполните таблицу типов/значений.")
        self.transaction_details.setPlainText("\n".join(lines))
