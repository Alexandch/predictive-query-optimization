"""Explicit bounded SELECT-workload comparison controls, never DML replay."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
)

from .analysis_control import AnalysisControl
from .transaction_comparison import compare_transaction_indexes, index_variants
from .transaction_holdout_desktop import TransactionHoldoutTabMixin


class TransactionComparisonTabMixin(TransactionHoldoutTabMixin):
    def _build_transaction_comparison_controls(self):
        self.transaction_compare_report = None
        self._transaction_comparison_active = False
        self._transaction_comparison_actions = ()
        self.transaction_compare_button = QPushButton("Сравнить отмеченные индексы и пары на копии")
        self.transaction_compare_summary = QLabel("Отметьте 1–3 кандидата флажками в списке индексов; числа повторений SELECT по умолчанию равны 1.")
        self.transaction_compare_summary.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.transaction_compare_button)
        row.addWidget(QLabel("Серий:"))
        self.transaction_series_spin = QSpinBox()
        self.transaction_series_spin.setRange(1, 3)
        self.transaction_series_spin.setValue(2)
        self.transaction_series_spin.setKeyboardTracking(False)
        self.transaction_series_spin.setToolTip("Повторные серии A/B/A: 1 — предварительно; 2–3 — проверка устойчивости. Общий бюджет всех серий 180 с.")
        self.transaction_series_spin.valueChanged.connect(self._transaction_comparison_series_changed)
        row.addWidget(self.transaction_series_spin)
        row.addWidget(self.transaction_compare_summary, 1)
        self.transaction_frequency_table = QTableWidget(0, 3)
        self.transaction_frequency_table.setHorizontalHeaderLabels(["SELECT №", "Повторов в наборе", "SQL (без выполнения)"])
        self.transaction_compare_table = QTableWidget(0, 7)
        self.transaction_compare_table.setHorizontalHeaderLabels(
            ["Серия", "Вариант", "Индексов", "Σ без, мс", "Σ с индексами, мс", "МиБ", "Итог для чтений"])
        for column in (3, 4):
            self.transaction_compare_table.horizontalHeaderItem(column).setToolTip("Сумма медиан SELECT × заданные числа повторений; не время транзакции.")
        for table in (self.transaction_frequency_table, self.transaction_compare_table):
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.verticalHeader().setVisible(False)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.transaction_frequency_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.transaction_compare_button.clicked.connect(self._start_transaction_comparison)
        self.transaction_compare_table.itemSelectionChanged.connect(self._show_transaction_comparison)
        self.transaction_index_table.itemChanged.connect(self._transaction_comparison_selection_changed)
        return row

    def _checked_transaction_indexes(self):
        report = self.transaction_index_report
        if report is None:
            return ()
        return tuple(candidate.action for row, candidate in enumerate(report.candidates)
            if candidate.read_uses and candidate.status != "covered"
            and (cell := self.transaction_index_table.item(row, 0)) is not None
            and cell.checkState() == Qt.CheckState.Checked)

    def _transaction_frequencies(self):
        return tuple((int(self.transaction_frequency_table.item(row, 0).text()),
            self.transaction_frequency_table.cellWidget(row, 1).value())
            for row in range(self.transaction_frequency_table.rowCount()))

    def _render_transaction_frequencies(self):
        old = dict(self._transaction_frequencies())
        self.transaction_frequency_table.setRowCount(0)
        if self.transaction_inspection is None:
            return
        reads = [item for item in self.transaction_inspection.statements
            if item.kind in {"select", "union", "intersect", "except"} and not item.changes_data]
        self.transaction_frequency_table.setRowCount(len(reads))
        for row, item in enumerate(reads):
            self.transaction_frequency_table.setItem(row, 0, QTableWidgetItem(str(item.number)))
            spin = QSpinBox()
            spin.setRange(1, 10000)
            spin.setKeyboardTracking(False)
            spin.setValue(old.get(item.number, 1))
            spin.setToolTip("Число повторений в условном наборе, не измеренная частота production. SQL столько раз не запускается.")
            spin.valueChanged.connect(self._transaction_comparison_frequency_changed)
            self.transaction_frequency_table.setCellWidget(row, 1, spin)
            cell = QTableWidgetItem(" ".join(item.sql.split()))
            cell.setToolTip(item.sql)
            self.transaction_frequency_table.setItem(row, 2, cell)

    def _sync_transaction_comparison_controls(self):
        if not hasattr(self, "transaction_compare_button"):
            return
        idle = not self._query_operation_active and not self._active_workers
        actions = self._checked_transaction_indexes()
        different = bool(self.transaction_copy_edit.text().strip()) and (
            self.transaction_copy_edit.text().strip().casefold() != self.database_edit.text().strip().casefold())
        self.transaction_frequency_table.setEnabled(idle)
        self.transaction_index_table.setEnabled(idle)
        self.transaction_series_spin.setEnabled(idle)
        self.transaction_compare_button.setEnabled(idle and 1 <= len(actions) <= 3
            and self.transaction_copy_check.isChecked() and different and bool(self._transaction_frequencies()))
        variants = len(actions) * (len(actions) + 1) // 2
        self.transaction_compare_summary.setText(
            f"Отмечено {len(actions)} (1–3); вариантов в серии: {variants if len(actions) <= 3 else 'слишком много'}. "
            f"Серий: {self.transaction_series_spin.value()}, общий бюджет 180 с. Веса — во вкладке «Повторы SELECT».")
        self._sync_transaction_holdout_controls()
        self._sync_transaction_write_controls()

    def _clear_transaction_comparison(self):
        if not hasattr(self, "transaction_compare_table"):
            return
        self.transaction_compare_report = None
        self._clear_transaction_holdout()
        self._clear_transaction_write()
        self._transaction_comparison_actions = ()
        self.transaction_compare_table.setRowCount(0)
        if hasattr(self, "transaction_views") and self.transaction_views.currentIndex() == 4:
            self._show_transaction_comparison()

    def _transaction_comparison_selection_changed(self, *_):
        if (self.transaction_compare_report is not None
                and self._checked_transaction_indexes() != self._transaction_comparison_actions):
            self._clear_transaction_comparison()
        self._sync_transaction_comparison_controls()

    def _transaction_comparison_frequency_changed(self, *_):
        had_result = self.transaction_compare_report is not None
        self._clear_transaction_comparison()
        if had_result:
            self.transaction_status.setText("Числа повторений изменены; прежнее сравнение сброшено. Одиночные пробы не пересчитывались.")
        self._sync_transaction_comparison_controls()

    def _transaction_comparison_series_changed(self, *_):
        had_result = self.transaction_compare_report is not None
        self._clear_transaction_comparison()
        if had_result:
            self.transaction_status.setText("Число серий изменено; прежнее сравнение сброшено. Новые пробы ещё не выполнялись.")
        self._sync_transaction_comparison_controls()

    def _show_transaction_frequencies(self):
        self.transaction_details.setPlainText(
            "ПОВТОРЫ SELECT В УСЛОВНОМ НАБОРЕ\n"
            "Для каждого самостоятельного SELECT задайте целое число 1–10000; по умолчанию 1.\n"
            "Целевая сумма = Σ (медиана SELECT × заданное число повторений). Это не время всей транзакции, "
            "не автоматически измеренные частоты и не время конкурентной нагрузки.\n"
            "Число повторений влияет только на оценку суммы, не на число фактических прогонов SQL. "
            "В каждой фазе остаются один прогрев и три замера.\n"
            "Существенная регрессия любого SELECT блокирует подтверждение независимо от его веса. "
            "DML/изменяющие CTE не измеряются, состояние предыдущих записей не воспроизводится.")

    def _start_transaction_comparison(self):
        if self._query_operation_active or self._active_workers:
            return
        try:
            actions = self._checked_transaction_indexes()
            variants = index_variants(actions)
            settings = self._database_settings()
            effective = self._transaction_effective_sql()
            target = self.transaction_copy_edit.text().strip()
            if (not self.transaction_copy_check.isChecked() or not target
                    or target.casefold() == settings.dbname.casefold()):
                raise ValueError("Подтвердите отдельную тестовую копию с другим именем БД.")
            for row in range(self.transaction_frequency_table.rowCount()):
                self.transaction_frequency_table.cellWidget(row, 1).interpretText()
            weights = self._transaction_frequencies()
            self.transaction_series_spin.interpretText()
            series_count = self.transaction_series_spin.value()
            if not weights:
                raise ValueError("Нет самостоятельных SELECT для сравнения.")
        except ValueError as exc:
            self.transaction_status.setText(str(exc))
            return
        answer = QMessageBox.question(self, "Сравнение индексов только на копии",
            f"Источник (не меняется): {settings.host}:{settings.port}/{settings.dbname}\n"
            f"Копия: {target}. Вариантов в серии: {len(variants)}; серий: {series_count}; проб всего: {len(variants) * series_count}.\n"
            "Для каждого варианта будут реально выполнены SELECT: A/B/A, один прогрев и три замера на фазу. "
            "Индексы временно создаются и откатываются перед следующим вариантом. Это блокировки, нагрузка и место.\n"
            f"Повторы в условном наборе: {dict(weights)} (только веса, не число прогонов SQL).\n"
            f"Пороги: {self.minimum_gain_spin.value():.1f} мс и {self.minimum_gain_percent_spin.value():.1f}%. "
            "Общий бюджет всех серий 180 с плюс завершение/проверка отката. Порядок вариантов меняется между сериями. "
            "Каждая проба имеет отдельный снимок БД; дрейф исходного времени и несовпадение предпочтений серий блокируют общий выбор. "
            "Одна серия — только предварительная проверка. Повторные серии не заменяют независимый контрольный набор.\n"
            "DML, цена записи и вся транзакция не измеряются. Постоянного применения индексов нет. "
            "Не использовать production и недоверенные функции/расширения. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        from .desktop import Worker
        text = self.transaction_editor.toPlainText()
        gain_ms, gain_ratio = self.minimum_gain_spin.value(), self.minimum_gain_percent_spin.value() / 100
        self._clear_transaction_comparison()
        self._transaction_comparison_actions = actions
        self._transaction_comparison_active = True
        control = AnalysisControl()
        worker = Worker(lambda: (text, settings, target, actions, weights, effective, series_count,
            compare_transaction_indexes(effective, settings, target, actions, frequencies=weights,
                test_copy_confirmed=True, minimum_gain_ms=gain_ms, minimum_gain_ratio=gain_ratio,
                series_count=series_count, control=control)))
        control.progress = worker.signals.progress.emit
        self._transaction_control = control
        worker.signals.progress.connect(self.transaction_status.setText)
        worker.signals.succeeded.connect(self._transaction_comparison_ready)
        worker.signals.failed.connect(self._transaction_comparison_failed)
        self._set_query_actions_enabled(False)
        self.transaction_status.setText(f"Сравнение на {target}: {series_count} серий × {len(variants)} вариантов, только SELECT; DML пропускается…")
        self._start_worker(worker)

    def _transaction_comparison_ready(self, payload):
        series_count = None  # compatibility with old in-memory callbacks/tests
        if len(payload) == 6:
            text, settings, target, actions, weights, report = payload
            effective = text
        elif len(payload) == 7:
            text, settings, target, actions, weights, effective, report = payload
        else:
            text, settings, target, actions, weights, effective, series_count, report = payload
        self._transaction_comparison_active = False
        self._transaction_control = None
        try:
            current = self._database_settings()
        except ValueError:
            current = None
        cleanup = all(trial.rollback_verified for trial in report.trials)
        if (not self._transaction_snapshot_current(text, effective) or settings != current
                or target != self.transaction_copy_edit.text().strip() or report.copy_database != target
                or weights != self._transaction_frequencies() or weights != report.frequencies
                or (series_count is not None and (series_count != self.transaction_series_spin.value()
                    or series_count != report.requested_series))
                or actions != self._checked_transaction_indexes()):
            self.transaction_status.setText("Устаревшее сравнение не показано. Копия " + target + ": "
                + ("откаты подтверждены/DDL не начинался." if cleanup else "ОТКАТ НЕ ПОДТВЕРЖДЁН; проверьте копию."))
        else:
            self.transaction_compare_report = report
            self._transaction_comparison_actions = actions
            self._render_transaction_comparison()
            self.transaction_status.setText(
                f"Сравнение на {target}: {len(report.trials)}/{report.expected_variants} проб. "
                + (f"{'Устойчиво в сериях' if report.stable_for_reads else 'Предварительно'}: вариант {report.coordinate(report.preferred_variant)[1]}. " if report.preferred_variant is not None else "Однозначного предпочтения нет. ")
                + ("Откаты подтверждены/DDL не начинался." if cleanup else "ОТКАТ НЕ ПОДТВЕРЖДЁН; проверьте копию."))
        self._set_query_actions_enabled(True)

    def _transaction_comparison_failed(self, message):
        self._transaction_comparison_active = False
        self._transaction_control = None
        self.transaction_status.setText("Сравнение не завершено. SELECT/временные индексы на копии могли выполняться; "
            "предпочтения нет. Проверьте состояние копии, если откат не подтверждён. " + message)
        self._set_query_actions_enabled(True)

    def _render_transaction_comparison(self):
        report = self.transaction_compare_report
        self.transaction_compare_table.setRowCount(len(report.trials))
        exclusions = dict(report.pair_exclusions)
        for row, trial in enumerate(report.trials):
            series, variant = report.coordinate(row)
            verdict = self._transaction_trial_status(trial)
            if row in exclusions:
                verdict = "нет добавочной пользы пары"
            if report.preferred_variant is not None and variant == report.coordinate(report.preferred_variant)[1]:
                verdict += "; устойчиво в сериях" if report.stable_for_reads else "; предварительно"
            values = (str(series), str(variant), str(len(trial.actions)),
                f"{trial.weighted_baseline_ms:.3f}" if trial.readings else "—",
                f"{trial.weighted_indexed_ms:.3f}" if trial.readings else "—",
                f"{trial.index_size_bytes / 1024 / 1024:.2f}", verdict)
            for column, value in enumerate(values):
                self.transaction_compare_table.setItem(row, column, QTableWidgetItem(value))
        self.transaction_views.setCurrentIndex(4)
        if report.trials:
            self.transaction_compare_table.selectRow(report.preferred_variant if report.preferred_variant is not None else 0)
        self._show_transaction_comparison()

    def _show_transaction_comparison(self):
        if not hasattr(self, "transaction_views") or self.transaction_views.currentIndex() != 4:
            return
        report = self.transaction_compare_report
        if report is None:
            self.transaction_details.setPlainText("Сравнение отмеченных индексов и пар ещё не выполнено для текущих параметров.")
            return
        from .desktop import _format_index_action
        lines = ["СРАВНЕНИЕ SELECT НА КОПИИ — НЕ ВРЕМЯ ТРАНЗАКЦИИ", f"Копия: {report.copy_database}",
            f"Числа повторений (заданы пользователем): {dict(report.frequencies)}.",
            f"Проб: {len(report.trials)}/{report.expected_variants}; сравнение {'завершено' if report.completed else 'неполное'}. Серий завершено: {report.completed_series}/{report.requested_series}.",
            "Взвешенные суммы относятся к условному набору, не конкурентной нагрузке. DML и цена записи не измерены.",
            "Пробы имеют отдельные снимки/базовые замеры. Порядок меняется: прямой, обратный, сдвинутый. "
            "Предпочтение требует совпадения выбора каждой серии и отсутствия существенного дрейфа. Это эвристика, не статистическое доказательство.",
            "Повторные серии используют те же SELECT/параметры: это не независимый контрольный набор и не глобальный оптимум.",
            "Устойчивость: " + ("совпадение предпочтений подтверждено." if report.stable_for_reads else
                "одна серия — только предварительно." if report.requested_series == 1 else "не подтверждена."),
            "Выбор по сериям: " + (", ".join(str(value) if value is not None else "нет" for value in report.series_preferences) or "нет полных серий"),
            "Для рассмотрения: " + (f"вариант {report.coordinate(report.preferred_variant)[1]}." if report.preferred_variant is not None else "нет однозначного предпочтения."),
            *report.reasons]
        row = self.transaction_compare_table.currentRow()
        if 0 <= row < len(report.trials):
            trial = report.trials[row]
            series, variant = report.coordinate(row)
            lines.extend(("", f"СЕРИЯ {series}, ВАРИАНТ {variant}: {self._transaction_trial_status(trial)}", *map(_format_index_action, trial.actions),
                f"Пороги: {trial.minimum_gain_ms:.2f} мс и {100*trial.minimum_gain_ratio:.1f}%; замеров в фазе {trial.repetitions} + прогрев.",
                (f"Сумма медиан × повторы: {trial.weighted_baseline_ms:.3f} → {trial.weighted_indexed_ms:.3f} мс."
                    if trial.readings else "Полных замеров SELECT для этой пробы нет; нулевое время не утверждается."),
                f"Построение: {trial.build_time_ms:.1f} мс; общий размер {trial.index_size_bytes / 1024 / 1024:.2f} МиБ.",
                "Откат подтверждён/DDL не начинался." if trial.rollback_verified else "ОТКАТ НЕ ПОДТВЕРЖДЁН.",
                "Имена пробных индексов: " + ", ".join(trial.index_names),
                "Использованы в измеренных планах: " + (", ".join(trial.used_index_names) or "нет"), *trial.reasons))
            if row in dict(report.pair_exclusions):
                lines.append(dict(report.pair_exclusions)[row])
            for reading in trial.readings:
                lines.extend(("", f"SELECT {reading.number}, повторов {dict(trial.frequencies).get(reading.number, 1)}: "
                    f"{reading.baseline_ms:.3f} → {reading.candidate_ms:.3f} мс (один запуск).",
                    f"A1: {reading.before_ms}\nB: {reading.indexed_ms}\nA2: {reading.restored_ms}"))
        self.transaction_details.setPlainText("\n".join(lines))
