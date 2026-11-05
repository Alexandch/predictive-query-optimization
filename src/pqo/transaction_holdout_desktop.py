"""One frozen hypothesis and one explicit control attempt per comparison, memory only."""
from pathlib import Path

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .analysis_control import AnalysisControl
from .transaction_holdout import freeze_index_selection, prepare_holdout, validate_index_holdout
from .transaction_write_desktop import TransactionWriteTabMixin


class TransactionHoldoutTabMixin(TransactionWriteTabMixin):
    def _build_transaction_holdout_controls(self):
        self._transaction_holdout_selection = None
        self._transaction_holdout_comparison = None
        self._transaction_holdout_attempted = False
        self._transaction_holdout_active = False
        self.transaction_holdout_report = None
        self.transaction_holdout_page = QScrollArea()
        self.transaction_holdout_page.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        self.transaction_holdout_page.setWidget(content)
        note = QLabel("Сначала зафиксируйте вариант, затем загрузите новые SELECT без BEGIN/DML/$1. "
            "Одна попытка, две серии, веса 1; другой индекс не выбирается.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.transaction_holdout_freeze_button = QPushButton("Зафиксировать вариант выбранной строки сравнения")
        layout.addWidget(self.transaction_holdout_freeze_button)
        self.transaction_holdout_status = QLabel("Нужно завершённое сравнение индексов и строка с подтверждённой пробой чтений.")
        self.transaction_holdout_status.setWordWrap(True)
        layout.addWidget(self.transaction_holdout_status)
        row = QHBoxLayout()
        self.transaction_holdout_load_button = QPushButton("Открыть контрольный SQL")
        self.transaction_holdout_run_button = QPushButton("Проверить контроль на копии")
        row.addWidget(self.transaction_holdout_load_button)
        row.addWidget(self.transaction_holdout_run_button)
        layout.addLayout(row)
        self.transaction_holdout_editor = QPlainTextEdit()
        self.transaction_holdout_editor.setFont(QFont("Consolas", 10))
        self.transaction_holdout_editor.setPlaceholderText("1–20 новых SELECT; без BEGIN/DML/SET. Значения должны отличаться от подбора.")
        layout.addWidget(self.transaction_holdout_editor, 2)
        self.transaction_holdout_table = QTableWidget(0, 4)
        self.transaction_holdout_table.setHorizontalHeaderLabels(["Серия", "Σ без, мс", "Σ с индексами, мс", "Контроль чтений"])
        self.transaction_holdout_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.transaction_holdout_table.setMinimumHeight(105)
        self.transaction_holdout_table.verticalHeader().setVisible(False)
        self.transaction_holdout_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.transaction_holdout_table, 1)
        self.transaction_holdout_freeze_button.clicked.connect(self._freeze_transaction_holdout)
        self.transaction_holdout_load_button.clicked.connect(self._load_transaction_holdout)
        self.transaction_holdout_run_button.clicked.connect(self._start_transaction_holdout)
        self.transaction_holdout_editor.textChanged.connect(self._transaction_holdout_text_changed)
        self.transaction_compare_table.itemSelectionChanged.connect(self._sync_transaction_holdout_controls)

    def _clear_transaction_holdout(self):
        if not hasattr(self, "transaction_holdout_page"):
            return
        self._transaction_holdout_selection = None
        self._transaction_holdout_comparison = None
        self._transaction_holdout_attempted = False
        self.transaction_holdout_report = None
        self.transaction_holdout_editor.blockSignals(True)
        self.transaction_holdout_editor.clear()
        self.transaction_holdout_editor.blockSignals(False)
        self.transaction_holdout_table.setRowCount(0)
        self.transaction_holdout_status.setText("Подбор изменён. Фиксация, контрольный SQL и результат сброшены.")
        self._sync_transaction_holdout_controls()
        self._show_transaction_holdout()

    def _sync_transaction_holdout_controls(self, *_):
        if not hasattr(self, "transaction_holdout_page"):
            return
        idle = not self._query_operation_active and not self._active_workers
        frozen = self._transaction_holdout_selection is not None
        self.transaction_holdout_freeze_button.setVisible(not frozen)
        report = self.transaction_compare_report
        row = self.transaction_compare_table.currentRow()
        eligible = (report is not None and report.completed and 0 <= row < len(report.trials)
            and report.trials[row].status == "measured" and report.trials[row].accepted_for_reads
            and all(trial.rollback_verified for trial in report.trials))
        self.transaction_holdout_freeze_button.setEnabled(idle and not frozen and eligible)
        editable = idle and frozen and not self._transaction_holdout_attempted
        self.transaction_holdout_editor.setReadOnly(not editable)
        self.transaction_holdout_load_button.setEnabled(editable)
        confirmed = self.transaction_copy_check.isChecked() and frozen and (
            self.transaction_copy_edit.text().strip() == self._transaction_holdout_selection.copy_database)
        self.transaction_holdout_run_button.setEnabled(editable and confirmed
            and bool(self.transaction_holdout_editor.toPlainText().strip()))

    def _freeze_transaction_holdout(self):
        if self._query_operation_active or self._active_workers or self._transaction_holdout_selection is not None:
            return
        try:
            selection = freeze_index_selection(self.transaction_compare_report, self.transaction_compare_table.currentRow(),
                self._transaction_effective_sql(), self._database_settings())
        except ValueError as exc:
            self.transaction_holdout_status.setText(str(exc))
            return
        self._transaction_holdout_selection = selection
        self._transaction_holdout_comparison = self.transaction_compare_report
        self.transaction_holdout_status.setText(f"Вариант {selection.variant_number} зафиксирован: "
            + ("устойчивый выбор подбора." if selection.development_stable else "ручная гипотеза, подбор не подтвердил её устойчивость.")
            + " Теперь введите контроль. Смена гипотезы требует нового подбора и нового, ещё не изученного контроля.")
        self._sync_transaction_holdout_controls()
        self._show_transaction_holdout()

    def _load_transaction_holdout(self):
        if not self.transaction_holdout_load_button.isEnabled():
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Новые контрольные SELECT", "", "SQL (*.sql);;Все файлы (*)")
        if not filename:
            return
        try:
            path = Path(filename)
            if path.stat().st_size > 2_000_000:
                raise ValueError("Файл контроля больше 2 МБ.")
            text = path.read_text(encoding="utf-8-sig")
            if len(text) > 500_000:
                raise ValueError("Контроль длиннее 500000 символов.")
            self.transaction_holdout_editor.setPlainText(text)
        except (OSError, UnicodeError, ValueError) as exc:
            self.transaction_holdout_status.setText(str(exc))

    def _transaction_holdout_text_changed(self):
        self.transaction_holdout_report = None
        self.transaction_holdout_table.setRowCount(0)
        self._sync_transaction_holdout_controls()
        self._show_transaction_holdout()

    def _start_transaction_holdout(self):
        if self._query_operation_active or self._active_workers or self._transaction_holdout_attempted:
            return
        selection = self._transaction_holdout_selection
        if selection is None:
            return
        try:
            settings = self._database_settings()
            target = self.transaction_copy_edit.text().strip()
            source, effective = self.transaction_editor.toPlainText(), self._transaction_effective_sql()
            text = self.transaction_holdout_editor.toPlainText()
            if not self.transaction_copy_check.isChecked() or target != selection.copy_database:
                raise ValueError("Подтвердите ту же отдельную тестовую копию, что при подборе.")
            prepare_holdout(text, selection, settings)  # invalid input does not consume an attempt
        except ValueError as exc:
            self.transaction_holdout_status.setText(str(exc))
            return
        answer = QMessageBox.question(self, "Контроль зафиксированного варианта на копии",
            f"Копия: {settings.host}:{settings.port}/{target}. Вариант {selection.variant_number}, индексов {len(selection.actions)}.\n"
            "Новые SELECT реально выполнятся в двух сериях A/B/A, прогрев + 3 замера на фазу. "
            "Индексы временно создаются и откатываются; это нагрузка, блокировки и место. "
            "Общий бюджет 180 с плюс ограниченное завершение. Веса всех контрольных SELECT равны 1.\n"
            "После запуска попытка израсходована, даже при отмене/ошибке; другой вариант не подбирается. "
            "Не используйте эти запросы повторно как независимый контроль после изучения результата. "
            "DML, цена записи и время всей транзакции не измеряются. Не использовать production/недоверенные расширения.\nПродолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        from .desktop import Worker
        self._transaction_holdout_attempted = True
        self._transaction_holdout_active = True
        comparison = self._transaction_holdout_comparison
        control = AnalysisControl()
        worker = Worker(lambda: (source, effective, settings, target, comparison, selection, text,
            validate_index_holdout(selection, text, settings, target, test_copy_confirmed=True, control=control)))
        self._transaction_control = control
        control.progress = worker.signals.progress.emit
        worker.signals.progress.connect(self.transaction_status.setText)
        worker.signals.succeeded.connect(self._transaction_holdout_ready)
        worker.signals.failed.connect(self._transaction_holdout_failed)
        self._set_query_actions_enabled(False)
        self.transaction_status.setText("Контроль на копии: только зафиксированный вариант, DML не запускается…")
        self._start_worker(worker)

    def _transaction_holdout_ready(self, payload):
        source, effective, settings, target, comparison, selection, text, report = payload
        self._transaction_holdout_active = False
        self._transaction_control = None
        try:
            current = self._database_settings()
        except ValueError:
            current = None
        cleanup = all(trial.rollback_verified for trial in report.trials)
        if (not self._transaction_snapshot_current(source, effective) or settings != current
                or target != self.transaction_copy_edit.text().strip()
                or comparison is not self.transaction_compare_report
                or selection is not self._transaction_holdout_selection or report.selection != selection
                or text != self.transaction_holdout_editor.toPlainText()):
            self.transaction_status.setText("Устаревший контроль не показан. "
                + ("Откаты подтверждены/DDL не начинался." if cleanup else "ОТКАТ НЕ ПОДТВЕРЖДЁН; проверьте копию."))
        else:
            self.transaction_holdout_report = report
            for row, trial in enumerate(report.trials):
                self.transaction_holdout_table.setRowCount(row + 1)
                values = (str(row + 1), f"{trial.weighted_baseline_ms:.3f}" if trial.readings else "—",
                    f"{trial.weighted_indexed_ms:.3f}" if trial.readings else "—", self._transaction_trial_status(trial))
                for column, value in enumerate(values):
                    self.transaction_holdout_table.setItem(row, column, QTableWidgetItem(value))
            verdict = ("Польза прошла подбор и контроль." if report.validated_on_both else
                "Контроль пройден, но устойчивого подтверждения на подборе нет." if report.passed_for_reads else
                "Польза на контроле не подтверждена.")
            self.transaction_holdout_status.setText(verdict + " Попытка израсходована. "
                + ("Откаты подтверждены/DDL не начинался." if cleanup else "ОТКАТ НЕ ПОДТВЕРЖДЁН; проверьте копию."))
            self.transaction_status.setText(self.transaction_holdout_status.text())
            self.transaction_views.setCurrentIndex(6)
            self._show_transaction_holdout()
        self._set_query_actions_enabled(True)

    def _transaction_holdout_failed(self, message):
        self._transaction_holdout_active = False
        self._transaction_control = None
        self.transaction_holdout_status.setText("Контроль не завершён, попытка израсходована. SELECT/индексы на копии могли выполняться; "
            "проверьте копию, если откат не подтверждён. " + message)
        self.transaction_status.setText(self.transaction_holdout_status.text())
        self._set_query_actions_enabled(True)

    def _show_transaction_holdout(self):
        if not hasattr(self, "transaction_views") or self.transaction_views.currentIndex() != 6:
            return
        from .desktop import _format_index_action
        selection = self._transaction_holdout_selection
        lines = ["ОТДЕЛЬНЫЙ КОНТРОЛЬ SELECT — НЕ ВЫПОЛНЕНИЕ ТРАНЗАКЦИИ",
            "Сначала фиксируется гипотеза, затем открывается контрольный SQL. На контроле другие индексы не выбираются.",
            "Проверяется отсутствие точных повторов AST-текста подбора, не семантическая независимость или непересечение строк.",
            "Приложение не может доказать, что пользователь не изучал эти данные ранее. Повторное использование после подбора по результату — уже не независимый контроль."]
        if selection is not None:
            lines.extend((f"Зафиксирован вариант {selection.variant_number}; копия {selection.copy_database}.",
                "Подбор: " + ("устойчивое предпочтение." if selection.development_stable else "ручная гипотеза, устойчивость не подтверждена."),
                *map(_format_index_action, selection.actions),
                f"Пороги, зафиксированные при подборе: {selection.minimum_gain_ms:.2f} мс и {100 * selection.minimum_gain_ratio:.1f}%.",
                "Все контрольные SELECT имеют вес 1. Состав пары проверяется только вместе, минимальность состава на контроле не доказывается."))
        report = self.transaction_holdout_report
        if report is not None:
            lines.extend(("", "Контроль: " + ("пройден." if report.passed_for_reads else "не подтверждён."),
                "Подбор + контроль: " + ("пройдены для чтений." if report.validated_on_both else "общего подтверждения нет."), *report.reasons))
            for series, trial in enumerate(report.trials, 1):
                lines.extend(("", f"Серия {series}: {self._transaction_trial_status(trial)}", *trial.reasons,
                    f"Размер: {trial.index_size_bytes / 1024 / 1024:.2f} МиБ; построение {trial.build_time_ms:.1f} мс.",
                    "Откат подтверждён/DDL не начинался." if trial.rollback_verified else "ОТКАТ НЕ ПОДТВЕРЖДЁН."))
                for reading in trial.readings:
                    lines.extend((f"SELECT {reading.number}: {reading.baseline_ms:.3f} → {reading.candidate_ms:.3f} мс.",
                        f"A1: {reading.before_ms}\nB: {reading.indexed_ms}\nA2: {reading.restored_ms}"))
        self.transaction_details.setPlainText("\n".join(lines))
