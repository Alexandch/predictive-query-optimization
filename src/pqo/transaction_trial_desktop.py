"""Explicit test-copy experiment controls; no transaction replay or deployment."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
)

from .analysis_control import AnalysisControl
from .transaction_index_trial import LAB_DATABASE, run_transaction_index_trial
from .transaction_comparison_desktop import TransactionComparisonTabMixin


class TransactionTrialTabMixin(TransactionComparisonTabMixin):
    def _build_transaction_trial_controls(self):
        self.transaction_trial_report = None
        # Evidence is local to the current SQL, source connection and copy.
        # All three context changes clear it; actions retain separate results.
        self._transaction_index_trials = {}
        self._transaction_trial_active = False
        row = QHBoxLayout()
        row.addWidget(QLabel("Тестовая копия БД:"))
        default = LAB_DATABASE if self.database_edit.text() == "pqo_university_unseen" else ""
        self.transaction_copy_edit = QLineEdit(default)
        self.transaction_copy_edit.setPlaceholderText("Другая БД на том же сервере")
        self.transaction_copy_edit.setMaximumWidth(270)
        self.transaction_copy_check = QCheckBox("Подтверждаю: отдельная тестовая копия")
        self.transaction_trial_button = QPushButton("Проверить выбранный индекс на копии")
        row.addWidget(self.transaction_copy_edit)
        row.addWidget(self.transaction_copy_check)
        row.addWidget(self.transaction_trial_button)
        self.transaction_trial_table = QTableWidget(0, 6)
        self.transaction_trial_table.setHorizontalHeaderLabels(
            ["SELECT №", "A1, мс", "B: индекс, мс", "A2, мс", "Индекс использован", "Изменение"])
        self.transaction_trial_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.transaction_trial_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.transaction_trial_table.verticalHeader().setVisible(False)
        self.transaction_trial_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.transaction_copy_edit.textChanged.connect(self._transaction_copy_changed)
        self.transaction_copy_check.toggled.connect(self._sync_transaction_trial_controls)
        self.transaction_trial_button.clicked.connect(self._start_transaction_index_trial)
        return row

    def _selected_transaction_index(self):
        report = self.transaction_index_report
        row = self.transaction_index_table.currentRow()
        return report.candidates[row] if report is not None and 0 <= row < len(report.candidates) else None

    def _sync_transaction_trial_controls(self):
        if not hasattr(self, "transaction_trial_button"):
            return
        idle = not self._query_operation_active and not self._active_workers
        self.transaction_copy_edit.setReadOnly(not idle)
        self.transaction_copy_check.setEnabled(idle)
        candidate = self._selected_transaction_index()
        eligible = candidate is not None and bool(candidate.read_uses)
        different = bool(self.transaction_copy_edit.text().strip()) and (
            self.transaction_copy_edit.text().strip().casefold() != self.database_edit.text().strip().casefold())
        self.transaction_trial_button.setEnabled(idle and candidate is not None
            and eligible and candidate.status != "covered" and self.transaction_copy_check.isChecked() and different)
        self.transaction_trial_button.setToolTip(
            "У кандидата нет применения в самостоятельных SELECT: поиск в DML этой пробой не измеряется."
            if candidate is not None and not eligible else
            "Проверка всех допустимых SELECT с одним индексом на отдельной копии; DML не запускается.")
        self._sync_transaction_comparison_controls()

    def _transaction_index_selection_changed(self):
        candidate = self._selected_transaction_index()
        self.transaction_trial_report = self._transaction_index_trials.get(candidate.action) if candidate else None
        self._render_transaction_trial(switch_view=False)
        self._show_transaction_index()
        self._sync_transaction_trial_controls()

    def _transaction_trial_status(self, report):
        if not report.rollback_verified:
            return "откат не подтверждён"
        if report.status == "cancelled":
            return "проба отменена"
        if report.status == "error":
            return "ошибка пробы"
        if report.status != "measured":
            return "проба не выполнена"
        return "чтение: ускорение" if report.accepted_for_reads else "чтение: не подтверждено"

    def _transaction_candidate_status(self, candidate):
        report = self._transaction_index_trials.get(candidate.action)
        if report is not None:
            return self._transaction_trial_status(report)
        if candidate.status != "covered" and not candidate.read_uses:
            return "только поиск в DML"
        from .transaction_desktop import _INDEX_STATUSES
        return _INDEX_STATUSES[candidate.status]

    def _refresh_transaction_trial_statuses(self):
        report = self.transaction_index_report
        if report is None:
            return
        for row, candidate in enumerate(report.candidates):
            cell = self.transaction_index_table.item(row, 5)
            if cell is not None:
                value = self._transaction_candidate_status(candidate)
                trial = self._transaction_index_trials.get(candidate.action)
                cell.setText(value)
                cell.setToolTip(value + (f"; копия: {trial.copy_database}. Польза/цена записи не измерялись." if trial else ""))

    def _clear_transaction_trial(self):
        self.transaction_trial_report = None
        self._transaction_index_trials.clear()
        self._clear_transaction_comparison()
        self.transaction_trial_table.setRowCount(0)
        self.transaction_copy_check.setChecked(False)
        self._refresh_transaction_trial_statuses()
        self._sync_transaction_trial_controls()

    def _transaction_copy_changed(self):
        had_results = bool(self._transaction_index_trials)
        self._clear_transaction_trial()
        if had_results:
            self.transaction_status.setText("Имя копии изменено. Прежние результаты проб сброшены; подтвердите новую тестовую копию.")
        if self.transaction_views.currentIndex() == 2:
            self._show_transaction_trial()
        elif self.transaction_views.currentIndex() == 1:
            self._show_transaction_index()
        elif self.transaction_views.currentIndex() == 4:
            self._show_transaction_comparison()

    def _start_transaction_index_trial(self):
        if self._query_operation_active or self._active_workers:
            return
        candidate = self._selected_transaction_index()
        if candidate is None or candidate.status == "covered" or not self.transaction_copy_check.isChecked():
            return
        if not candidate.read_uses:
            self.transaction_status.setText(
                "У кандидата есть только поиск в DML, но нет применения в самостоятельных SELECT. "
                "Эта проба не измеряет UPDATE/DELETE; выберите кандидат с SELECT.")
            return
        try:
            settings = self._database_settings()
            effective = self._transaction_effective_sql()
            target = self.transaction_copy_edit.text().strip()
            if not target or target.casefold() == settings.dbname.casefold():
                raise ValueError("Проба не допускается на исходной БД. Укажите другое имя тестовой копии.")
        except ValueError as exc:
            self.transaction_status.setText(str(exc))
            return
        answer = QMessageBox.question(self, "Реальная проба только на копии",
            f"Исходная БД (не меняется): {settings.dbname}\n"
            f"Тестовая копия: {settings.host}:{settings.port}/{target}\n"
            "На копии будут реально выполнены все допустимые SELECT (прогрев + 3 замера для A/B/A), "
            "временно создан один индекс, затем выполнен откат. Это нагрузка, блокировки и расход места. "
            "UPDATE/INSERT/DELETE и изменяющие CTE не запускаются; время всей транзакции и цена записи не измеряются. "
            "Не используйте production или недоверенные функции/расширения.\n"
            f"Минимальный выигрыш суммы медиан SELECT: {self.minimum_gain_spin.value():.1f} мс и "
            f"{self.minimum_gain_percent_spin.value():.1f}%. Регрессия отдельного чтения проверяется отдельно.\nПродолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        from .desktop import Worker
        text = self.transaction_editor.toPlainText()
        self._transaction_index_trials.pop(candidate.action, None)
        self._refresh_transaction_trial_statuses()
        self.transaction_trial_report = None
        self.transaction_trial_table.setRowCount(0)
        self._transaction_trial_active = True
        control = AnalysisControl()
        worker = Worker(lambda: (text, settings, target, effective, run_transaction_index_trial(
            effective, settings, target, candidate.action, test_copy_confirmed=True, repetitions=3,
            minimum_gain_ms=self._transaction_trial_gain_ms,
            minimum_gain_ratio=self._transaction_trial_gain_ratio, control=control)))
        self._transaction_trial_gain_ms = self.minimum_gain_spin.value()
        self._transaction_trial_gain_ratio = self.minimum_gain_percent_spin.value() / 100
        control.progress = worker.signals.progress.emit
        self._transaction_control = control
        worker.signals.progress.connect(self.transaction_status.setText)
        worker.signals.succeeded.connect(self._transaction_index_trial_ready)
        worker.signals.failed.connect(self._transaction_index_trial_failed)
        self._set_query_actions_enabled(False)
        self.transaction_status.setText(f"Фактическая проба на {target}; SELECT выполняются, команды записи пропускаются…")
        self._start_worker(worker)

    def _transaction_index_trial_ready(self, payload):
        if len(payload) == 4:
            text, settings, target, report = payload
            effective = text
        else:
            text, settings, target, effective, report = payload
        self._transaction_trial_active = False
        self._transaction_control = None
        try:
            current = self._database_settings()
        except ValueError:
            current = None
        rollback = "отсутствие индекса подтверждено" if report.rollback_verified else "ОТКАТ НЕ ПОДТВЕРЖДЁН — проверьте копию"
        if (not self._transaction_snapshot_current(text, effective) or settings != current
                or target != self.transaction_copy_edit.text().strip()
                or len(report.actions) != 1
                or report.copy_database != target or self.transaction_index_report is None
                or not any(item.action == report.action for item in self.transaction_index_report.candidates)):
            self.transaction_status.setText(f"Параметры изменились; устаревшие замеры не показаны. Копия {target}: {rollback}.")
        else:
            self._transaction_index_trials[report.action] = report
            self._refresh_transaction_trial_statuses()
            for row, candidate in enumerate(self.transaction_index_report.candidates):
                if candidate.action == report.action:
                    self.transaction_index_table.selectRow(row)
                    break
            self.transaction_trial_report = report
            self._render_transaction_trial()
            verdict = "польза для измеренных чтений подтверждена" if report.accepted_for_reads else "оптимизация не подтверждена"
            self.transaction_status.setText(f"Копия {target}: {verdict}; {rollback}. Это не результат выполнения всей транзакции.")
        self._set_query_actions_enabled(True)

    def _transaction_index_trial_failed(self, message):
        self._transaction_trial_active = False
        self._transaction_control = None
        self.transaction_status.setText(
            "Проба не завершена. SELECT на копии могли выполняться; вывода об ускорении нет. "
            "Проверьте копию, если откат не подтверждён в результате. " + message)
        self._set_query_actions_enabled(True)

    def _render_transaction_trial(self, *, switch_view=True):
        from statistics import median
        report = self.transaction_trial_report
        self.transaction_trial_table.setRowCount(0)
        if report is None:
            if self.transaction_views.currentIndex() == 2:
                self._show_transaction_trial()
            return
        self.transaction_trial_table.setRowCount(len(report.readings))
        for row, item in enumerate(report.readings):
            gain = 100 * (item.baseline_ms - item.candidate_ms) / max(item.baseline_ms, .001)
            values = [str(item.number), f"{median(item.before_ms):.3f}", f"{item.candidate_ms:.3f}",
                f"{median(item.restored_ms):.3f}", "да" if item.used_trial_index else "нет", f"{gain:+.1f}%"]
            for column, value in enumerate(values):
                self.transaction_trial_table.setItem(row, column, QTableWidgetItem(value))
        if switch_view:
            self.transaction_views.setCurrentIndex(2)
        if self.transaction_views.currentIndex() == 2:
            self._show_transaction_trial()

    def _show_transaction_trial(self):
        report = self.transaction_trial_report
        if report is None:
            self.transaction_details.setPlainText("Фактическая проба выбранного индекса на отдельной копии ещё не выполнялась.")
            return
        from .desktop import _format_index_action
        lines = ["ФАКТИЧЕСКАЯ ПРОБА SELECT НА КОПИИ — НЕ ЗАПУСК ТРАНЗАКЦИИ",
            f"Копия: {report.copy_database}", _format_index_action(report.action),
            "Статус пробы: " + self._transaction_trial_status(report) + ". Польза и цена записи не измерялись.",
            "Структурное применение в SELECT №: " + (", ".join(map(str, report.lookup_reads)) or "не указано в результате"),
            f"Пробный индекс: {report.index_name}; замеров в фазе: {report.repetitions} плюс один прогрев.",
            f"Пороги выигрыша: {report.minimum_gain_ms:.2f} мс и {100*report.minimum_gain_ratio:.1f}%.",
            "Отказ при регрессии отдельного SELECT более 2 мс и 10%; дрейф A1/A2 более 2 мс и 20% тоже блокирует подтверждение.",
            "Польза для измеренных чтений подтверждена." if report.accepted_for_reads else "Оптимизация не подтверждена.",
            "Отсутствие пробного индекса после отката подтверждено отдельным подключением."
                if report.rollback_verified else "ВНИМАНИЕ: отсутствие индекса после отката не подтверждено.",
            f"Построение индекса: {report.build_time_ms:.1f} мс; размер: {report.index_size_bytes/1024/1024:.2f} МиБ."]
        if report.readings:
            lines.extend((f"Сумма медиан SELECT (каждый по одному разу): {report.baseline_sum_ms:.3f} → {report.indexed_sum_ms:.3f} мс.",
                "База сравнения объединяет A1 и A2. Это не время транзакции и не оценка частот production.", ""))
            for item in report.readings:
                lines.extend((f"SELECT {item.number}; индекс использован: {'да' if item.used_trial_index else 'нет'}",
                    f"A1: {item.before_ms}\nB: {item.indexed_ms}\nA2: {item.restored_ms}"))
        lines.extend(("", *report.reasons,
            "Команды записи/изменяющие CTE пропущены: " + (", ".join(map(str, report.skipped_writes)) or "нет"),
            "Предыдущие изменения данных не воспроизводились. Все SELECT измерены в исходном состоянии копии.",
            "Издержки записи, триггеры/каскады и конкурентная нагрузка не измерялись. Автоматического применения индекса нет."))
        self.transaction_details.setPlainText("\n".join(lines))
