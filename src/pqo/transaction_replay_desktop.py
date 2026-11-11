"""Explicitly confirmed real DML experiment on fresh, tracked disposable DBs."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QDoubleSpinBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QHeaderView, QVBoxLayout, QWidget)

from .analysis_control import AnalysisControl
from .transaction_select_rewrite_desktop import TransactionSelectRewriteTabMixin
from .transaction_workspace import collapsible_notice
from .transaction_replay import run_transaction_replay, validate_reference
from .transaction_replay_guard import prepare_replay
from .transaction_mixed_recommendation import MixedReplayPolicy, replay_request_digest


class TransactionReplayTabMixin(TransactionSelectRewriteTabMixin):
    def _build_transaction_replay_controls(self):
        self.transaction_replay_report = None
        self._transaction_replay_active = False
        self.transaction_replay_page = QScrollArea()
        self.transaction_replay_page.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        self.transaction_replay_page.setWidget(content)
        full_note = ('РЕАЛЬНОЕ выполнение SELECT и записи только в одноразовых БД. '
            'Каждый A1/B/A2 начинается с новой копии одного закрытого снимка эталона. '
            'Эталон должен быть отдельной лабораторной БД на доверенном тестовом сервере, без активных подключений в момент снимка. '
            'Нужны права CREATEDB и владение эталоном. Исходная БД не запускается.')
        note = QLabel('Реальное выполнение SELECT и записи — на новых A1/B/A2-копиях. Эталон — отдельная лабораторная БД; нужны CREATEDB и владение эталоном.')
        note.setToolTip(full_note)
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        row.addWidget(QLabel('Лабораторный эталон:'))
        self.transaction_replay_reference = QLineEdit()
        self.transaction_replay_reference.setPlaceholderText('Например, pqo_university_index_lab; не исходная БД')
        row.addWidget(self.transaction_replay_reference, 1)
        options = QHBoxLayout()
        options.addWidget(QLabel('Серий A1/B/A2:'))
        self.transaction_replay_series = QSpinBox()
        self.transaction_replay_series.setRange(1,3)
        self.transaction_replay_series.setValue(2)
        options.addWidget(self.transaction_replay_series)
        self.transaction_replay_series.setToolTip('1 — диагностика; 2–3 — итоговый вывод')
        thresholds = QHBoxLayout()
        thresholds.addWidget(QLabel('Общий выигрыш ≥'))
        self.transaction_replay_gain_ms = QDoubleSpinBox()
        self.transaction_replay_gain_ms.setRange(0,1_000_000)
        self.transaction_replay_gain_ms.setDecimals(1)
        self.transaction_replay_gain_ms.setValue(5.)
        self.transaction_replay_gain_ms.setSuffix(' мс')
        thresholds.addWidget(self.transaction_replay_gain_ms)
        thresholds.addWidget(QLabel('и'))
        self.transaction_replay_gain_percent = QDoubleSpinBox()
        self.transaction_replay_gain_percent.setRange(0,100)
        self.transaction_replay_gain_percent.setValue(5.)
        self.transaction_replay_gain_percent.setSuffix(' %')
        thresholds.addWidget(self.transaction_replay_gain_percent)
        thresholds.addStretch()
        options.addLayout(thresholds)
        layout.addLayout(options)
        budget = QHBoxLayout()
        budget.addWidget(QLabel('Бюджет новых индексов:'))
        self.transaction_replay_size_mib = QSpinBox()
        self.transaction_replay_size_mib.setRange(1,64)
        self.transaction_replay_size_mib.setValue(64)
        self.transaction_replay_size_mib.setSuffix(' МиБ')
        budget.addWidget(self.transaction_replay_size_mib)
        row.addLayout(budget)
        layout.addLayout(row)
        policy_note = ('Пороги фиксируются до пробы. В каждой серии нужны оба порога общего выигрыша. '
            'Регрессия SELECT/DML или отдельной команды > 2 мс И > 10% блокирует рекомендацию; '
            'дрейф A1/A2 либо между сериями > 2 мс И > 20% означает недостаточно данных. '
            'Смена порогов сбрасывает вывод и требует нового прогона.')
        self.transaction_replay_confirm = QCheckBox('Разрешаю CREATE/DROP одноразовых копий лабораторного эталона')
        self.transaction_replay_confirm.setToolTip('Подтверждаю лабораторный сервер/эталон; разрешаю создание и удаление только одноразовых pqo_replay_* текущей пробы, не исходной БД.')
        layout.addWidget(self.transaction_replay_confirm)
        limits = ('Первый режим: BEGIN … COMMIT, 2–20 простых команд; одна таблица на команду, '
            'без JOIN/CTE/функций/неявных текущих дат; INSERT VALUES с явными столбцами, UPDATE/DELETE с WHERE. '
            'До 8 обычных таблиц с PK, без DEFAULT/identity/генерации/RLS/пользовательских триггеров/каскадов/расширений. '
            'БД до 256 МиБ; до 10000 строк результата/изменения на команду. Отметьте 1–2 индексных кандидата. '
            'Вывод относится только к отмеченному набору и этой нагрузке, без автоматического применения.')
        layout.addWidget(collapsible_notice('Ограничения и правила итогового вывода', policy_note+'\n\n'+limits))
        self.transaction_replay_button = QPushButton('Выполнить A1/B/A2 на новых одноразовых копиях')
        layout.addWidget(self.transaction_replay_button)
        self.transaction_replay_status = QLabel('Эксперимент не запускался. Копии создаются только после отдельного подтверждения.')
        self.transaction_replay_status.setTextFormat(Qt.TextFormat.PlainText)
        self.transaction_replay_status.setWordWrap(True)
        layout.addWidget(self.transaction_replay_status)
        self.transaction_replay_table = QTableWidget(0,6)
        self.transaction_replay_table.setHorizontalHeaderLabels(['Серия / фаза','Клиент BEGIN–COMMIT, мс','SELECT, мс','DML, мс','Новые индексы, МиБ','Построение, мс'])
        self.transaction_replay_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.transaction_replay_table.setMinimumHeight(120)
        self.transaction_replay_effect_table = QTableWidget(0,4)
        self.transaction_replay_effect_table.setHorizontalHeaderLabels(['Метрика / команда','База, мс','Индексы, мс','Выигрыш, мс / %'])
        self.transaction_replay_effect_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.transaction_replay_effect_table.setMinimumHeight(120)
        self.transaction_replay_results = QTabWidget()
        self.transaction_replay_results.addTab(self.transaction_replay_effect_table,'Итог и команды')
        self.transaction_replay_results.addTab(self.transaction_replay_table,'Прогоны A1/B/A2')
        layout.addWidget(self.transaction_replay_results,1)
        self.transaction_replay_button.clicked.connect(self._start_transaction_replay)
        self.transaction_index_table.itemChanged.connect(self._clear_transaction_replay)
        for signal in (self.transaction_replay_reference.textChanged, self.transaction_replay_series.valueChanged,
                       self.transaction_replay_confirm.toggled,self.transaction_replay_gain_ms.valueChanged,
                       self.transaction_replay_gain_percent.valueChanged,self.transaction_replay_size_mib.valueChanged):
            signal.connect(self._clear_transaction_replay)

    def _replay_policy(self):
        return MixedReplayPolicy(minimum_gain_ms=self.transaction_replay_gain_ms.value(),
            minimum_gain_ratio=self.transaction_replay_gain_percent.value()/100,
            maximum_index_bytes=self.transaction_replay_size_mib.value()*1024**2)

    def _replay_actions(self):
        report = self.transaction_index_report
        return tuple(item.action for row, item in enumerate(report.candidates)
            if item.status != 'covered' and (cell := self.transaction_index_table.item(row,0)) is not None
            and cell.checkState() == Qt.CheckState.Checked) if report else ()

    def _clear_transaction_replay(self, *_args):
        if not hasattr(self,'transaction_replay_page'):
            return
        had_result = self.transaction_replay_report is not None
        self.transaction_replay_report = None
        self.transaction_replay_table.setRowCount(0)
        self.transaction_replay_effect_table.setRowCount(0)
        self.transaction_replay_status.setText('Результат выполнения не получен или сброшен после изменения контекста/набора.')
        if self.transaction_views.currentIndex() == 8:
            self._show_transaction_replay()
            if had_result:
                self.transaction_status.setText('Результат выполнения сброшен; требуется новая проверка.')
        self._sync_transaction_replay_controls()

    def _sync_transaction_replay_controls(self):
        if not hasattr(self,'transaction_replay_page'):
            return
        idle = not self._query_operation_active and not self._active_workers
        for widget in (self.transaction_replay_reference,self.transaction_replay_series,self.transaction_replay_confirm,
                self.transaction_replay_gain_ms,self.transaction_replay_gain_percent,self.transaction_replay_size_mib):
            widget.setEnabled(idle)
        self.transaction_replay_button.setEnabled(idle and self.transaction_replay_confirm.isChecked()
            and bool(self.transaction_replay_reference.text().strip()) and 1 <= len(self._replay_actions()) <= 2
            and self.transaction_plan_report is not None)

    def _start_transaction_replay(self):
        if self._query_operation_active or self._active_workers:
            return
        try:
            source, effective = self.transaction_editor.toPlainText(), self._transaction_effective_sql()
            settings, actions = self._database_settings(), self._replay_actions()
            reference, series = self.transaction_replay_reference.text().strip(), self.transaction_replay_series.value()
            policy = self._replay_policy()
            if not self.transaction_replay_confirm.isChecked() or not 1 <= len(actions) <= 2:
                raise ValueError('Нужно отдельное подтверждение и 1–2 отмеченных кандидата.')
            validate_reference(settings,reference,True,True)
            prepare_replay(effective,settings)
        except ValueError as exc:
            self.transaction_replay_status.setText(str(exc))
            return
        answer = QMessageBox.question(self,'Реальное выполнение на одноразовых копиях',
            f'Сервер: {settings.host}:{settings.port}\nИсходная БД (не запускается): {settings.dbname}\n'
            f'Лабораторный эталон: {reference}\nБудет создано до {1+3*series} одноразовых БД. '
            f'Общий выигрыш ≥ {policy.minimum_gain_ms:g} мс И {policy.minimum_gain_ratio*100:g}%; '
            f'бюджет индексов {policy.maximum_index_bytes/1024**2:g} МиБ. '
            'DML будет реально выполнен с COMMIT только в новых копиях. Их удаление проверяется по имени и OID; '
            'FORCE и завершение чужих подключений не используются. Бюджет 300 с, CREATE может задержать отмену до 30 с. '
            'На production-сервере не запускать. Продолжить?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        from .desktop import ARTIFACT_ROOT, Worker
        self._clear_transaction_replay()
        indices = self.transaction_index_report
        self._transaction_replay_active = True
        control = AnalysisControl()
        worker = Worker(lambda: (source,effective,settings,reference,series,actions,indices,
            run_transaction_replay(effective,settings,reference,actions,test_copy_confirmed=True,
                disposable_databases_confirmed=True,series=series,control=control,
                recovery_directory=ARTIFACT_ROOT / 'disposable-recovery',policy=policy)))
        self._transaction_control = control
        control.progress = worker.signals.progress.emit
        worker.signals.progress.connect(self.transaction_status.setText)
        worker.signals.succeeded.connect(self._transaction_replay_ready)
        worker.signals.failed.connect(self._transaction_replay_failed)
        self._set_query_actions_enabled(False)
        self.transaction_status.setText('Подготовка одноразовых БД; исходная БД не исполняется…')
        self._start_worker(worker)

    def _transaction_replay_ready(self,payload):
        source,effective,settings,reference,series,actions,indices,report = payload
        self._transaction_replay_active = False
        self._transaction_control = None
        try:
            current = self._database_settings()
        except ValueError:
            current = None
        if (not self._transaction_snapshot_current(source,effective) or settings != current
                or reference != self.transaction_replay_reference.text().strip() or series != self.transaction_replay_series.value()
                or actions != self._replay_actions() or indices is not self.transaction_index_report
                or report.request_digest != replay_request_digest(effective,settings,reference,actions,series,self._replay_policy())
                or report.policy != self._replay_policy()
                or report.recommendation is not None and (report.recommendation.request_digest != report.request_digest
                    or report.recommendation.policy != report.policy)):
            self.transaction_status.setText('Устаревший результат выполнения не присоединён. '
                + ('Удаление копий подтверждено.' if report.cleanup_verified else 'ВНИМАНИЕ: удаление копий НЕ подтверждено: '+', '.join(report.leftover_databases)))
        else:
            self.transaction_replay_report = report
            self.transaction_replay_table.setRowCount(len(report.samples))
            for row,sample in enumerate(report.samples):
                values = (f'{sample.series} / {sample.phase}', f'{sample.transaction_client_ms:.3f}',
                    f'{sum(item.client_ms for item in sample.commands if item.kind=="select"):.3f}',
                    f'{sum(item.client_ms for item in sample.commands if item.kind!="select"):.3f}',
                    f'{max(sample.index_size_bytes,sample.index_final_size_bytes)/1024**2:.3f}',f'{sample.index_build_ms:.1f}')
                for column,value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.transaction_replay_table.setItem(row,column,cell)
            recommendation = report.recommendation
            if recommendation:
                self.transaction_replay_effect_table.setRowCount(len(recommendation.summary))
                for row,metric in enumerate(recommendation.summary):
                    for column,value in enumerate((metric.label,f'{metric.baseline_ms:.3f}',f'{metric.indexed_ms:.3f}',
                            f'{metric.gain_ms:+.3f} / {metric.gain_ratio*100:+.1f}%')):
                        cell = QTableWidgetItem(value)
                        cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                        self.transaction_replay_effect_table.setItem(row,column,cell)
            verdict = {'recommended':'Набор рекомендован для измеренной смешанной нагрузки',
                'not_recommended':'Набор не рекомендован для измеренной смешанной нагрузки',
                'inconclusive':'Недостаточно надёжных данных для рекомендации'}
            status = (verdict[recommendation.status] if recommendation else 'Итоговая оценка отсутствует') + (
                f'. Корректность {"подтверждена" if report.correctness_verified else "НЕ подтверждена"}; '
                f'удаление копий {"подтверждено" if report.cleanup_verified else "НЕ подтверждено"}.')
            if recommendation:
                status += ' ' + recommendation.reasons[0]
            self.transaction_replay_status.setText(status)
            self.transaction_status.setText(status)
            self.transaction_views.setCurrentIndex(8)
            self._show_transaction_replay()
        self._set_query_actions_enabled(True)

    def _transaction_replay_failed(self,message):
        self._transaction_replay_active = False
        self._transaction_control = None
        self.transaction_replay_status.setText('Неожиданная ошибка: удаление копий НЕ считается подтверждённым. '+message)
        self.transaction_status.setText(self.transaction_replay_status.text())
        self._set_query_actions_enabled(True)

    def _show_transaction_replay(self):
        report = self.transaction_replay_report
        if report is None:
            self.transaction_details.setPlainText('Выполнение на новых копиях: результат пока не получен. Поддерживается только узкий контракт простых COMMIT-транзакций.')
            return
        from .desktop import _format_index_action
        evidence = ['ИТОГ ДЛЯ ИЗМЕРЕННОЙ СМЕШАННОЙ НАГРУЗКИ — НЕ РАЗРЕШЕНИЕ НА PRODUCTION',
            'Эталон: '+report.reference_database,
            'Журнал владения копиями (без SQL/данных): '+str(report.recovery_manifest),
            f'Исходные состояния равны: {report.initial_states_equal}; результаты команд равны: {report.results_equal}; итоговые данные равны: {report.final_states_equal}.',
            f'Удаление копий подтверждено: {report.cleanup_verified}. Остались/не проверены: {report.leftover_databases}.',
            'Клиентское время включает BEGIN/COMMIT, передачу/получение результата, его контрольный хеш и служебную настройку тайм-аутов. '
            'Клонирование, построение индексов и полные снимки таблиц исключены. SELECT/DML — отдельные execute+fetch, поэтому их сумма не равна общему времени.',
            'UTC, ISO, jit=off, parallel=0; полное чтение состояния прогревает страницы. Это не оценка холодного кэша или production-конкуренции.',
            'Никакой SQL/данные автоматически не сохранялись.']
        lines = [*evidence[:2],'Выбранный набор (не применять автоматически):',*map(_format_index_action,report.actions)]
        recommendation = report.recommendation
        if recommendation:
            policy = recommendation.policy
            size = f'{recommendation.maximum_index_bytes/1024**2:.3f} МиБ' if recommendation.maximum_index_bytes is not None else 'нет полных данных'
            build = f'{recommendation.median_build_ms:.1f} мс' if recommendation.median_build_ms is not None else 'нет полных данных'
            lines.extend(['',f'Решение: {recommendation.status}.',
                f'Выигрыш ≥ {policy.minimum_gain_ms:g} мс И {policy.minimum_gain_ratio*100:g}% в КАЖДОЙ серии.',
                f'Регрессия > {policy.regression_ms:g} мс И > {policy.regression_ratio*100:g}%; '
                f'дрейф > {policy.drift_ms:g} мс И > {policy.drift_ratio*100:g}%.',
                f'Максимум новых индексов до/после DML: {size}; '
                f'бюджет: {policy.maximum_index_bytes/1024**2:g} МиБ. Медиана построения: {build}.',
                'Построение — отдельная разовая стоимость, из времени транзакции не вычитается. '
                'Таблица эффекта — медианы серий; решение проверяет каждую серию отдельно. Плюс = ускорение, минус = замедление.',
                *recommendation.reasons,
                'Нет сравнения всех наборов, независимого mixed-контроля или доказательства использования каждого индекса. '
                'Результат не обобщается на другие транзакции/конкурентную нагрузку.'])
            for series in recommendation.series:
                lines.append(f'Серия {series.number}: общий выигрыш прошёл пороги: {series.gain_passed}.')
                lines.extend(f'{metric.label}: {metric.baseline_ms:.3f} → {metric.indexed_ms:.3f} мс; '
                    f'{metric.gain_ms:+.3f} мс / {metric.gain_ratio*100:+.1f}%.' for metric in series.metrics)
        lines.extend(['','ПРОВЕРКИ И УСЛОВИЯ:',*evidence[2:]])
        for sample in report.samples:
            lines.extend(['',f'Серия {sample.series} / {sample.phase}: {sample.database}',f'Общий клиентский прогон: {sample.transaction_client_ms:.3f} мс.'])
            lines.extend(f'{item.number} {item.kind.upper()}: {item.client_ms:.3f} мс; строк {item.row_count}; результат SHA256 {item.result_sha256 or "нет RETURNING"}.' for item in sample.commands)
            lines.extend(f'Итог {state.table}: {state.rows} строк, SHA256 {state.sha256}.' for state in sample.final_state)
        lines.extend(['',*report.reasons])
        self.transaction_details.setPlainText('\n'.join(lines))
