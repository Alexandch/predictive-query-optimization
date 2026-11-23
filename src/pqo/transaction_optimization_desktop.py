"""One desktop result for index-only, rewrite-only and combined transaction trials."""

from dataclasses import asdict
import json
from statistics import median

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .analysis_control import AnalysisControl
from .transaction_mixed_recommendation import MixedReplayPolicy
from .transaction_optimization import (
    TITLES,
    optimization_request_digest,
    run_transaction_optimization,
)
from .transaction_workspace import collapsible_notice


class TransactionOptimizationTabMixin:
    def _build_transaction_optimization_controls(self):
        self.transaction_optimization_report = None
        self._transaction_optimization_active = False
        self.transaction_optimization_page = QScrollArea()
        self.transaction_optimization_page.setWidgetResizable(True)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.transaction_optimization_page.setWidget(content)
        note = QLabel(
            "Сравните исходную транзакцию, выбранные индексы, замену SELECT и оба изменения вместе. "
            "Во всех вариантах сохраняются порядок записи и режимы транзакции."
        )
        note.setWordWrap(True)
        body.addWidget(note)
        self.transaction_optimization_context = QLabel()
        self.transaction_optimization_context.setWordWrap(True)
        body.addWidget(self.transaction_optimization_context)
        links = QHBoxLayout()
        for caption, index in (
            ("Выбрать индексы", 1),
            ("Выбрать или изменить SELECT", 9),
        ):
            button = QPushButton(caption)
            button.clicked.connect(
                lambda checked=False, i=index: self.transaction_views.setCurrentIndex(i)
            )
            links.addWidget(button)
        links.addStretch()
        body.addLayout(links)
        self.transaction_optimization_options_toggle = QToolButton()
        self.transaction_optimization_options_toggle.setText("Параметры сравнения")
        self.transaction_optimization_options_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.transaction_optimization_options_toggle.setCheckable(True)
        self.transaction_optimization_options = QWidget()
        options_body = QVBoxLayout(self.transaction_optimization_options)
        options_body.setContentsMargins(0, 0, 0, 0)
        self.transaction_optimization_options_toggle.toggled.connect(
            self.transaction_optimization_options.setVisible
        )
        self.transaction_optimization_options_toggle.toggled.connect(
            lambda shown: self.transaction_optimization_options_toggle.setArrowType(
                Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow
            )
        )
        body.addWidget(self.transaction_optimization_options_toggle)
        body.addWidget(self.transaction_optimization_options)
        self.transaction_optimization_options_toggle.setChecked(True)
        reference = QHBoxLayout()
        reference.addWidget(QLabel("Эталон стенда:"))
        self.transaction_optimization_reference = QLineEdit()
        reference.addWidget(self.transaction_optimization_reference, 1)
        options_body.addLayout(reference)
        row = QHBoxLayout()
        row.addWidget(QLabel("Серий:"))
        self.transaction_optimization_series = QSpinBox()
        self.transaction_optimization_series.setRange(1, 3)
        self.transaction_optimization_series.setValue(2)
        row.addWidget(self.transaction_optimization_series)
        row.addWidget(QLabel("Общий выигрыш ≥"))
        self.transaction_optimization_gain_ms = QDoubleSpinBox()
        self.transaction_optimization_gain_ms.setRange(0, 1_000_000)
        self.transaction_optimization_gain_ms.setValue(5)
        self.transaction_optimization_gain_ms.setSuffix(" мс")
        row.addWidget(self.transaction_optimization_gain_ms)
        row.addWidget(QLabel("и"))
        self.transaction_optimization_gain_percent = QDoubleSpinBox()
        self.transaction_optimization_gain_percent.setRange(0, 100)
        self.transaction_optimization_gain_percent.setValue(5)
        self.transaction_optimization_gain_percent.setSuffix(" %")
        row.addWidget(self.transaction_optimization_gain_percent)
        row.addStretch()
        options_body.addLayout(row)
        options_body.addWidget(
            collapsible_notice(
                "Как формируется итог",
                "Отметьте 1–2 индекса и подготовьте одну замену SELECT. Используется один закрытый снимок: "
                "новая копия на каждый прогон, исходные прогоны чередуются с вариантами. "
                "При двух сериях создаётся 15 одноразовых БД последовательно; общий бюджет 10 минут, CREATE/DROP до 30 секунд. "
                "Сверяются все результаты SELECT/RETURNING, итоговые строки, последовательности и режимы транзакции. "
                "Минимум две полные стабильные серии, отсутствие существенной регрессии любой команды и бюджет индексов до 64 МиБ. "
                "Близкие результаты дают несколько допустимых вариантов без единственного победителя. "
                "Время построения индексов показано отдельно и не входит во время исполнения транзакции. "
                "Выигрыш считается относительно соседних исходных прогонов; первая строка показывает медиану всех исходных прогонов. "
                "Действуют ограничения выполнения на одноразовых копиях; автоматического применения к источнику нет.",
            )
        )
        self.transaction_optimization_confirm = QCheckBox(
            "Разрешаю выполнение транзакции и создание/удаление одноразовых копий тестового эталона"
        )
        options_body.addWidget(self.transaction_optimization_confirm)
        actions = QHBoxLayout()
        self.transaction_optimization_button = QPushButton("Сравнить четыре варианта")
        self.transaction_optimization_button.setObjectName("primaryButton")
        self.transaction_optimization_export = QPushButton("Сохранить результат JSON")
        actions.addWidget(self.transaction_optimization_button, 1)
        actions.addWidget(self.transaction_optimization_export)
        body.addLayout(actions)
        self.transaction_optimization_status = QLabel(
            "Подготовьте замену SELECT и отметьте индексы в списке кандидатов."
        )
        self.transaction_optimization_status.setWordWrap(True)
        self.transaction_optimization_status.setTextFormat(Qt.TextFormat.PlainText)
        body.addWidget(self.transaction_optimization_status)
        self.transaction_optimization_details_check = QCheckBox(
            "Показать чтение, запись и стоимость индексов"
        )
        body.addWidget(self.transaction_optimization_details_check)
        self.transaction_optimization_table = QTableWidget(0, 8)
        self.transaction_optimization_table.setHorizontalHeaderLabels(
            [
                "Вариант",
                "Транзакция, мс",
                "SELECT, мс",
                "Запись, мс",
                "Выигрыш, %",
                "Индексы, МиБ",
                "Построение, мс",
                "Вывод",
            ]
        )
        header = self.transaction_optimization_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.transaction_optimization_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        header.sectionResized.connect(self._fit_transaction_optimization_table)
        self.transaction_optimization_details_check.toggled.connect(
            self._toggle_transaction_optimization_details
        )
        self._toggle_transaction_optimization_details(False)
        body.addWidget(self.transaction_optimization_table, 1)
        body.addStretch(1)
        self.transaction_optimization_button.clicked.connect(
            self._start_transaction_optimization
        )
        self.transaction_optimization_export.clicked.connect(
            self._export_transaction_optimization
        )
        self.transaction_index_table.itemChanged.connect(
            self._clear_transaction_optimization
        )
        for signal in (
            self.transaction_optimization_reference.textChanged,
            self.transaction_optimization_series.valueChanged,
            self.transaction_optimization_gain_ms.valueChanged,
            self.transaction_optimization_gain_percent.valueChanged,
            self.transaction_optimization_confirm.toggled,
        ):
            signal.connect(self._clear_transaction_optimization)
        open_button = QPushButton("Итоговая проверка: индексы и SQL вместе")
        open_button.clicked.connect(lambda: self.transaction_views.setCurrentIndex(10))
        self.transaction_select_rewrite_page.widget().layout().insertWidget(
            0, open_button
        )

    def _optimization_policy(self):
        return MixedReplayPolicy(
            minimum_gain_ms=self.transaction_optimization_gain_ms.value(),
            minimum_gain_ratio=self.transaction_optimization_gain_percent.value() / 100,
        )

    def _optimization_snapshot(self):
        snapshot, prepared = self._select_rewrite_prepared()
        return (
            snapshot,
            prepared.number,
            prepared.variant_sql,
            self._replay_actions(),
            self.transaction_optimization_reference.text().strip(),
            self.transaction_optimization_series.value(),
            self._optimization_policy(),
        )

    def _clear_transaction_optimization(self, *_):
        if not hasattr(self, "transaction_optimization_table"):
            return
        had_result=self.transaction_optimization_report is not None
        self.transaction_optimization_report = None
        self.transaction_optimization_options_toggle.setChecked(True)
        self.transaction_optimization_table.setRowCount(0)
        self.transaction_optimization_status.setText(
            "Настройки или входные варианты изменены; требуется новое сравнение."
        )
        if had_result:
            message='Результат общего сравнения сброшен; требуется новая проверка.'
            self.transaction_status.setText(message)
            self.statusBar().showMessage(message)
        if (
            hasattr(self, "transaction_views")
            and self.transaction_views.currentIndex() == 10
        ):
            self.transaction_details.setPlainText(
                "Нет актуального общего результата оптимизации."
            )
        self._sync_transaction_optimization_controls()

    def _sync_transaction_optimization_controls(self):
        if not hasattr(self, "transaction_optimization_table"):
            return
        idle = not self._active_workers and not self._query_operation_active
        count = len(self._replay_actions())
        ready = self._select_rewrite_current() and bool(
            self._transaction_select_rewrite_options
        )
        rewrite_label = 'не подготовлена'
        if ready:
            index = self.transaction_select_rewrite_choice.currentIndex()
            if index >= 0:
                number = self._transaction_select_rewrite_options[index].number
                rewrite_label = 'вся транзакция' if number is None else f'SELECT № {number}'
        self.transaction_optimization_context.setText(
            f"Отмечено индексов: {count}. Вариант SQL: {rewrite_label}"
        )
        for field in (
            self.transaction_optimization_reference,
            self.transaction_optimization_series,
            self.transaction_optimization_gain_ms,
            self.transaction_optimization_gain_percent,
            self.transaction_optimization_confirm,
        ):
            field.setEnabled(idle)
        self.transaction_optimization_button.setEnabled(
            idle
            and ready
            and 1 <= count <= 2
            and self.transaction_optimization_confirm.isChecked()
            and bool(self.transaction_optimization_reference.text().strip())
            and bool(self.transaction_select_rewrite_variant.toPlainText().strip())
        )
        self.transaction_optimization_export.setEnabled(
            idle and self.transaction_optimization_report is not None
        )

    def _start_transaction_optimization(self):
        if not self._user_action_ready(database=False):
            return
        try:
            if not self.transaction_optimization_confirm.isChecked():
                raise ValueError(
                    "Подтвердите выполнение на одноразовых копиях эталона."
                )
            current = self._optimization_snapshot()
            snapshot, number, variant, actions, reference, series, policy = current
            if not 1 <= len(actions) <= 2:
                raise ValueError("Отметьте 1–2 индекса в списке кандидатов.")
            from .transaction_replay import validate_reference

            validate_reference(snapshot[2], reference, True, True)
        except ValueError as exc:
            self.transaction_optimization_status.setText(str(exc))
            return
        if (
            QMessageBox.question(
                self,
                "Сравнение всей транзакции на тестовых копиях",
                f"Сервер: {snapshot[2].host}:{snapshot[2].port}\nЭталон: {reference}\n"
                f"Четыре варианта; до {1 + 7 * series} новых одноразовых БД. "
                f"Общий выигрыш ≥ {policy.minimum_gain_ms:g} мс и {policy.minimum_gain_ratio:.0%}. "
                "Транзакция с записью будет выполнена на свежих копиях. Продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        from .desktop import Worker, ARTIFACT_ROOT

        self._clear_transaction_optimization()
        control = AnalysisControl()
        self._transaction_control = control
        self._transaction_optimization_active = True
        worker = Worker(
            lambda: (
                current,
                run_transaction_optimization(
                    snapshot[1],
                    snapshot[2],
                    reference,
                    number,
                    variant,
                    actions,
                    series=series,
                    policy=policy,
                    control=control,
                    test_copy_confirmed=True,
                    disposable_databases_confirmed=True,
                    recovery_directory=ARTIFACT_ROOT / "disposable-recovery",
                ),
            )
        )
        control.progress = worker.signals.progress.emit
        worker.signals.progress.connect(self.transaction_status.setText)
        worker.signals.succeeded.connect(self._transaction_optimization_ready)
        worker.signals.failed.connect(self._transaction_optimization_failed)
        self.transaction_optimization_status.setText(
            "Сравнение выполняется; дождитесь итогового вывода и очистки копий."
        )
        self._set_query_actions_enabled(False)
        self._start_worker(worker)

    def _transaction_optimization_ready(self, payload):
        current, report = payload
        self._transaction_control = None
        self._transaction_optimization_active = False
        try:
            snapshot, number, variant, actions, reference, series, policy = (
                self._optimization_snapshot()
            )
            valid = (
                current == self._optimization_snapshot()
                and report.request_digest
                == optimization_request_digest(
                    snapshot[1],
                    snapshot[2],
                    reference,
                    number,
                    variant,
                    actions,
                    series,
                    policy,
                )
                and report.policy == policy
                and (
                    report.decision not in {"recommended", "multiple"}
                    or report.status == "completed"
                    and report.comparable
                    and report.cleanup_verified
                )
            )
        except ValueError:
            valid = False
        if valid:
            self.transaction_optimization_report = report
            if report.status != "completed":
                message = "Сравнение не завершено. " + " ".join(report.reasons)
            else:
                message = {
                    "recommended": "Лучший подтверждённый вариант: "
                    + TITLES.get(report.best_variant, ""),
                    "multiple": "Есть полезные варианты; единственный лучший по замерам не выделен.",
                    "not_recommended": "Изменения не дали достаточного общего эффекта.",
                    "inconclusive": "Недостаточно надёжных данных для общей рекомендации.",
                }[report.decision]
            message = (
                message.rstrip(". ")
                + ". Удаление копий "
                + (
                    "подтверждено."
                    if report.cleanup_verified
                    else "НЕ подтверждено: " + ", ".join(report.leftover_databases)
                )
            )
            self.transaction_optimization_status.setText(message)
            self.transaction_status.setText(message)
            self._render_transaction_optimization()
            self.transaction_optimization_options_toggle.setChecked(False)
            self.transaction_views.setCurrentIndex(10)
            self._show_transaction_optimization()
        else:
            self.transaction_status.setText(
                "Устаревшее сравнение не присоединено. Удаление копий "
                + (
                    "подтверждено."
                    if report.cleanup_verified
                    else "НЕ подтверждено: " + ", ".join(report.leftover_databases)
                )
            )
        self.statusBar().showMessage(self.transaction_status.text())
        self._set_query_actions_enabled(True)

    def _transaction_optimization_failed(self, message):
        self._transaction_control = None
        self._transaction_optimization_active = False
        self.transaction_optimization_status.setText(
            "Сравнение не завершено. Очистка копий не подтверждена. " + message
        )
        self.transaction_status.setText(self.transaction_optimization_status.text())
        self.statusBar().showMessage(self.transaction_status.text())
        self._set_query_actions_enabled(True)

    def _render_transaction_optimization(self):
        report = self.transaction_optimization_report
        self.transaction_optimization_table.setRowCount(4)
        variants = {v.kind: v.measurement for v in report.variants}
        for row, kind in enumerate(("original", "indexes", "rewrite", "combined")):
            samples = (
                [x.sample for x in report.observations if x.variant == kind]
                if report.status == "completed"
                else []
            )
            values = [TITLES[kind], *(["—"] * 6), "Нет полных замеров"]
            reason = ""
            if samples:
                values[1:4] = [
                    f"{median(items):.2f}"
                    for items in (
                        [s.transaction_client_ms for s in samples],
                        [
                            sum(c.client_ms for c in s.commands if c.kind == "select")
                            for s in samples
                        ],
                        [
                            sum(
                                c.client_ms
                                for c in s.commands
                                if c.kind in {"insert", "update", "delete"}
                            )
                            for s in samples
                        ],
                    )
                ]
                if kind == "original":
                    values[7] = "База сравнения"
                else:
                    measured = variants[kind]
                    decision = measured.recommendation
                    if decision.summary:
                        values[4] = f"{decision.summary[0].gain_ratio * 100:+.1f}"
                    values[5] = (
                        f"{max(max(s.index_size_bytes, s.index_final_size_bytes) for s in samples) / 1024**2:.2f}"
                    )
                    values[6] = f"{median(s.index_build_ms for s in samples):.1f}"
                    values[7] = (
                        "Результаты различаются"
                        if not measured.results_equal
                        else "Итоговые данные различаются"
                        if not measured.final_states_equal
                        else {
                            "recommended": "Подтверждён",
                            "not_recommended": "Недостаточный эффект",
                            "inconclusive": "Недостаточно данных",
                        }[decision.status]
                    )
                    if kind == report.best_variant:
                        values[7] = "Лучший подтверждённый"
                    elif decision.recommended and kind not in report.eligible_variants:
                        values[7] = "Исключён из выбора"
                    reason = "\n".join(decision.reasons)
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                cell.setToolTip(reason if column == 7 else value)
                self.transaction_optimization_table.setItem(row, column, cell)
        self._fit_transaction_optimization_table()

    def _toggle_transaction_optimization_details(self, shown):
        for column in (2, 3, 5, 6):
            self.transaction_optimization_table.setColumnHidden(column, not shown)
        self._fit_transaction_optimization_table()

    def _fit_transaction_optimization_table(self, *_):
        if getattr(self, "_optimization_fitting", False):
            return
        self._optimization_fitting = True
        try:
            table = self.transaction_optimization_table
            table.resizeRowsToContents()
            height = (
                table.horizontalHeader().height()
                + sum(table.rowHeight(i) for i in range(table.rowCount()))
                + 8
            )
            if self.transaction_optimization_details_check.isChecked():
                height += table.horizontalScrollBar().sizeHint().height()
            table.setMinimumHeight(max(190, height))
            table.setMaximumHeight(max(190, height))
        finally:
            self._optimization_fitting = False

    def _show_transaction_optimization(self):
        report = self.transaction_optimization_report
        self.transaction_details.setPlainText(
            json.dumps(asdict(report), ensure_ascii=False, indent=2)
            if report
            else "Подготовьте SELECT и отметьте индексы, затем запустите сравнение четырёх вариантов."
        )

    def _export_transaction_optimization(self):
        if self.transaction_optimization_report is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить общий результат",
            "transaction-optimization.json",
            "JSON (*.json)",
        )
        if path:
            from pathlib import Path

            try:
                Path(path).write_text(
                    json.dumps(
                        asdict(self.transaction_optimization_report),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError as exc:
                QMessageBox.warning(self, "Файл не сохранён", str(exc))
