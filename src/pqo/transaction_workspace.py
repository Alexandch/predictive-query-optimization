"""Resizable transaction workspace. View changes never alter analysis state or SQL."""

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


def collapsible_notice(title, text):
    panel = QWidget()
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    button = QToolButton()
    button.setText(title)
    button.setCheckable(True)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    button.setArrowType(Qt.ArrowType.RightArrow)
    description = QLabel(text)
    description.setWordWrap(True)
    description.setVisible(False)
    button.toggled.connect(description.setVisible)
    button.toggled.connect(
        lambda opened: button.setArrowType(
            Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow
        )
    )
    layout.addWidget(button)
    layout.addWidget(description)
    return panel


class TransactionWorkspaceMixin:
    def _build_transaction_workspace(
        self, layout, index_actions, trial_actions, comparison_actions
    ):
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Вид:"))
        self.transaction_workspace_mode = QComboBox()
        for label, mode in (
            ("SQL транзакции", "sql"),
            ("Анализ и проверки", "analysis"),
            ("SQL + анализ", "split"),
            ("Подробный протокол", "report"),
        ):
            self.transaction_workspace_mode.addItem(label, mode)
        self.transaction_workspace_mode.setToolTip(
            "В режиме «SQL + анализ» высоту панелей можно менять перетягиванием разделителя."
        )
        controls.addWidget(self.transaction_workspace_mode)
        self.transaction_protocol_check = QCheckBox("Протокол рядом")
        self.transaction_protocol_check.setChecked(
            str(self.preferences.value("transaction_protocol_beside", "false")).lower()
            in {"true", "1"}
        )
        controls.addWidget(self.transaction_protocol_check)
        self.transaction_protocol_button = QPushButton("Открыть протокол")
        controls.addWidget(self.transaction_protocol_button)
        controls.addStretch()
        layout.addLayout(controls)

        self.transaction_workspace_splitter = QSplitter(Qt.Orientation.Vertical)
        self.transaction_workspace_splitter.setHandleWidth(8)
        self.transaction_workspace_splitter.setChildrenCollapsible(False)
        source = self.transaction_source_panel = QGroupBox("Исходная транзакция")
        source_layout = QVBoxLayout(source)
        self.transaction_editor.setMinimumHeight(100)
        source_layout.addWidget(self.transaction_editor)
        self.transaction_workspace_splitter.addWidget(source)

        analysis = self.transaction_analysis_panel = QWidget()
        body = QVBoxLayout(analysis)
        body.setContentsMargins(0, 0, 0, 0)
        self.transaction_index_tools = QWidget()
        self.transaction_index_tools.setLayout(index_actions)
        body.addWidget(self.transaction_index_tools)
        self.transaction_trial_tools = QWidget()
        trial_layout = QVBoxLayout(self.transaction_trial_tools)
        trial_layout.setContentsMargins(0, 0, 0, 0)
        trial_layout.addLayout(trial_actions)
        trial_layout.addLayout(comparison_actions)
        body.addWidget(self.transaction_trial_tools)

        work = QHBoxLayout()
        self.transaction_sections = QListWidget()
        metrics = self.transaction_sections.fontMetrics()
        labels = [
            self.transaction_views.tabText(i)
            for i in range(self.transaction_views.count())
        ]
        self.transaction_sections.setFixedWidth(
            max(205, max(metrics.horizontalAdvance(label) for label in labels) + 30)
        )
        self.transaction_sections.setStyleSheet(
            "QListWidget::item { padding: 7px 4px; }"
            "QListWidget::item:selected { background: #2563eb; color: white; }"
        )
        for i in range(self.transaction_views.count()):
            self.transaction_sections.addItem(self.transaction_views.tabText(i))
            self.transaction_sections.item(i).setSizeHint(
                QSize(190, max(33, metrics.height() + 16))
            )
        self.transaction_views.tabBar().hide()
        work.addWidget(self.transaction_sections)
        self.transaction_result_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.transaction_result_splitter.setHandleWidth(8)
        self.transaction_result_splitter.setChildrenCollapsible(False)
        self.transaction_result_splitter.addWidget(self.transaction_views)
        protocol = self.transaction_protocol_panel = QGroupBox("Подробный протокол")
        protocol_layout = QVBoxLayout(protocol)
        self.transaction_details.setPlaceholderText(
            "Выберите команду или результат проверки."
        )
        protocol_layout.addWidget(self.transaction_details)
        self.transaction_result_splitter.addWidget(protocol)
        work.addWidget(self.transaction_result_splitter, 1)
        body.addLayout(work, 1)
        self.transaction_workspace_splitter.addWidget(analysis)
        for splitter in (
            self.transaction_workspace_splitter,
            self.transaction_result_splitter,
        ):
            splitter.setStyleSheet(
                "QSplitter::handle { background: #334155; }"
                "QSplitter::handle:hover { background: #60a5fa; }"
            )
            splitter.handle(1).setToolTip(
                "Перетащите разделитель, чтобы изменить размеры панелей."
            )
        self.transaction_workspace_splitter.setStretchFactor(0, 1)
        self.transaction_workspace_splitter.setStretchFactor(1, 3)
        layout.addWidget(self.transaction_workspace_splitter, 1)
        self._restore_transaction_panel_sizes()

        self.transaction_sections.currentRowChanged.connect(
            self._select_transaction_section
        )
        self.transaction_views.currentChanged.connect(
            self._transaction_workspace_section_changed
        )
        self.transaction_workspace_mode.currentIndexChanged.connect(
            self._apply_transaction_workspace_mode
        )
        self.transaction_workspace_mode.activated.connect(
            self._save_transaction_workspace_mode
        )
        self.transaction_protocol_check.toggled.connect(
            self._transaction_protocol_toggled
        )
        self.transaction_protocol_button.clicked.connect(
            self._transaction_open_protocol
        )
        self.transaction_workspace_splitter.splitterMoved.connect(
            self._save_transaction_panel_sizes
        )
        self.transaction_result_splitter.splitterMoved.connect(
            self._save_transaction_panel_sizes
        )
        mode = self.preferences.value("transaction_workspace_mode", "sql")
        self._last_transaction_mode = None
        self._last_transaction_beside = False
        self.transaction_workspace_mode.setCurrentIndex(
            max(0, self.transaction_workspace_mode.findData(mode))
        )
        self._transaction_workspace_section_changed(
            self.transaction_views.currentIndex()
        )

    def _restore_transaction_panel_sizes(self):
        for splitter, key, defaults in (
            (
                self.transaction_workspace_splitter,
                "transaction_vertical_sizes",
                [220, 540],
            ),
            (
                self.transaction_result_splitter,
                "transaction_horizontal_sizes",
                [750, 350],
            ),
        ):
            splitter.setSizes(self._transaction_saved_sizes(key, defaults))

    def _transaction_saved_sizes(self, key, defaults):
        values = self.preferences.value(key, defaults)
        try:
            if not isinstance(values, (list, tuple)) or len(values) != 2:
                raise ValueError("Invalid splitter sizes")
            sizes = [int(value) for value in values]
            if any(value < 60 or value > 20000 for value in sizes):
                raise ValueError("Invalid splitter size")
            return sizes
        except (TypeError, ValueError, OverflowError):
            return defaults

    def _save_transaction_panel_sizes(self, *_):
        # A hidden child has size zero: do not overwrite useful split positions.
        if self.transaction_workspace_mode.currentData() == "split":
            self.preferences.setValue(
                "transaction_vertical_sizes",
                self.transaction_workspace_splitter.sizes(),
            )
        if (
            self.transaction_views.isVisible()
            and self.transaction_protocol_panel.isVisible()
        ):
            self.preferences.setValue(
                "transaction_horizontal_sizes", self.transaction_result_splitter.sizes()
            )

    def _save_transaction_workspace_mode(self, *_):
        self.preferences.setValue(
            "transaction_workspace_mode", self.transaction_workspace_mode.currentData()
        )

    def _apply_transaction_workspace_mode(self, *_):
        mode = self.transaction_workspace_mode.currentData()
        self.transaction_source_panel.setVisible(mode in {"sql", "split"})
        self.transaction_analysis_panel.setVisible(mode != "sql")
        report_only = mode == "report"
        self.transaction_sections.setVisible(not report_only)
        self.transaction_views.setVisible(not report_only)
        self.transaction_protocol_panel.setVisible(
            report_only or self.transaction_protocol_check.isChecked()
        )
        self.transaction_protocol_check.setEnabled(mode in {"split", "analysis"})
        self.transaction_protocol_button.setText(
            "Вернуться к работе" if report_only else "Открыть протокол"
        )
        if mode == "split" and self._last_transaction_mode != "split":
            self.transaction_workspace_splitter.setSizes(
                self._transaction_saved_sizes("transaction_vertical_sizes", [220, 540])
            )
        beside = (
            mode in {"split", "analysis"}
            and self.transaction_protocol_check.isChecked()
        )
        if beside and (
            not self._last_transaction_beside or self._last_transaction_mode == "sql"
        ):
            self.transaction_result_splitter.setSizes(
                self._transaction_saved_sizes(
                    "transaction_horizontal_sizes", [750, 350]
                )
            )
        self._last_transaction_mode = mode
        self._last_transaction_beside = beside
        self._transaction_workspace_tools()

    def _transaction_workspace_tools(self):
        index = self.transaction_views.currentIndex()
        report_only = self.transaction_workspace_mode.currentData() == "report"
        self.transaction_index_tools.setVisible(not report_only and index in {0, 1})
        self.transaction_trial_tools.setVisible(
            not report_only and index in {1, 2, 3, 4}
        )

    def _transaction_workspace_section_changed(self, index):
        self.transaction_sections.blockSignals(True)
        self.transaction_sections.setCurrentRow(index)
        self.transaction_sections.blockSignals(False)
        self._transaction_workspace_tools()
        self._apply_transaction_workspace_mode()

    def _select_transaction_section(self, index):
        if 0 <= index < self.transaction_views.count():
            self.transaction_views.setCurrentIndex(index)
            self._transaction_reveal_analysis()

    def _transaction_reveal_analysis(self):
        if not hasattr(self, "transaction_workspace_mode"):
            return
        if self.transaction_workspace_mode.currentData() in {"sql", "report"}:
            self.transaction_workspace_mode.setCurrentIndex(
                self.transaction_workspace_mode.findData("analysis")
            )

    def _transaction_protocol_toggled(self, checked):
        self.preferences.setValue("transaction_protocol_beside", checked)
        self._apply_transaction_workspace_mode()

    def _transaction_open_protocol(self):
        if self.transaction_workspace_mode.currentData() == "report":
            self._transaction_reveal_analysis()
        else:
            self.transaction_workspace_mode.setCurrentIndex(
                self.transaction_workspace_mode.findData("report")
            )
