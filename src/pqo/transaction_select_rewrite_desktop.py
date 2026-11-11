"""Side-by-side single SELECT replacement; other transaction commands stay fixed."""
from sqlglot import parse_one

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QCheckBox,QComboBox,QDoubleSpinBox,QHBoxLayout,QLabel,QLineEdit,
    QMessageBox,QPlainTextEdit,QPushButton,QScrollArea,QSpinBox,QSplitter,QVBoxLayout,QWidget)

from .analysis_control import AnalysisControl
from .sql_diff_view import SQLDiffView
from .transaction_workspace import collapsible_notice
from .transaction_parameters_desktop import TransactionParameterTabMixin
from .transaction_mixed_recommendation import MixedReplayPolicy
from .transaction_select_rewrite import (generate_transaction_select_rewrites,prepare_select_replacement,
    run_transaction_select_rewrite,select_rewrite_request_digest,_render)


class TransactionSelectRewriteTabMixin(TransactionParameterTabMixin):
    def _build_transaction_select_rewrite_controls(self):
        self.transaction_select_rewrite_report = None
        self._transaction_select_rewrite_active = None
        self._transaction_select_rewrite_snapshot = None
        self._transaction_select_rewrite_options = ()
        self.transaction_select_rewrite_page = QScrollArea()
        self.transaction_select_rewrite_page.setWidgetResizable(True)
        content = QWidget()
        self.transaction_select_rewrite_page.setWidget(content)
        layout = QVBoxLayout(content)
        full_note = ('Замена одного SELECT без изменения остальных команд и их порядка. '
            'Подготовка вариантов локальная. Фактическая проверка выполняет всю BEGIN … COMMIT транзакцию '
            'на свежих одноразовых БД A1/B/A2; выбранный SELECT в B видит предыдущие записи. Новые индексы не создаются.')
        note = self.transaction_select_rewrite_note = QLabel('Замена одного SELECT; порядок остальных команд сохраняется. Реальные DML/COMMIT — только на одноразовых копиях.')
        note.setToolTip(full_note)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.transaction_select_rewrite_generate = QPushButton('Подготовить замены SELECT (без подключения)')
        tools = QHBoxLayout()
        tools.addWidget(self.transaction_select_rewrite_generate)
        self.transaction_select_rewrite_choice = QComboBox()
        self.transaction_select_rewrite_choice.setMinimumContentsLength(18)
        self.transaction_select_rewrite_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        tools.addWidget(self.transaction_select_rewrite_choice,1)
        self.transaction_select_rewrite_expand = QPushButton('Развернуть редакторы SQL')
        self.transaction_select_rewrite_expand.setCheckable(True)
        self.transaction_select_rewrite_expand.toggled.connect(self._expand_transaction_select_editors)
        tools.addWidget(self.transaction_select_rewrite_expand)
        layout.addLayout(tools)
        editors = self.transaction_select_rewrite_editor_splitter = QSplitter(Qt.Orientation.Horizontal)
        editors.setChildrenCollapsible(False)
        editors.setHandleWidth(8)
        for name,title,readonly in (('original','Исходный SELECT — только чтение',True),('variant','Замена SELECT — можно редактировать',False)):
            panel = QWidget(); panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(0,0,0,0)
            panel_layout.addWidget(QLabel(title))
            editor = QPlainTextEdit()
            editor.setFont(QFont('Consolas',10))
            editor.setReadOnly(readonly)
            editor.setMinimumHeight(150)
            setattr(self,'transaction_select_rewrite_'+name,editor)
            panel_layout.addWidget(editor,1)
            editors.addWidget(panel)
        editors.setSizes([300,300])
        layout.addWidget(editors,1)
        self.transaction_select_rewrite_diff = SQLDiffView(
            self.transaction_select_rewrite_original, self.transaction_select_rewrite_variant)
        self.transaction_select_rewrite_preview = QPushButton('Показать замену и порядок команд (без запуска)')
        settings = self.transaction_select_rewrite_settings = QWidget()
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(0,0,0,0)
        row = QHBoxLayout()
        row.addWidget(QLabel('Лабораторный эталон:'))
        self.transaction_select_rewrite_reference = QLineEdit()
        self.transaction_select_rewrite_reference.setPlaceholderText('Отдельная БД, например pqo_university_index_lab')
        row.addWidget(self.transaction_select_rewrite_reference,1)
        settings_layout.addLayout(row)
        options = QHBoxLayout()
        options.addWidget(QLabel('Серий A1/B/A2:'))
        self.transaction_select_rewrite_series = QSpinBox()
        self.transaction_select_rewrite_series.setRange(1,3); self.transaction_select_rewrite_series.setValue(2)
        options.addWidget(self.transaction_select_rewrite_series)
        options.addWidget(QLabel('Общий выигрыш ≥'))
        self.transaction_select_rewrite_gain_ms = QDoubleSpinBox()
        self.transaction_select_rewrite_gain_ms.setRange(0,1_000_000); self.transaction_select_rewrite_gain_ms.setValue(5.)
        self.transaction_select_rewrite_gain_ms.setSuffix(' мс')
        options.addWidget(self.transaction_select_rewrite_gain_ms)
        options.addWidget(QLabel('и'))
        self.transaction_select_rewrite_gain_percent = QDoubleSpinBox()
        self.transaction_select_rewrite_gain_percent.setRange(0,100); self.transaction_select_rewrite_gain_percent.setValue(5.)
        self.transaction_select_rewrite_gain_percent.setSuffix(' %')
        options.addWidget(self.transaction_select_rewrite_gain_percent)
        settings_layout.addLayout(options)
        limits = ('Только доверенный тестовый сервер и отдельный эталон; нужны CREATEDB и владение эталоном. '
            'Сохраняется узкий контракт простых SELECT/DML. Дополнительно: одна FROM-подвыборка под внешним агрегатом. '
            'JOIN, CTE, функции вне разрешённых агрегатов, триггеры, DEFAULT/identity, каскады и RLS исключены. '
            'Внешние ORDER BY/LIMIT замены неизменны. Совпадение данных не означает ускорение; '
            'рекомендация требует двух полных устойчивых серий. Регрессия любой команды > 2 мс И > 10% блокирует рекомендацию; '
            'дрейф > 2 мс И > 20% означает недостаточно данных. Пороги фиксируются до запуска.')
        settings_layout.addWidget(collapsible_notice('Ограничения и правила проверки',limits))
        self.transaction_select_rewrite_confirm = QCheckBox('Разрешаю реальный DML/COMMIT и CREATE/DROP новых одноразовых лабораторных копий')
        settings_layout.addWidget(self.transaction_select_rewrite_confirm)
        self.transaction_select_rewrite_button = QPushButton('Проверить замену в контексте всей транзакции на копиях')
        buttons = QHBoxLayout()
        buttons.addWidget(self.transaction_select_rewrite_preview)
        buttons.addWidget(self.transaction_select_rewrite_button)
        settings_layout.addLayout(buttons)
        layout.addWidget(settings)
        self.transaction_select_rewrite_status = QLabel('Начните с локальной подготовки. Исходный редактор транзакции не изменяется.')
        self.transaction_select_rewrite_status.setWordWrap(True)
        self.transaction_select_rewrite_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.transaction_select_rewrite_status)
        self.transaction_select_rewrite_generate.clicked.connect(self._start_transaction_select_rewrites)
        self.transaction_select_rewrite_choice.currentIndexChanged.connect(self._transaction_select_rewrite_selected)
        self.transaction_select_rewrite_variant.textChanged.connect(self._clear_transaction_select_rewrite_result)
        self.transaction_select_rewrite_preview.clicked.connect(self._show_transaction_select_rewrite_preview)
        self.transaction_select_rewrite_button.clicked.connect(self._start_transaction_select_rewrite_trial)
        for signal in (self.transaction_select_rewrite_reference.textChanged,self.transaction_select_rewrite_series.valueChanged,
                self.transaction_select_rewrite_gain_ms.valueChanged,self.transaction_select_rewrite_gain_percent.valueChanged,
                self.transaction_select_rewrite_confirm.toggled):
            signal.connect(self._clear_transaction_select_rewrite_result)
        # Direct invalidation also works when no estimated plans were requested.
        for editor in (self.transaction_editor,self.host_edit,self.database_edit,self.user_edit,self.password_edit,self.allowed_schemas_edit):
            signal = editor.textChanged
            signal.connect(self._clear_transaction_select_rewrites)
        self.port_spin.valueChanged.connect(self._clear_transaction_select_rewrites)

    def _select_rewrite_policy(self):
        return MixedReplayPolicy(minimum_gain_ms=self.transaction_select_rewrite_gain_ms.value(),
            minimum_gain_ratio=self.transaction_select_rewrite_gain_percent.value()/100)

    def _select_rewrite_current(self):
        try:
            return self._transaction_select_rewrite_snapshot == (self.transaction_editor.toPlainText(),
                self._transaction_effective_sql(),self._database_settings())
        except ValueError:
            return False

    def _clear_transaction_select_rewrite_result(self,*_):
        if not hasattr(self,'transaction_select_rewrite_page'): return
        had_result = self.transaction_select_rewrite_report is not None
        self.transaction_select_rewrite_report = None
        self.transaction_select_rewrite_status.setText('Результат не получен или сброшен после изменения варианта/контекста/порогов.')
        if hasattr(self,'transaction_views') and self.transaction_views.currentIndex()==9:
            self.transaction_details.setPlainText('Нет актуального результата проверки замены SELECT.')
            if had_result: self.transaction_status.setText('Результат переписывания сброшен; требуется новая проверка.')
        self._sync_transaction_select_rewrite_controls()

    def _clear_transaction_select_rewrites(self,*_):
        if not hasattr(self,'transaction_select_rewrite_page'): return
        self._transaction_select_rewrite_snapshot = None
        self._transaction_select_rewrite_options = ()
        self.transaction_select_rewrite_choice.blockSignals(True)
        self.transaction_select_rewrite_choice.clear()
        self.transaction_select_rewrite_choice.blockSignals(False)
        self.transaction_select_rewrite_original.clear()
        self.transaction_select_rewrite_variant.clear()
        self._clear_transaction_select_rewrite_result()

    def _sync_transaction_select_rewrite_controls(self):
        if not hasattr(self,'transaction_select_rewrite_page'): return
        idle = not self._query_operation_active and not self._active_workers
        ready = bool(self._transaction_select_rewrite_options) and self._select_rewrite_current()
        for widget in (self.transaction_select_rewrite_choice,self.transaction_select_rewrite_reference,
                self.transaction_select_rewrite_series,self.transaction_select_rewrite_gain_ms,
                self.transaction_select_rewrite_gain_percent,self.transaction_select_rewrite_confirm): widget.setEnabled(idle)
        self.transaction_select_rewrite_variant.setReadOnly(not idle or not ready)
        self.transaction_select_rewrite_generate.setEnabled(idle and bool(self.transaction_editor.toPlainText().strip()))
        self.transaction_select_rewrite_preview.setEnabled(idle and ready)
        self.transaction_select_rewrite_button.setEnabled(idle and ready and self.transaction_select_rewrite_confirm.isChecked()
            and bool(self.transaction_select_rewrite_reference.text().strip()) and bool(self.transaction_select_rewrite_variant.toPlainText().strip()))

    def _start_transaction_select_rewrites(self):
        if self._query_operation_active or self._active_workers: return
        try:
            snapshot = (self.transaction_editor.toPlainText(),self._transaction_effective_sql(),self._database_settings())
        except ValueError as exc:
            self.transaction_select_rewrite_status.setText(str(exc)); return
        from .desktop import Worker
        self._clear_transaction_select_rewrites()
        self._transaction_select_rewrite_active = 'generate'
        control = AnalysisControl()
        worker = Worker(lambda: (snapshot,generate_transaction_select_rewrites(snapshot[1],snapshot[2],control=control)))
        self._start_select_rewrite_worker(worker,control,self._transaction_select_rewrites_ready)

    def _start_select_rewrite_worker(self,worker,control,callback):
        self._transaction_control = control
        control.progress = worker.signals.progress.emit
        worker.signals.progress.connect(self.transaction_status.setText)
        worker.signals.succeeded.connect(callback)
        worker.signals.failed.connect(self._transaction_select_rewrite_failed)
        self._set_query_actions_enabled(False)
        self.transaction_status.setText('Локальная подготовка замен…' if self._transaction_select_rewrite_active=='generate' else 'Проверка замены: вся транзакция только на одноразовых копиях…')
        self._start_worker(worker)

    def _transaction_select_rewrites_ready(self,payload):
        snapshot,options = payload
        self._transaction_control = None; self._transaction_select_rewrite_active = None
        self._transaction_select_rewrite_snapshot = snapshot
        if not self._select_rewrite_current():
            self._clear_transaction_select_rewrites()
            self.transaction_status.setText('Устаревшая локальная подготовка не присоединена.')
        else:
            self._transaction_select_rewrite_options = options
            self.transaction_select_rewrite_choice.blockSignals(True)
            for option in options: self.transaction_select_rewrite_choice.addItem(f'SELECT № {option.number}: {option.title}')
            self.transaction_select_rewrite_choice.blockSignals(False)
            self._transaction_select_rewrite_selected()
            self._transaction_reveal_analysis()
            automatic = sum(option.rule_id!='manual' for option in options)
            message = f'Подготовлено автоматических вариантов: {automatic}. Ничего не выполнялось. '
            message += 'Выберите вариант или измените SELECT справа.' if automatic else 'Поддерживаемых автоматических замен нет; доступен ручной вариант справа.'
            self.transaction_select_rewrite_status.setText(message)
            self.transaction_status.setText(message)
            self.transaction_views.setCurrentIndex(9)
        self._set_query_actions_enabled(True)

    def _transaction_select_rewrite_selected(self,*_):
        index = self.transaction_select_rewrite_choice.currentIndex()
        if not 0 <= index < len(self._transaction_select_rewrite_options): return
        option = self._transaction_select_rewrite_options[index]
        self.transaction_select_rewrite_original.setPlainText(parse_one(option.original_sql,read='postgres').sql(dialect='postgres',pretty=True))
        self.transaction_select_rewrite_variant.setPlainText(parse_one(option.variant_sql,read='postgres').sql(dialect='postgres',pretty=True))
        self._clear_transaction_select_rewrite_result()

    def _select_rewrite_prepared(self):
        if not self._select_rewrite_current(): raise ValueError('SQL/подключение/параметры изменились; подготовьте замены заново.')
        index = self.transaction_select_rewrite_choice.currentIndex()
        if not 0 <= index < len(self._transaction_select_rewrite_options): raise ValueError('Выберите SELECT.')
        snapshot = self._transaction_select_rewrite_snapshot
        option = self._transaction_select_rewrite_options[index]
        prepared = prepare_select_replacement(snapshot[1],snapshot[2],option.number,self.transaction_select_rewrite_variant.toPlainText())
        return snapshot,prepared

    def _show_transaction_select_rewrite_preview(self):
        try:
            _,prepared = self._select_rewrite_prepared()
            self.transaction_details.setPlainText(f'ЛОКАЛЬНЫЙ ПРОСМОТР — НЕ ЗАПУСК\nМеняется только SELECT № {prepared.number}. '
                'Все остальные команды и их порядок неизменны.\n\nТранзакция с заменой (нормализованный SQL):\n'+_render(prepared.variant.commands))
            if self.transaction_workspace_mode.currentData() != 'report':
                self._transaction_open_protocol()
        except ValueError as exc:
            self.transaction_select_rewrite_status.setText(str(exc))

    def _expand_transaction_select_editors(self, expanded):
        self.transaction_select_rewrite_settings.setVisible(not expanded)
        self.transaction_select_rewrite_note.setVisible(not expanded)
        self.transaction_select_rewrite_expand.setText('Показать настройки проверки' if expanded else 'Развернуть редакторы SQL')

    def _start_transaction_select_rewrite_trial(self):
        if self._query_operation_active or self._active_workers: return
        try:
            snapshot,prepared = self._select_rewrite_prepared()
            if not self.transaction_select_rewrite_confirm.isChecked(): raise ValueError('Нужно явное разрешение на одноразовые БД и выполнение DML.')
            reference,series = self.transaction_select_rewrite_reference.text().strip(),self.transaction_select_rewrite_series.value()
            policy = self._select_rewrite_policy()
            from .transaction_replay import validate_reference
            validate_reference(snapshot[2],reference,True,True)
        except ValueError as exc:
            self.transaction_select_rewrite_status.setText(str(exc)); return
        answer = QMessageBox.question(self,'Замена SELECT в транзакции — только лабораторные копии',
            f'Сервер: {snapshot[2].host}:{snapshot[2].port}\nИсходная БД (не выполняется): {snapshot[2].dbname}\nЭталон: {reference}\n'
            f'Меняется только SELECT № {prepared.number}. До {1+3*series} новых БД; вся транзакция с DML/COMMIT в каждой копии. '
            f'В B используется замена, новые индексы не создаются. Пороги общего выигрыша: {policy.minimum_gain_ms:g} мс И {policy.minimum_gain_ratio*100:g}%. '
            'Каталоги/исходные состояния/все результаты/итоговые данные и удаление копий проверяются. '
            'Только доверенный тестовый сервер, не production. Бюджет 300 с; CREATE может задержать отмену до 30 с. Продолжить?',
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        if answer!=QMessageBox.StandardButton.Yes: return
        from .desktop import ARTIFACT_ROOT,Worker
        self._clear_transaction_select_rewrite_result()
        self._transaction_select_rewrite_active = 'replay'
        control = AnalysisControl()
        worker = Worker(lambda: (snapshot,reference,series,policy,prepared,
            run_transaction_select_rewrite(snapshot[1],snapshot[2],reference,prepared.number,prepared.variant_sql,series=series,
                policy=policy,control=control,test_copy_confirmed=True,disposable_databases_confirmed=True,
                recovery_directory=ARTIFACT_ROOT/'disposable-recovery')))
        self._start_select_rewrite_worker(worker,control,self._transaction_select_rewrite_ready)

    def _transaction_select_rewrite_ready(self,payload):
        snapshot,reference,series,policy,prepared,result = payload
        self._transaction_control = None; self._transaction_select_rewrite_active = None
        report = result.measurement
        try:
            current,current_prepared = self._select_rewrite_prepared()
            expected = select_rewrite_request_digest(current[1],current[2],self.transaction_select_rewrite_reference.text().strip(),
                current_prepared.number,current_prepared.variant_sql,self.transaction_select_rewrite_series.value(),self._select_rewrite_policy())
            valid = (snapshot==current and reference==self.transaction_select_rewrite_reference.text().strip()
                and series==self.transaction_select_rewrite_series.value() and policy==self._select_rewrite_policy()
                and prepared==current_prepared and result.number==prepared.number and report.request_digest==expected
                and report.policy==policy and report.recommendation is not None
                and report.recommendation.request_digest==expected and report.recommendation.policy==policy)
        except Exception:
            valid = False
        if not valid:
            self.transaction_status.setText('Устаревший результат замены не присоединён. '+('Удаление копий подтверждено.'
                if report.cleanup_verified else 'ВНИМАНИЕ: удаление копий НЕ подтверждено: '+', '.join(report.leftover_databases)))
        else:
            self.transaction_select_rewrite_report = result
            status = {'recommended':'Замена рекомендована для измеренной транзакции',
                'not_recommended':'Замена не дала надёжного общего выигрыша',
                'inconclusive':'Недостаточно надёжных данных для рекомендации'}[report.recommendation.status]
            status += f'. Результаты/данные: {"совпали" if report.correctness_verified else "НЕ подтверждены"}; удаление копий: {report.cleanup_verified}.'
            self.transaction_select_rewrite_status.setText(status)
            self.transaction_status.setText(status)
            self.transaction_views.setCurrentIndex(9)
            self._show_transaction_select_rewrite()
        self._set_query_actions_enabled(True)

    def _transaction_select_rewrite_failed(self,message):
        phase = self._transaction_select_rewrite_active
        self._transaction_control = None; self._transaction_select_rewrite_active = None
        message = ('Локальная подготовка не завершена; БД не подключалась. ' if phase=='generate' else
            'Проверка не завершена; удаление одноразовых БД НЕ считается подтверждённым. ') + message
        self.transaction_select_rewrite_status.setText(message); self.transaction_status.setText(message)
        self._set_query_actions_enabled(True)

    def _show_transaction_select_rewrite(self):
        result = self.transaction_select_rewrite_report
        if result is None:
            self.transaction_details.setPlainText('Нет актуального результата замены SELECT. Подготовьте варианты, '
                'сравните исходный и правый SELECT; локальный просмотр показывает полный порядок команд без запуска.')
            return
        report = result.measurement; decision = report.recommendation
        lines = [f'ЗАМЕНА SELECT № {result.number} В ИЗМЕРЕННОЙ ТРАНЗАКЦИИ — НЕ PRODUCTION',
            f'Эталон: {report.reference_database}; решение: {decision.status}.',
            f'Исходные состояния: {report.initial_states_equal}; все результаты/RETURNING: {report.results_equal}; '
            f'итоговые данные: {report.final_states_equal}; удаление копий: {report.cleanup_verified}.',
            f'Не удалены/не проверены: {report.leftover_databases}.',
            'A1/A2 — исходная транзакция; B — только один SELECT заменён. Порядок DML и остальных SELECT неизменен; новых индексов нет.',
            'Сравниваются результаты каждой команды в её контексте, не отдельный SELECT до транзакции. '
            'ORDER BY — последовательность, иначе мультимножество с NULL/дубликатами; сравниваются имена/типы столбцов и затронутые строки.',
            'Общее время — клиент BEGIN–COMMIT, включая результат/его хеш/служебные тайм-ауты; '
            'команды — execute+fetch. Клонирование и полные снимки исключены; UTC/ISO/jit=off/parallel=0, кэш прогрет снимками.',
            *decision.reasons,'','Медианы серий (+ ускорение / − замедление; решение проверяет каждую серию):']
        lines.extend(f'{m.label}: {m.baseline_ms:.3f} → {m.indexed_ms:.3f} мс; {m.gain_ms:+.3f} мс / {m.gain_ratio*100:+.1f}%.' for m in decision.summary)
        for sample in report.samples:
            lines.append(f'Серия {sample.series} {sample.phase}: BEGIN–COMMIT {sample.transaction_client_ms:.3f} мс.')
        lines.extend(['',f'Журнал владения копиями (без SQL/данных): {report.recovery_manifest}',
            'Нет автоматического применения, сохранения SQL/значений или обучения. Равенство на этих данных — не доказательство для любых данных/конкуренции.',*report.reasons])
        self.transaction_details.setPlainText('\n'.join(lines))
