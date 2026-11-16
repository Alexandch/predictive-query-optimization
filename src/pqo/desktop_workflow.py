"""Guided source → laboratory → SELECT → transaction workflow."""

from dataclasses import asdict, replace
import json
import re
from uuid import uuid4

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .analysis_control import AnalysisControl
from .connections import ConnectionProfile, endpoint, encode_profiles, PROFILE_KEY
from .laboratory import prepare_laboratory


class DesktopWorkflowMixin:
    def _build_workflow(self):
        self._workflow_connected = set()
        self._workflow_copy_control = None
        self._workflow_select_link = None
        self._workflow_last_error = None
        try:
            saved = json.loads(str(self.preferences.value("laboratories_v1", "[]")))
            self._workflow_labs = [
                x
                for x in saved
                if isinstance(x, dict)
                and {"source_profile", "working_profile", "stand", "endpoint"}
                <= x.keys()
                and isinstance(x["stand"], dict)
                and all(
                    isinstance(x["stand"].get(k), str)
                    for k in (
                        "source_database",
                        "working_database",
                        "reference_database",
                    )
                )
                and isinstance(x["endpoint"], list)
            ]
        except (ValueError, TypeError):
            self._workflow_labs = []
        self.workflow_stack = QStackedWidget()
        self.workflow_stack.addWidget(self.takeCentralWidget())
        self.setCentralWidget(self.workflow_stack)
        home = QWidget()
        layout = QVBoxLayout(home)
        layout.setSpacing(14)
        title = QLabel("От подключения до проверенной оптимизации")
        title.setObjectName("pageTitle")
        top = QHBoxLayout()
        top.addWidget(title, 1)
        resume = QPushButton("Рабочее окно")
        resume.clicked.connect(
            lambda: self._workflow_open_tab(self.tabs.currentIndex())
        )
        top.addWidget(resume)
        layout.addLayout(top)
        subtitle = QLabel(
            "Подготовьте тестовый стенд, проверьте отдельные запросы и сравните всю транзакцию на одинаковых данных."
        )
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        self.workflow_context = QLabel()
        self.workflow_context.setWordWrap(True)
        layout.addWidget(self.workflow_context)
        grid = QGridLayout()
        grid.setSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        def card(number, title, text, row, column):
            group = QGroupBox(f"{number}. {title}")
            group.setMinimumWidth(0)
            group.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            body = QVBoxLayout(group)
            body.setSpacing(9)
            label = QLabel(text)
            label.setWordWrap(True)
            label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            body.addWidget(label)
            grid.addWidget(group, row, column)
            return body

        first = card(
            1,
            "Подключите свою БД",
            "Укажите сервер и базу, введите пароль и выберите схемы для проверки.",
            0,
            0,
        )
        self.workflow_connection_status = QLabel()
        self.workflow_connection_status.setWordWrap(True)
        first.addWidget(self.workflow_connection_status)
        self.workflow_settings_button = QPushButton("Настроить подключение")
        # Settings are a QScrollArea; route by its actual child, not a tab number.
        self.workflow_settings_button.clicked.connect(self._workflow_open_settings)
        self.workflow_connect_button = QPushButton("Проверить подключение и схемы")
        self.workflow_connect_button.clicked.connect(self._load_source_schemas)
        first.addWidget(self.workflow_settings_button)
        first.addWidget(self.workflow_connect_button)
        first.addStretch()
        second = card(
            2,
            "Подготовьте тестовый стенд",
            "Рабочая копия — для запросов. Отдельный эталон сохраняет исходные данные для сравнения транзакций.",
            0,
            1,
        )
        self.workflow_copy_group = second.parentWidget()
        self.workflow_target_combo = QComboBox()
        self.workflow_target_combo.setToolTip(
            "Сервер, на котором создаются новые копии. Можно выбрать подключение к отдельному тестовому PostgreSQL."
        )
        second.addWidget(self.workflow_target_combo)
        self.workflow_copy_name = QLineEdit()
        self.workflow_copy_name.setPlaceholderText("Название новой рабочей копии")
        self.workflow_copy_name.setToolTip(
            "Создаются две новые БД на выбранном сервере: это имя и имя с суффиксом _base. Существующие БД не заменяются."
        )
        second.addWidget(self.workflow_copy_name)
        self.workflow_lab_details = QLabel()
        self.workflow_lab_details.setWordWrap(True)
        self.workflow_lab_details.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        second.addWidget(self.workflow_lab_details)
        self.workflow_copy_button = QPushButton("Создать тестовый стенд")
        self.workflow_copy_button.setObjectName("primaryButton")
        self.workflow_copy_button.clicked.connect(self._workflow_create_copy)
        second.addWidget(self.workflow_copy_button)
        self.workflow_use_copy_button = QPushButton("Переключиться на рабочую копию")
        self.workflow_use_copy_button.clicked.connect(self._workflow_use_copy)
        second.addWidget(self.workflow_use_copy_button)
        self.workflow_source_button = QPushButton("Вернуться к исходному подключению")
        self.workflow_source_button.clicked.connect(self._workflow_use_source)
        second.addWidget(self.workflow_source_button)
        self.workflow_cancel_button = QPushButton("Отменить создание")
        self.workflow_cancel_button.clicked.connect(
            lambda: (
                self._workflow_copy_control.cancel()
                if self._workflow_copy_control
                else None
            )
        )
        second.addWidget(self.workflow_cancel_button)
        self.workflow_tools_edit = QLineEdit(
            str(self.preferences.value("postgres_tools_directory", ""))
        )
        self.workflow_tools_edit.setPlaceholderText(
            "Папка bin с pg_dump и pg_restore (обычно находится автоматически)"
        )
        self.workflow_tools_edit.setVisible(False)
        second.addWidget(self.workflow_tools_edit)
        second.addStretch()
        third = card(
            3,
            "Проверьте отдельные запросы",
            "Получите прогноз, проверьте индексы и сравните исходный SELECT с новым вариантом в рабочей копии.",
            1,
            0,
        )
        self.workflow_query_button = QPushButton("Открыть анализ SQL")
        self.workflow_query_button.clicked.connect(lambda: self._workflow_open_tab(0))
        third.addWidget(self.workflow_query_button)
        third.addStretch()
        fourth = card(
            4,
            "Сравните транзакцию целиком",
            "Загрузите BEGIN … COMMIT/ROLLBACK. Индексы и замены SELECT проверяются вместе с записью на свежих копиях эталона.",
            1,
            1,
        )
        self.workflow_transaction_button = QPushButton("Открыть транзакцию")
        self.workflow_transaction_button.clicked.connect(
            self._workflow_open_transaction
        )
        fourth.addWidget(self.workflow_transaction_button)
        self.workflow_result_button = QPushButton("Посмотреть последний результат")
        self.workflow_result_button.clicked.connect(self._workflow_open_result)
        fourth.addWidget(self.workflow_result_button)
        fourth.addStretch()
        layout.addLayout(grid)
        self.workflow_status = QLabel(
            "Начните с подключения. Для создания стенда нужны клиентские утилиты PostgreSQL и право CREATEDB."
        )
        self.workflow_status.setWordWrap(True)
        layout.addWidget(self.workflow_status)
        note = QLabel(
            "Копируются выбранные схемы доверенного источника. Служебная БД с историей хранится отдельно. Исходные пользовательские команды в БД-источнике не запускаются."
        )
        note.setWordWrap(True)
        note.setObjectName("mutedLabel")
        layout.addWidget(note)
        layout.addStretch()
        self.workflow_home = QScrollArea()
        self.workflow_home.setWidgetResizable(True)
        self.workflow_home.setWidget(home)
        self.workflow_stack.addWidget(self.workflow_home)
        home_button = QPushButton("Начало")
        home_button.clicked.connect(self._workflow_show_home)
        self.tabs.setCornerWidget(home_button, Qt.Corner.TopLeftCorner)
        self.tabs.currentChanged.connect(
            lambda *_: self.workflow_stack.setCurrentWidget(self.tabs)
        )
        self._build_workflow_transfers()
        self.transaction_views.currentChanged.connect(
            lambda *_: self._refresh_workflow()
        )
        self._refresh_workflow()
        self._workflow_show_home()

    def _workflow_show_home(self):
        self._refresh_workflow()
        self.workflow_stack.setCurrentWidget(self.workflow_home)

    def _workflow_open_tab(self, index):
        self.workflow_stack.setCurrentWidget(self.tabs)
        self.tabs.setCurrentIndex(index)

    def _workflow_open_settings(self):
        for i in range(self.tabs.count()):
            page = self.tabs.widget(i)
            if isinstance(page, QScrollArea) and page.widget() is self.settings_page:
                self._workflow_open_tab(i)
                page.verticalScrollBar().setValue(0)
                return

    def _workflow_open_transaction(self):
        self._workflow_open_tab(1)
        self.transaction_workspace_mode.setCurrentIndex(
            self.transaction_workspace_mode.findData("sql")
        )

    def _workflow_open_result(self):
        self._workflow_open_tab(1)
        self.transaction_views.setCurrentIndex(
            9 if self.transaction_select_rewrite_report else 8
        )
        self._transaction_reveal_analysis()

    def _workflow_lab(self):
        try:
            current = list(endpoint(self._database_settings()))
        except ValueError:
            return None
        return next(
            (
                x
                for x in reversed(self._workflow_labs)
                if x.get("working_profile") == self._active_profile_id
                and x.get("endpoint") == current
            ),
            None,
        )

    def _refresh_workflow(self):
        if not hasattr(self, "workflow_status"):
            return
        busy = bool(self._active_workers) or self._query_operation_active
        try:
            settings = self._database_settings()
            key = endpoint(settings)
        except ValueError:
            settings = None
            key = None
        lab = self._workflow_lab()
        existing = lab or next(
            (
                x
                for x in reversed(self._workflow_labs)
                if x.get("source_profile") == self._active_profile_id
            ),
            None,
        )
        self.workflow_context.setText(
            f"Текущая база: {settings.host}:{settings.port}/{settings.dbname} · схемы: {', '.join(sorted(settings.allowed_schemas))}"
            if settings
            else "Проверьте параметры подключения и выберите хотя бы одну схему."
        )
        self.workflow_connection_status.setText(
            "Выбрана рабочая копия. Источник сохранён в отдельном профиле."
            if lab
            else "Подключение проверено. Можно подготовить стенд."
            if key in self._workflow_connected
            else "Подключение ещё не проверено в этом сеансе."
        )
        if (
            settings
            and not lab
            and not self.workflow_copy_name.isModified()
            and self.workflow_copy_name.property("source") != settings.dbname
        ):
            base = re.sub("[^A-Za-z0-9_]", "_", settings.dbname)[:28]
            self.workflow_copy_name.setText("pqo_lab_" + base + "_" + uuid4().hex[:6])
            self.workflow_copy_name.setProperty("source", settings.dbname)
        self.workflow_settings_button.setEnabled(not busy)
        self.workflow_connect_button.setEnabled(not busy)
        self.workflow_copy_name.setEnabled(not busy and not lab)
        self.workflow_copy_name.setVisible(lab is None)
        self.workflow_target_combo.setVisible(lab is None)
        self.workflow_target_combo.setEnabled(not busy)
        ids = tuple(p.id for p in self._connection_profiles)
        if self.workflow_target_combo.property("profiles") != ids:
            selected = self.workflow_target_combo.currentData()
            self.workflow_target_combo.clear()
            self.workflow_target_combo.addItem(
                "Сервер копии: тот же, что у источника", None
            )
            for p in self._connection_profiles:
                self.workflow_target_combo.addItem(
                    f"{p.name} · {p.host}:{p.port}", p.id
                )
            self.workflow_target_combo.setCurrentIndex(
                max(0, self.workflow_target_combo.findData(selected))
            )
            self.workflow_target_combo.setProperty("profiles", ids)
        self.workflow_lab_details.setVisible(lab is not None)
        self.workflow_source_button.setVisible(lab is not None)
        self.workflow_source_button.setEnabled(not busy)
        self.workflow_copy_group.setTitle(
            "2. Тестовый стенд готов" if lab else "2. Подготовьте тестовый стенд"
        )
        self.workflow_tools_edit.setEnabled(not busy)
        self.workflow_tools_edit.setVisible(self._advanced_mode)
        self.workflow_copy_button.setEnabled(
            not busy
            and settings is not None
            and key in self._workflow_connected
            and not lab
        )
        self.workflow_copy_button.setVisible(lab is None)
        self.workflow_use_copy_button.setVisible(existing is not None and lab is None)
        self.workflow_use_copy_button.setEnabled(not busy)
        self.workflow_cancel_button.setVisible(self._workflow_copy_control is not None)
        self.workflow_cancel_button.setEnabled(
            self._workflow_copy_control is not None
            and not self._workflow_copy_control.cancelled.is_set()
        )
        self.workflow_query_button.setEnabled(not busy and lab is not None)
        self.workflow_transaction_button.setEnabled(not busy and lab is not None)
        self.workflow_result_button.setEnabled(
            not busy
            and bool(
                self.transaction_replay_report or self.transaction_select_rewrite_report
            )
        )
        if lab:
            stand = lab["stand"]
            self.workflow_lab_details.setText(
                f"Рабочая копия: {stand['working_database']}\nЭталон: {stand['reference_database']}"
            )
            message = f"Стенд готов. Запросы: {stand['working_database']}. Эталон транзакций: {stand['reference_database']}. Источник: {stand['source_database']}."
            if not busy:
                self.workflow_status.setText(self._workflow_last_error or message)
            if (
                getattr(self, "_workflow_reference_profile", None)
                != self._active_profile_id
            ):
                self._workflow_reference_profile = self._active_profile_id
                self._workflow_copy_fields(stand["reference_database"])
        else:
            self._workflow_reference_profile = None
            if not busy:
                self.workflow_status.setText(
                    self._workflow_last_error
                    or (
                        "Подключение готово. Следующий шаг — создать тестовый стенд."
                        if key in self._workflow_connected
                        else "Начните с подключения. Для копирования нужны pg_dump/pg_restore и право CREATEDB."
                    )
                )
        if hasattr(self, "workflow_send_select_button"):
            self.workflow_send_select_button.setEnabled(
                not busy and self.transaction_inspection is not None
            )
            self.workflow_send_select_button.setVisible(
                self.transaction_views.currentIndex() == 0
            )
            self.workflow_return_select_button.setEnabled(
                not busy and self._workflow_select_link is not None
            )
            self.workflow_return_select_button.setVisible(
                self._workflow_select_link is not None
            )
        self._workflow_transaction_navigation()

    def _workflow_transaction_navigation(self):
        if not hasattr(self, "transaction_sections"):
            return
        for index in range(self.transaction_sections.count()):
            self.transaction_sections.item(index).setHidden(
                not self._advanced_mode and index in {2, 3, 4, 6, 7}
            )
        self.transaction_trial_tools.setVisible(
            self._advanced_mode
            and self.transaction_views.currentIndex() in {1, 2, 3, 4}
            and self.transaction_workspace_mode.currentData() != "report"
        )

    def _workflow_create_copy(self):
        if not self._user_action_ready(database=False):
            return
        from .desktop import Worker, ARTIFACT_ROOT

        try:
            source = self._database_settings()
            self._remember_profile()
            if endpoint(source) not in self._workflow_connected:
                raise ValueError("Сначала проверьте подключение и загрузите схемы.")
            name = self.workflow_copy_name.text().strip()
            source_profile = self._active_profile_id
            selected = self.workflow_target_combo.currentData()
            profile = next(
                (p for p in self._connection_profiles if p.id == selected), None
            )
            destination = (
                profile.settings(
                    self._profile_passwords.get(endpoint(profile.settings()), "")
                )
                if profile
                else source
            )
        except ValueError as exc:
            self.workflow_status.setText(str(exc))
            return
        control = AnalysisControl()
        self._workflow_copy_control = control
        self._workflow_last_error = None
        tools = self.workflow_tools_edit.text().strip() or None
        worker = Worker(
            lambda: (
                source_profile,
                source,
                prepare_laboratory(
                    source,
                    name,
                    destination_settings=destination,
                    tools_directory=tools,
                    control=control,
                    recovery_directory=ARTIFACT_ROOT / "laboratory-preparation",
                ),
                destination,
            )
        )
        control.progress = worker.signals.progress.emit
        worker.signals.progress.connect(self.workflow_status.setText)
        worker.signals.succeeded.connect(self._workflow_copy_ready)
        worker.signals.failed.connect(self._workflow_copy_failed)
        self._start_worker(worker)

    def _workflow_copy_ready(self, payload):
        source_profile, source, stand = payload[:3]
        destination = payload[3] if len(payload) > 3 else source
        self._workflow_copy_control = None
        self._workflow_last_error = None
        profile = ConnectionProfile.from_settings(
            "Тест: " + source.dbname,
            replace(
                destination,
                dbname=stand.working_database,
                allowed_schemas=source.allowed_schemas,
            ),
        )
        self._connection_profiles.append(profile)
        self._profile_passwords[endpoint(profile.settings())] = destination.password
        record = {
            "source_profile": source_profile,
            "working_profile": profile.id,
            "endpoint": list(endpoint(profile.settings())),
            "stand": asdict(stand),
        }
        self._workflow_labs.append(record)
        current = (self._active_profile_id, self._database_settings())
        if current == (source_profile, source):
            self._workflow_use_profile(profile.id)
            self._workflow_connected.add(endpoint(self._database_settings()))
        self.preferences.setValue(
            PROFILE_KEY, encode_profiles(self._connection_profiles)
        )
        self.preferences.setValue("active_connection_profile", self._active_profile_id)
        self.preferences.setValue(
            "laboratories_v1", json.dumps(self._workflow_labs, ensure_ascii=False)
        )
        self.preferences.setValue(
            "postgres_tools_directory", self.workflow_tools_edit.text().strip()
        )
        self.preferences.sync()
        self._workflow_copy_fields(stand.reference_database)
        self._refresh_workflow()

    def _workflow_copy_fields(self, reference):
        self.transaction_replay_reference.setText(reference)
        self.transaction_select_rewrite_reference.setText(reference)
        self.transaction_copy_edit.setText(reference)
        # Real replay remains an explicit action with its normal confirmation.
        self.transaction_replay_confirm.setChecked(False)
        self.transaction_select_rewrite_confirm.setChecked(False)
        self.transaction_copy_check.setChecked(False)

    def _workflow_use_profile(self, profile_id):
        profile = next(
            (p for p in self._connection_profiles if p.id == profile_id), None
        )
        if profile is None:
            self.workflow_status.setText(
                "Профиль копии удалён. Подготовьте новый стенд."
            )
            return
        self._remember_profile()
        self._active_profile_id = profile_id
        self._load_profile_fields(profile)
        self._fill_profile_combos()

    def _workflow_use_copy(self):
        lab = next(
            (
                x
                for x in reversed(self._workflow_labs)
                if x.get("source_profile") == self._active_profile_id
            ),
            None,
        )
        if lab:
            self._workflow_use_profile(lab["working_profile"])
            self._workflow_copy_fields(lab["stand"]["reference_database"])
            self._refresh_workflow()

    def _workflow_use_source(self):
        lab = self._workflow_lab()
        if lab:
            self._workflow_use_profile(lab["source_profile"])
            self._refresh_workflow()

    def _workflow_copy_failed(self, message):
        self._workflow_copy_control = None
        self._workflow_last_error = message
        self.workflow_status.setText(message)
        self._refresh_workflow()

    def _build_workflow_transfers(self):
        self.workflow_send_select_button = QPushButton(
            "Проверить выбранный SELECT отдельно"
        )
        self.workflow_send_select_button.clicked.connect(self._workflow_send_select)
        self.transaction_analysis_panel.layout().insertWidget(
            0, self.workflow_send_select_button
        )
        self.workflow_return_select_button = QPushButton(
            "Вернуть вариант в проверку транзакции"
        )
        self.workflow_return_select_button.clicked.connect(self._workflow_return_select)
        self.workflow_return_select_button.setToolTip(
            "Заполняет замену SELECT. Исходная транзакция сохраняется; нужен отдельный прогон всей транзакции."
        )
        self.analysis_scroll.widget().layout().insertWidget(
            1, self.workflow_return_select_button
        )

    def _workflow_send_select(self):
        if not self._user_action_ready(database=False):
            return
        from .transaction_inspection import inspect_transaction

        text = self.transaction_editor.toPlainText()
        try:
            effective = self._transaction_effective_sql()
            inspection = inspect_transaction(effective)
            row = self.transaction_table.currentRow()
            item = (
                inspection.statements[row]
                if 0 <= row < len(inspection.statements)
                else next(
                    (x for x in inspection.statements if x.kind == "select"), None
                )
            )
            if item is None or item.kind != "select":
                raise ValueError("Выберите самостоятельный SELECT в списке команд.")
            settings = self._database_settings()
        except ValueError as exc:
            self.transaction_status.setText(str(exc))
            return
        self._workflow_select_link = (text, effective, settings, item.number)
        self.sql_editor.setPlainText(item.sql)
        self.rewrite_editor.clear()
        self._workflow_open_tab(0)
        self._refresh_workflow()

    def _workflow_return_select(self):
        if (
            not self._user_action_ready(database=False)
            or self._workflow_select_link is None
        ):
            return
        from .transaction_select_rewrite import (
            generate_transaction_select_rewrites,
            prepare_select_replacement,
        )

        text, effective, settings, number = self._workflow_select_link
        try:
            if (text, effective, settings) != (
                self.transaction_editor.toPlainText(),
                self._transaction_effective_sql(),
                self._database_settings(),
            ):
                raise ValueError(
                    "Транзакция, параметры или подключение изменились. Откройте SELECT из транзакции заново."
                )
            variant = (
                self.rewrite_editor.toPlainText().strip()
                or self.sql_editor.toPlainText().strip()
            )
            prepared = prepare_select_replacement(effective, settings, number, variant)
            options = generate_transaction_select_rewrites(effective, settings)
        except ValueError as exc:
            self.variant_status.setText(str(exc))
            return
        self._clear_transaction_select_rewrites()
        self._transaction_select_rewrites_ready(((text, effective, settings), options))
        row = next(
            i
            for i, x in enumerate(options)
            if x.number == number and x.rule_id == "manual"
        )
        self.transaction_select_rewrite_choice.setCurrentIndex(row)
        self.transaction_select_rewrite_variant.setPlainText(prepared.variant_sql)
        self._workflow_open_tab(1)
        self.transaction_views.setCurrentIndex(9)
        self._transaction_reveal_analysis()
