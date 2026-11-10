"""Separate service storage and named, non-secret source connection controls."""

from dataclasses import replace
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
)
from .config import DatabaseSettings
from .connections import (
    ConnectionProfile,
    PROFILE_KEY,
    SERVICE_KEY,
    decode_profiles,
    encode_profiles,
    endpoint,
    discover_schemas,
    prepare_service_schema,
)


def ssl_choice():
    widget = QComboBox()
    for label, value in (
        ("По умолчанию (libpq)", None),
        ("Без SSL", "disable"),
        ("Предпочитать SSL", "prefer"),
        ("Разрешить SSL", "allow"),
        ("SSL обязателен", "require"),
        ("Проверять CA", "verify-ca"),
        ("Проверять CA и имя сервера", "verify-full"),
    ):
        widget.addItem(label, value)
    return widget


class DesktopConnectionsMixin:
    def _build_profile_header(self, layout):
        row = QHBoxLayout()
        row.addWidget(QLabel("Исследуемая БД — профиль:"))
        self.connection_profile_combo = QComboBox()
        row.addWidget(self.connection_profile_combo, 1)
        self.profile_new_button = QPushButton("Добавить подключение")
        self.profile_delete_button = QPushButton("Удалить профиль")
        self.profile_new_button.clicked.connect(self._new_connection_profile)
        self.profile_delete_button.clicked.connect(self._delete_connection_profile)
        row.addWidget(self.profile_new_button)
        row.addWidget(self.profile_delete_button)
        layout.addLayout(row)

    def _build_schema_picker(self, layout):
        row = QHBoxLayout()
        self.schema_load_button = QPushButton("Подключиться и загрузить схемы")
        self.schema_load_button.clicked.connect(self._load_source_schemas)
        self.schema_all_button = QPushButton("Выбрать все")
        self.schema_none_button = QPushButton("Снять выбор")
        self.schema_all_button.clicked.connect(lambda: self._set_all_schemas(True))
        self.schema_none_button.clicked.connect(lambda: self._set_all_schemas(False))
        for w in (
            self.schema_load_button,
            self.schema_all_button,
            self.schema_none_button,
        ):
            row.addWidget(w)
        row.addStretch()
        layout.addLayout(row)
        self.source_schema_list = QListWidget()
        self.source_schema_list.setMaximumHeight(125)
        self.source_schema_list.itemChanged.connect(self._schema_checks_changed)
        layout.addWidget(self.source_schema_list)
        self.source_connection_status = QLabel(
            "Схемы можно ввести через запятую или загрузить с сервера. Ничего не создаётся."
        )
        self.source_connection_status.setWordWrap(True)
        layout.addWidget(self.source_connection_status)

    def _build_connection_manager(self, layout):
        self._profile_passwords = {}
        self._switching_profile = False
        self._profiles_load_error = None
        self._service_load_error = None
        self._schema_snapshot = None
        self._build_schema_picker(layout)
        self.source_ssl_combo = ssl_choice()
        self.source_ssl_combo.setToolTip(
            "Для удалённой БД рекомендуется verify-full с доверенным корневым сертификатом."
        )
        self.source_cert_edit = QLineEdit()
        self.source_cert_edit.setPlaceholderText(
            "Путь к корневому сертификату CA (необязательно)"
        )
        ssl_row = QHBoxLayout()
        ssl_row.addWidget(QLabel("SSL исследуемой БД:"))
        ssl_row.addWidget(self.source_ssl_combo)
        ssl_row.addWidget(self.source_cert_edit, 1)
        layout.addLayout(ssl_row)
        service = QGroupBox(
            "Служебная БД — история всех подключений (отдельно от исследуемой)"
        )
        form = QFormLayout(service)
        defaults = replace(
            DatabaseSettings.from_env(),
            dbname="pqo_service",
            allowed_schemas=frozenset({"pqo"}),
        )
        try:
            raw = self.preferences.value(SERVICE_KEY)
            profile = (
                decode_profiles(raw)[0]
                if raw
                else ConnectionProfile.from_settings("Служебная БД", defaults)
            )
            self._service_profile_id = profile.id
            configured = profile.settings()
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            configured = defaults
            self._service_profile_id = "service"
            self._service_load_error = str(exc)
        self.service_host_edit = QLineEdit(configured.host)
        self.service_port_spin = QSpinBox()
        self.service_port_spin.setRange(1, 65535)
        self.service_port_spin.setValue(configured.port)
        self.service_database_edit = QLineEdit(configured.dbname)
        self.service_user_edit = QLineEdit(configured.user)
        self.service_password_edit = QLineEdit(
            defaults.password if endpoint(configured) == endpoint(defaults) else ""
        )
        self.service_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.service_ssl_combo = ssl_choice()
        self.service_ssl_combo.setCurrentIndex(
            max(0, self.service_ssl_combo.findData(configured.sslmode))
        )
        self.service_cert_edit = QLineEdit(configured.sslrootcert or "")
        for label, widget in (
            ("Хост", self.service_host_edit),
            ("Порт", self.service_port_spin),
            ("Служебная база", self.service_database_edit),
            ("Пользователь", self.service_user_edit),
            ("Пароль (только этот сеанс)", self.service_password_edit),
            ("SSL", self.service_ssl_combo),
            ("Корневой сертификат CA", self.service_cert_edit),
        ):
            form.addRow(label, widget)
        buttons = QHBoxLayout()
        self.service_test_button = QPushButton("Проверить служебную БД")
        self.service_create_button = QPushButton("Создать служебную БД")
        self.service_prepare_button = QPushButton("Подготовить служебную схему")
        self.service_test_button.clicked.connect(self._test_service_connection)
        self.service_create_button.clicked.connect(self._create_service_connection)
        self.service_prepare_button.clicked.connect(self._prepare_service_connection)
        buttons.addWidget(self.service_test_button)
        buttons.addWidget(self.service_create_button)
        buttons.addWidget(self.service_prepare_button)
        form.addRow(buttons)
        self.service_status = QLabel(
            "Служебная БД хранит историю. Внешняя БД не требует установки pqo. Пароли не сохраняются."
        )
        self.service_status.setWordWrap(True)
        form.addRow(self.service_status)
        if self._service_load_error:
            self.service_status.setText(
                "Сохранённое служебное подключение повреждено. Показаны параметры по умолчанию; проверьте их перед сохранением."
            )
        layout.addWidget(service)
        try:
            legacy = self._database_settings()
        except (ValueError, TypeError):
            legacy = DatabaseSettings.from_env()
        initial = ConnectionProfile.from_settings("Текущее подключение", legacy)
        raw = self.preferences.value(PROFILE_KEY)
        try:
            self._connection_profiles = decode_profiles(raw) if raw else [initial]
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self._connection_profiles = [initial]
            self._profiles_load_error = str(exc)
            self.source_connection_status.setText(
                "Сохранённые профили повреждены. Используется прежнее подключение; запись профилей пока не изменена."
            )
        # Offer the configured local demonstration DB, not a guessed remote DB.
        default_source = DatabaseSettings.from_env()
        if not raw and endpoint(default_source) != endpoint(legacy):
            self._connection_profiles.append(
                ConnectionProfile.from_settings("Учебная БД", default_source)
            )
        active = str(
            self.preferences.value(
                "active_connection_profile", self._connection_profiles[0].id
            )
        )
        self._active_profile_id = (
            active
            if any(p.id == active for p in self._connection_profiles)
            else self._connection_profiles[0].id
        )
        self._profile_passwords[endpoint(legacy)] = self.password_edit.text()
        self._profile_passwords.setdefault(
            endpoint(default_source), default_source.password
        )
        self._fill_profile_combos()
        current = next(
            p for p in self._connection_profiles if p.id == self._active_profile_id
        )
        self._load_profile_fields(current)
        self.connection_profile_combo.currentIndexChanged.connect(
            self._profile_selected
        )
        for field in (
            self.host_edit,
            self.database_edit,
            self.user_edit,
            self.allowed_schemas_edit,
            self.password_edit,
            self.source_cert_edit,
        ):
            field.textChanged.connect(self._source_connection_edited)
        self.port_spin.valueChanged.connect(self._source_connection_edited)
        self.source_ssl_combo.currentIndexChanged.connect(
            self._source_connection_edited
        )
        for field in (
            self.service_host_edit,
            self.service_database_edit,
            self.service_user_edit,
            self.service_password_edit,
            self.service_cert_edit,
        ):
            field.textChanged.connect(self._service_connection_edited)
        self.service_port_spin.valueChanged.connect(self._service_connection_edited)
        self.service_ssl_combo.currentIndexChanged.connect(
            self._service_connection_edited
        )

    def _fill_profile_combos(self):
        for combo in (
            self.connection_profile_combo,
            getattr(self, "active_database_combo", None),
        ):
            if combo is None:
                continue
            combo.blockSignals(True)
            combo.clear()
            for profile in self._connection_profiles:
                combo.addItem(f"{profile.name} — {profile.database}", profile.id)
                combo.setItemData(
                    combo.count() - 1,
                    f"{profile.host}:{profile.port}/{profile.database}\nСхемы: {', '.join(profile.schemas)}",
                    Qt.ItemDataRole.ToolTipRole,
                )
            combo.setCurrentIndex(max(0, combo.findData(self._active_profile_id)))
            combo.blockSignals(False)

    def _load_profile_fields(self, profile):
        self._switching_profile = True
        try:
            self.host_edit.setText(profile.host)
            self.port_spin.setValue(profile.port)
            self.database_edit.setText(profile.database)
            self.user_edit.setText(profile.user)
            self.allowed_schemas_edit.setText(",".join(profile.schemas))
            self.source_ssl_combo.setCurrentIndex(
                max(0, self.source_ssl_combo.findData(profile.sslmode))
            )
            self.source_cert_edit.setText(profile.sslrootcert or "")
            self.password_edit.setText(
                self._profile_passwords.get(endpoint(profile.settings()), "")
            )
            self.source_schema_list.clear()
            self._schema_snapshot = None
        finally:
            self._switching_profile = False
        self._source_connection_edited()

    def _remember_profile(self):
        settings = self._database_settings()
        old = next(
            p for p in self._connection_profiles if p.id == self._active_profile_id
        )
        new = ConnectionProfile.from_settings(old.name, settings, profile_id=old.id)
        self._connection_profiles = [
            new if p.id == old.id else p for p in self._connection_profiles
        ]
        self._profile_passwords[endpoint(settings)] = settings.password

    def _profile_selected(self, index):
        selected = (
            self.sender().itemData(index)
            if self.sender() is not None
            else self.connection_profile_combo.itemData(index)
        )
        if not selected or selected == self._active_profile_id:
            return
        if self._active_workers or self._query_operation_active:
            self._fill_profile_combos()
            return
        try:
            self._remember_profile()
        except ValueError:
            QMessageBox.warning(
                self,
                "Профиль не переключён",
                "Исправьте текущие параметры и выберите хотя бы одну схему перед переключением.",
            )
            self._fill_profile_combos()
            return
        self._active_profile_id = selected
        self._load_profile_fields(
            next(p for p in self._connection_profiles if p.id == selected)
        )
        self._fill_profile_combos()
        self.statusBar().showMessage(
            "Исследуемая БД изменена. Служебное подключение и модели сохранены."
        )

    def _new_connection_profile(self):
        if not self._user_action_ready(database=False):
            return
        name, ok = QInputDialog.getText(self, "Новое подключение", "Название профиля:")
        if not ok or not name.strip():
            return
        try:
            self._remember_profile()
        except ValueError:
            QMessageBox.warning(
                self,
                "Параметры текущего профиля",
                "Исправьте поля текущего подключения перед добавлением нового.",
            )
            return
        profile = ConnectionProfile.from_settings(
            name,
            DatabaseSettings(
                dbname="postgres",
                user="postgres",
                password="",
                host="localhost",
                port=5432,
                allowed_schemas=frozenset({"public"}),
            ),
        )
        self._connection_profiles.append(profile)
        self._active_profile_id = profile.id
        self._fill_profile_combos()
        self._load_profile_fields(profile)
        self.source_connection_status.setText(
            "Укажите реальный сервер, базу и роль, введите пароль и загрузите схемы. Сохраните профиль кнопкой «Сохранить настройки»."
        )

    def _delete_connection_profile(self):
        if not self._user_action_ready(database=False):
            return
        if len(self._connection_profiles) <= 1:
            QMessageBox.information(
                self, "Профили", "Нужно оставить хотя бы одно подключение."
            )
            return
        if (
            QMessageBox.question(
                self,
                "Удалить профиль?",
                "Удалится только локальный профиль. База данных и служебная история не удаляются.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._connection_profiles = [
            p for p in self._connection_profiles if p.id != self._active_profile_id
        ]
        self._active_profile_id = self._connection_profiles[0].id
        self._fill_profile_combos()
        self._load_profile_fields(self._connection_profiles[0])

    def _service_database_settings(self):
        settings = DatabaseSettings(
            host=self.service_host_edit.text().strip(),
            port=self.service_port_spin.value(),
            dbname=self.service_database_edit.text().strip(),
            user=self.service_user_edit.text().strip(),
            password=self.service_password_edit.text(),
            allowed_schemas=frozenset({"pqo"}),
            sslmode=self.service_ssl_combo.currentData(),
            sslrootcert=self.service_cert_edit.text().strip() or None,
        )
        source_identity = (
            self.host_edit.text().strip().lower(),
            self.port_spin.value(),
            self.database_edit.text().strip(),
        )
        if (settings.host.lower(), settings.port, settings.dbname) == source_identity:
            raise ValueError(
                "Служебная и исследуемая БД должны быть разными. Выберите отдельную служебную базу."
            )
        return settings

    def _save_connection_profiles(self):
        if self._profiles_load_error or self._service_load_error:
            if (
                QMessageBox.question(
                    self,
                    "Восстановить профили?",
                    "Сохранённый список профилей повреждён. Заменить его текущим восстановленным списком?",
                )
                != QMessageBox.StandardButton.Yes
            ):
                return False
        self._remember_profile()
        service = ConnectionProfile.from_settings(
            "Служебная БД",
            self._service_database_settings(),
            profile_id=self._service_profile_id,
        )
        self.preferences.setValue(
            PROFILE_KEY, encode_profiles(self._connection_profiles)
        )
        self.preferences.setValue("active_connection_profile", self._active_profile_id)
        self.preferences.setValue(SERVICE_KEY, encode_profiles([service]))
        self._profiles_load_error = None
        self._service_load_error = None
        self._fill_profile_combos()
        return True

    def _source_connection_edited(self, *_):
        if getattr(self, "_switching_profile", False):
            return
        self.last_query_run_id = None
        self.last_analyzed_sql = None
        self.last_sequential_plan = None
        self.last_prediction_ms = None
        self.last_uncalibrated_prediction_ms = None
        self.predicted_value.setText("—")
        self.cost_value.setText("—")
        self.rows_value.setText("—")
        self.node_value.setText("—")
        self._invalidate_rewrite_candidates()
        self._clear_verified_rewrite()
        self.applied_rewrite_sql = None
        self.applied_rewrite_original_sql = None
        self.rewrite_editor.clear()
        if hasattr(self, "transaction_editor"):
            self._invalidate_transaction_plans()
        if self._schema_snapshot is not None:
            try:
                stale = endpoint(self._schema_snapshot) != endpoint(
                    self._source_catalog_settings()
                )
            except (ValueError, TypeError):
                stale = True
            if stale:
                self.source_schema_list.blockSignals(True)
                self.source_schema_list.clear()
                self.source_schema_list.blockSignals(False)
                self._schema_snapshot = None
        self._reset_structural_feedback()
        if hasattr(self, "source_connection_status"):
            self.source_connection_status.setText(
                "Подключение изменено. Загрузите схемы или задайте их явно; прежние результаты сброшены."
            )
        self._refresh_calibration_status()
        self._refresh_managed_index_state()
        self._set_query_actions_enabled(not self._query_operation_active)

    def _service_connection_edited(self, *_):
        self.last_query_run_id = None
        self._history_connection_settings = None
        self._reset_structural_feedback()
        self._sync_history_decision_controls()
        self.service_status.setText(
            "Служебное подключение изменено. Проверьте его; история и решения требуют обновления."
        )

    def _load_source_schemas(self):
        if not self._user_action_ready(database=False):
            return
        from .desktop import Worker

        try:
            settings = self._source_catalog_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Исследуемая БД", str(exc))
            return
        self._schema_snapshot = settings
        self.source_connection_status.setText("Подключаюсь и читаю доступные схемы…")
        worker = Worker(discover_schemas, settings)
        worker.signals.succeeded.connect(
            lambda result: self._source_schemas_loaded(settings, result)
        )
        worker.signals.failed.connect(
            lambda message: self.source_connection_status.setText(
                "Схемы не загружены: " + message
            )
        )
        self._start_worker(worker)

    def _source_schemas_loaded(self, settings, result):
        try:
            if settings != self._source_catalog_settings():
                return
        except (ValueError, TypeError):
            return
        self.source_schema_list.blockSignals(True)
        self.source_schema_list.clear()
        database, rows = result
        for name, count in rows:
            item = QListWidgetItem(f"{name} — таблиц и представлений: {count}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if name in settings.allowed_schemas
                else Qt.CheckState.Unchecked
            )
            self.source_schema_list.addItem(item)
        self.source_schema_list.blockSignals(False)
        self.source_connection_status.setText(
            f"Подключено: {settings.host}:{settings.port}/{database}. Доступно схем: {len(rows)}. Выберите нужные и сохраните настройки."
        )

    def _set_all_schemas(self, checked):
        self.source_schema_list.blockSignals(True)
        for i in range(self.source_schema_list.count()):
            self.source_schema_list.item(i).setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
        self.source_schema_list.blockSignals(False)
        self._schema_checks_changed()

    def _source_catalog_settings(self):
        # Discovery remains available when the user has deselected all schemas.
        schemas = frozenset(
            x.strip() for x in self.allowed_schemas_edit.text().split(",") if x.strip()
        )
        return DatabaseSettings(
            host=self.host_edit.text().strip(),
            port=self.port_spin.value(),
            dbname=self.database_edit.text().strip(),
            user=self.user_edit.text().strip(),
            password=self.password_edit.text(),
            allowed_schemas=schemas or frozenset({"public"}),
            sslmode=self.source_ssl_combo.currentData(),
            sslrootcert=self.source_cert_edit.text().strip() or None,
        )

    @staticmethod
    def _history_source_label(record):
        context = record.target_context or {}
        if not isinstance(context, dict):
            return "Не указана (некорректные метаданные)"
        if not context.get("database"):
            return "Не указана (старая запись)"
        return f"{context.get('host', '?')}:{context.get('port', '?')}/{context['database']}"

    def _schema_checks_changed(self, *_):
        self.allowed_schemas_edit.setText(
            ",".join(
                self.source_schema_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.source_schema_list.count())
                if self.source_schema_list.item(i).checkState() == Qt.CheckState.Checked
            )
        )

    def _test_service_connection(self):
        if not self._user_action_ready(database=False):
            return
        from .desktop import Worker
        from .connections import check_service_connection

        try:
            settings = self._service_database_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Служебная БД", str(exc))
            return
        worker = Worker(check_service_connection, settings)
        worker.signals.succeeded.connect(self.service_status.setText)
        worker.signals.failed.connect(
            lambda message: self.service_status.setText(
                "Служебная БД недоступна: " + message
            )
        )
        self._start_worker(worker)

    def _prepare_service_connection(self):
        if not self._user_action_ready(database=False):
            return
        from .desktop import Worker

        try:
            settings = self._service_database_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Служебная БД", str(exc))
            return
        if (
            QMessageBox.question(
                self,
                "Подготовить служебную схему?",
                f"В {settings.host}:{settings.port}/{settings.dbname} будут созданы/обновлены только объекты служебной схемы pqo. "
                "Исследуемые таблицы не меняются. Это именно выбранная вами служебная БД?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        worker = Worker(prepare_service_schema, settings, confirmed=True)
        worker.signals.succeeded.connect(self.service_status.setText)
        worker.signals.failed.connect(
            lambda message: self.service_status.setText(
                "Схема не подготовлена: " + message
            )
        )
        self._start_worker(worker)

    def _create_service_connection(self):
        if not self._user_action_ready(database=False):
            return
        from .desktop import Worker
        from .connections import create_service_database

        try:
            settings = self._service_database_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Служебная БД", str(exc))
            return
        if (
            QMessageBox.question(
                self,
                "Создать пустую служебную БД?",
                f"На {settings.host}:{settings.port} будет создана база {settings.dbname}. "
                "Нужны права CREATEDB и доступ к базе postgres. Существующая БД не заменяется. Продолжить?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        worker = Worker(create_service_database, settings, confirmed=True)
        worker.signals.succeeded.connect(self.service_status.setText)
        worker.signals.failed.connect(
            lambda message: self.service_status.setText("БД не создана: " + message)
        )
        self._start_worker(worker)
