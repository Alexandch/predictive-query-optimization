"""Compact desktop mode and shared lifecycle rules for user-initiated work."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QMessageBox, QWidget


class DesktopSessionMixin:
    def _build_session_controls(self):
        self.advanced_mode_check = QCheckBox("Расширенный режим")
        self.advanced_mode_check.setToolTip(
            "Обучение, эксперименты, пути к моделям и ручная калибровка. "
            "Переключение режима не запускает SQL и не меняет выбранные веса."
        )
        corner = QWidget()
        row = QHBoxLayout(corner)
        row.setContentsMargins(5, 0, 5, 0)
        row.addWidget(QLabel("БД:"))
        self.active_database_combo = QComboBox()
        self.active_database_combo.setMaximumWidth(280)
        self.active_database_combo.setMinimumWidth(160)
        row.addWidget(self.active_database_combo)
        row.addWidget(self.advanced_mode_check)
        self._fill_profile_combos()
        self.active_database_combo.currentIndexChanged.connect(self._profile_selected)
        self.tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        selected = str(self.preferences.value("advanced_mode", "false")).lower() in {"1", "true", "yes"}
        self._set_advanced_mode(selected, persist=False)
        self.advanced_mode_check.toggled.connect(self._set_advanced_mode)

    def _set_advanced_mode(self, selected, *, persist=True):
        if persist and (self._active_workers or self._query_operation_active):
            self.advanced_mode_check.blockSignals(True)
            self.advanced_mode_check.setChecked(self._advanced_mode)
            self.advanced_mode_check.blockSignals(False)
            return
        selected = bool(selected)
        self._advanced_mode = selected
        self.advanced_mode_check.blockSignals(True)
        self.advanced_mode_check.setChecked(selected)
        self.advanced_mode_check.blockSignals(False)
        for page in (self.experiments_page, self.training_page):
            index = self.tabs.indexOf(page)
            if not selected and self.tabs.currentWidget() is page:
                self.tabs.setCurrentWidget(self.analysis_scroll)
            self.tabs.setTabVisible(index, selected)
        for row in self._advanced_settings_rows:
            self.settings_form.setRowVisible(row, selected)
        self.batch_calibrate_button.setVisible(selected)
        self.calibration_status.setVisible(selected)
        if persist:
            self.preferences.setValue("advanced_mode", selected)
            self.preferences.sync()

    def _sync_application_availability(self):
        """Lock inputs while any worker owns the session; leave cancellation usable."""
        busy = bool(self._active_workers) or self._query_operation_active
        self._set_query_actions_enabled(not self._query_operation_active)
        for page in (self.settings_page, self.experiments_page, self.training_page):
            page.setEnabled(not busy)
        self.structural_management_group.setEnabled(not busy)
        self.advanced_mode_check.setEnabled(not busy)
        self.active_database_combo.setEnabled(not busy)

    def _user_action_ready(self, *, database=True):
        if self._active_workers or self._query_operation_active:
            self.statusBar().showMessage("Дождитесь завершения текущей операции.")
            return False
        if database:
            try:
                self._database_settings()
                if self.persist_check.isChecked():
                    self._service_database_settings()
            except (ValueError, TypeError) as exc:
                QMessageBox.warning(self, "Проверьте настройки подключения", str(exc))
                return False
        return True
