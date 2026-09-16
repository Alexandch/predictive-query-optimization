"""PyQt6 desktop application for prediction and index recommendations."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys
import traceback

from PyQt6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .analysis_service import QueryAnalysis, analyze_query
from .app_paths import resource_root, writable_root
from .calibration import (
    MINIMUM_ACTIVE_SAMPLES,
    calibrate_query,
    load_profile,
    suggested_profile_path,
)
from .charts import AccuracyBarChart, ScatterChart
from .config import DatabaseSettings
from .dqn import train_dqn
from .experiments import (
    ExperimentReport,
    assess_candidate,
    build_experiment_report,
    export_experiment_report,
    export_history_records,
    promote_candidate_model,
)
from .history import check_database_connection, load_analysis_history
from .index_actions import IndexActionKind
from .sequential_recommendation import (
    SequentialRecommendationPlan,
    recommend_sequential_indexes,
)
from .sql_rewrite import SQLRewritePlan, evaluate_sql_rewrites
from .structural_feedback import (
    RecommendationDecision,
    record_recommendation_decision,
    record_recommendation_measurement,
)
from .training import train_xgboost


PROJECT_ROOT = resource_root()
WRITABLE_ROOT = writable_root()
ARTIFACT_ROOT = WRITABLE_ROOT / "artifacts"
APP_ICON = PROJECT_ROOT / "assets" / "pqo.ico"
DEFAULT_XGB_MODEL = PROJECT_ROOT / "models" / "xgboost" / "xgboost_query_time.joblib"
DEFAULT_DQN_MODEL = PROJECT_ROOT / "models" / "dqn" / "dqn_index_advisor.pt"
DEFAULT_SEQUENTIAL_DQN_MODEL = (
    PROJECT_ROOT / "models" / "sequential_dqn" / "sequential_dqn_index_advisor.pt"
)
DEFAULT_STRATEGY_MODEL = PROJECT_ROOT / "models" / "strategy" / "strategy_selector.joblib"
DEFAULT_DATASET = PROJECT_ROOT / "dataset" / "postgresql" / "aviation_dataset.csv"
DEFAULT_DQN_DATASET = (
    PROJECT_ROOT / "dataset" / "postgresql" / "multidomain_dqn_augmented.jsonl"
)
DEFAULT_DQN_STRESS_METRICS = (
    PROJECT_ROOT
    / "models"
    / "control"
    / "negative_augmented"
    / "logistics"
    / "dqn_control_metrics.json"
)


class WorkerSignals(QObject):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)


class Worker(QRunnable):
    def __init__(self, function, *args, **kwargs):
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.function(*self.args, **self.kwargs)
        except Exception as exc:  # GUI boundary: show a safe, useful error
            traceback.print_exc()
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.signals.succeeded.emit(result)


def _format_index_action(action) -> str:
    keys = ", ".join(f'"{name}"' for name in action.key_columns)
    ddl = f'CREATE INDEX ON "{action.schema_name}"."{action.table_name}" ({keys})'
    if action.include_columns:
        includes = ", ".join(f'"{name}"' for name in action.include_columns)
        ddl += f" INCLUDE ({includes})"
    return ddl + ";"


def _format_structural_recommendations(recommendations) -> str:
    if not recommendations:
        return "Дополнительный структурный анализ: рекомендаций не найдено."
    categories = {
        "aggregation": "Агрегация",
        "join": "JOIN",
        "sort": "Сортировка",
        "materialized_view": "Материализованное представление",
    }
    priorities = {"high": "высокий", "medium": "средний", "low": "низкий"}
    lines = ["Дополнительные структурные рекомендации:"]
    for position, item in enumerate(recommendations, start=1):
        lines.extend(
            (
                f"{position}. [{categories[item.category.value]}; "
                f"приоритет: {priorities[item.priority.value]}] {item.title}",
                f"   Основание: {item.evidence}",
                f"   Действие: {item.action}",
                f"   Проверка: {item.verification}",
            )
        )
        if item.suggested_sql:
            lines.append(f"   Шаблон SQL:\n{item.suggested_sql}")
    return "\n".join(lines)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        self.preferences = QSettings("PQO", "PredictiveQueryOptimization")
        self.thread_pool = QThreadPool.globalInstance()
        self.history_records = []
        self.last_prediction_ms: float | None = None
        self.last_query_run_id: int | None = None
        self.last_analyzed_sql: str | None = None
        self.last_structural_recommendations = ()
        self.structural_feedback_state: dict[int, str] = {}
        self.experiment_report: ExperimentReport | None = None
        self.candidate_directories: dict[str, Path] = {}
        self.setWindowTitle("Predictive Query Optimization")
        if APP_ICON.is_file():
            self.setWindowIcon(QIcon(str(APP_ICON)))
        self.resize(1180, 760)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._build_analysis_tab()
        self._build_history_tab()
        self._build_settings_tab()
        self._build_experiments_tab()
        self._build_training_tab()
        self._refresh_calibration_status()
        self._apply_style()
        self.statusBar().showMessage("Готово")

    def _build_analysis_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("Анализ SQL-запроса")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        self.sql_editor = QPlainTextEdit()
        self.sql_editor.setPlaceholderText("Введите один SELECT или WITH-запрос…")
        self.sql_editor.setPlainText(
            "SELECT flight_id, scheduled_departure\n"
            "FROM aviation.flights\n"
            "WHERE departure_airport = 'MSQ'\n"
            "ORDER BY scheduled_departure"
        )
        self.sql_editor.setMinimumHeight(210)
        self.sql_editor.setFont(QFont("Cascadia Mono", 10))
        layout.addWidget(self.sql_editor)

        controls = QHBoxLayout()
        self.analyze_button = QPushButton("Анализировать")
        self.analyze_button.setObjectName("primaryButton")
        self.analyze_button.clicked.connect(self._start_analysis)
        controls.addWidget(self.analyze_button)
        self.deep_analyze_button = QPushButton("Глубокий анализ индексов")
        self.deep_analyze_button.setToolTip(
            "Выполняет SELECT через EXPLAIN ANALYZE и проверяет до двух "
            "временных индексов с полным откатом."
        )
        self.deep_analyze_button.clicked.connect(self._start_sequential_analysis)
        controls.addWidget(self.deep_analyze_button)
        self.rewrite_analyze_button = QPushButton("Проверить переписывание SQL")
        self.rewrite_analyze_button.setToolTip(
            "Формирует безопасные варианты SQL, доказывает эквивалентность "
            "результатов и измеряет фактическое время выполнения."
        )
        self.rewrite_analyze_button.clicked.connect(self._start_rewrite_analysis)
        controls.addWidget(self.rewrite_analyze_button)
        self.persist_check = QCheckBox("Сохранять в историю pqo")
        self.persist_check.setChecked(
            str(self.preferences.value("persist_analysis", "false")).lower()
            in {"1", "true", "yes"}
        )
        self.persist_check.setToolTip(
            "Включайте только если в подключённой БД установлена служебная схема pqo."
        )
        controls.addWidget(self.persist_check)
        controls.addStretch()
        controls.addWidget(QLabel("Порог рекомендации, мс:"))
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(0, 1_000_000)
        self.threshold_spin.setDecimals(1)
        self.threshold_spin.setValue(
            float(self.preferences.value("threshold_ms", 50.0))
        )
        controls.addWidget(self.threshold_spin)
        layout.addLayout(controls)

        calibration_controls = QHBoxLayout()
        self.use_calibration_check = QCheckBox("Использовать калибровку этой БД")
        self.use_calibration_check.setChecked(
            str(self.preferences.value("use_calibration", "false")).lower()
            in {"1", "true", "yes"}
        )
        calibration_controls.addWidget(self.use_calibration_check)
        self.calibrate_button = QPushButton("Добавить текущий SQL в калибровку")
        self.calibrate_button.clicked.connect(self._start_calibration)
        calibration_controls.addWidget(self.calibrate_button)
        self.calibration_status = QLabel("Калибровка: профиль не создан")
        self.calibration_status.setObjectName("mutedLabel")
        calibration_controls.addWidget(self.calibration_status)
        calibration_controls.addStretch()
        layout.addLayout(calibration_controls)

        metrics = QGridLayout()
        self.predicted_value = self._metric_card(metrics, 0, "Время запроса", "—")
        self.cost_value = self._metric_card(metrics, 1, "Стоимость плана", "—")
        self.rows_value = self._metric_card(metrics, 2, "Строки плана", "—")
        self.node_value = self._metric_card(metrics, 3, "Корневой узел", "—")
        layout.addLayout(metrics)

        recommendation_group = QGroupBox("Рекомендации по оптимизации")
        recommendation_layout = QVBoxLayout(recommendation_group)
        self.recommendation_text = QPlainTextEdit()
        self.recommendation_text.setReadOnly(True)
        self.recommendation_text.setMaximumHeight(300)
        self.recommendation_text.setPlaceholderText(
            "Рекомендация появится после анализа достаточно долгого запроса."
        )
        recommendation_layout.addWidget(self.recommendation_text)

        feedback_row = QHBoxLayout()
        self.structural_recommendation_combo = QComboBox()
        self.structural_recommendation_combo.setEnabled(False)
        self.structural_recommendation_combo.currentIndexChanged.connect(
            self._sync_structural_feedback_controls
        )
        feedback_row.addWidget(self.structural_recommendation_combo, 2)
        self.structural_feedback_note = QLineEdit()
        self.structural_feedback_note.setPlaceholderText("Комментарий (необязательно)")
        feedback_row.addWidget(self.structural_feedback_note, 2)
        self.accept_structural_button = QPushButton("Принять")
        self.accept_structural_button.setEnabled(False)
        self.accept_structural_button.clicked.connect(
            lambda: self._submit_structural_decision(
                RecommendationDecision.ACCEPTED
            )
        )
        feedback_row.addWidget(self.accept_structural_button)
        self.reject_structural_button = QPushButton("Отклонить")
        self.reject_structural_button.setEnabled(False)
        self.reject_structural_button.clicked.connect(
            lambda: self._submit_structural_decision(
                RecommendationDecision.REJECTED
            )
        )
        feedback_row.addWidget(self.reject_structural_button)
        recommendation_layout.addLayout(feedback_row)

        measurement_row = QHBoxLayout()
        measurement_row.addWidget(QLabel("Фактический замер, мс: до"))
        self.structural_before_spin = QDoubleSpinBox()
        self.structural_before_spin.setRange(0, 1_000_000_000)
        self.structural_before_spin.setDecimals(3)
        measurement_row.addWidget(self.structural_before_spin)
        measurement_row.addWidget(QLabel("после"))
        self.structural_after_spin = QDoubleSpinBox()
        self.structural_after_spin.setRange(0, 1_000_000_000)
        self.structural_after_spin.setDecimals(3)
        measurement_row.addWidget(self.structural_after_spin)
        self.save_structural_measurement_button = QPushButton("Сохранить замер")
        self.save_structural_measurement_button.setEnabled(False)
        self.save_structural_measurement_button.clicked.connect(
            self._submit_structural_measurement
        )
        measurement_row.addWidget(self.save_structural_measurement_button)
        self.structural_feedback_status = QLabel(
            "Сохраните анализ в историю, чтобы оставить обратную связь."
        )
        self.structural_feedback_status.setObjectName("mutedLabel")
        measurement_row.addWidget(self.structural_feedback_status, 2)
        recommendation_layout.addLayout(measurement_row)
        layout.addWidget(recommendation_group)
        self.tabs.addTab(page, "Анализ")

    def _metric_card(self, layout: QGridLayout, column: int, caption: str, value: str):
        box = QGroupBox(caption)
        box_layout = QVBoxLayout(box)
        label = QLabel(value)
        label.setObjectName("metricValue")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box_layout.addWidget(label)
        layout.addWidget(box, 0, column)
        return label

    def _build_history_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QHBoxLayout()
        title = QLabel("История анализа")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch()
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self._refresh_history)
        header.addWidget(refresh)
        self.history_export_button = QPushButton("Экспорт CSV/JSON")
        self.history_export_button.setEnabled(False)
        self.history_export_button.clicked.connect(self._export_history)
        header.addWidget(self.history_export_button)
        layout.addLayout(header)

        self.history_table = QTableWidget(0, 11)
        self.history_table.setHorizontalHeaderLabels(
            [
                "ID", "Время", "SQL", "ML-прогноз", "План",
                "Рекомендация", "Q/Reward", "Факт до → после",
                "Выигрыш", "Итог глубокого анализа", "Структурная ОС",
            ]
        )
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header_view = self.history_table.horizontalHeader()
        header_view.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.history_table)
        self.tabs.addTab(page, "История")

    def _build_settings_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("Настройки")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = QFormLayout()

        defaults = DatabaseSettings.from_env()
        self.host_edit = QLineEdit(str(self.preferences.value("db_host", defaults.host)))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(int(self.preferences.value("db_port", defaults.port)))
        self.database_edit = QLineEdit(
            str(self.preferences.value("db_name", defaults.dbname))
        )
        self.user_edit = QLineEdit(str(self.preferences.value("db_user", defaults.user)))
        self.allowed_schemas_edit = QLineEdit(
            str(
                self.preferences.value(
                    "allowed_schemas",
                    ",".join(sorted(defaults.allowed_schemas)),
                )
            )
        )
        self.password_edit = QLineEdit(defaults.password)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.xgb_model_edit = QLineEdit(
            str(self.preferences.value("xgb_model", str(DEFAULT_XGB_MODEL)))
        )
        self.dqn_model_edit = QLineEdit(
            str(self.preferences.value("dqn_model", str(DEFAULT_DQN_MODEL)))
        )
        self.sequential_dqn_model_edit = QLineEdit(
            str(
                self.preferences.value(
                    "sequential_dqn_model", str(DEFAULT_SEQUENTIAL_DQN_MODEL)
                )
            )
        )
        self.strategy_model_edit = QLineEdit(
            str(
                self.preferences.value(
                    "strategy_model", str(DEFAULT_STRATEGY_MODEL)
                )
            )
        )
        form.addRow("Хост", self.host_edit)
        form.addRow("Порт", self.port_spin)
        form.addRow("База данных", self.database_edit)
        form.addRow("Пользователь", self.user_edit)
        form.addRow("Разрешённые схемы", self.allowed_schemas_edit)
        form.addRow("Пароль", self.password_edit)
        form.addRow("Модель XGBoost", self._path_row(self.xgb_model_edit, False))
        form.addRow("Модель DQN", self._path_row(self.dqn_model_edit, False))
        form.addRow(
            "Последовательная DQN",
            self._path_row(self.sequential_dqn_model_edit, False),
        )
        form.addRow(
            "Селектор стратегии",
            self._path_row(self.strategy_model_edit, False),
        )
        layout.addLayout(form)

        note = QLabel(
            "Пароль используется только в памяти текущего процесса и не сохраняется."
        )
        note.setObjectName("mutedLabel")
        layout.addWidget(note)
        actions = QHBoxLayout()
        test_button = QPushButton("Проверить подключение")
        test_button.clicked.connect(self._test_connection)
        save_button = QPushButton("Сохранить настройки")
        save_button.setObjectName("primaryButton")
        save_button.clicked.connect(self._save_settings)
        actions.addWidget(test_button)
        actions.addWidget(save_button)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addStretch()
        self.tabs.addTab(page, "Настройки")

    def _build_experiments_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QHBoxLayout()
        title = QLabel("Эксперименты")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch()
        self.load_experiments_button = QPushButton("Загрузить и пересчитать")
        self.load_experiments_button.setObjectName("primaryButton")
        self.load_experiments_button.clicked.connect(self._load_experiments)
        header.addWidget(self.load_experiments_button)
        self.experiment_export_button = QPushButton("Экспорт CSV/JSON")
        self.experiment_export_button.setEnabled(False)
        self.experiment_export_button.clicked.connect(self._export_experiments)
        header.addWidget(self.experiment_export_button)
        layout.addLayout(header)

        cards = QGridLayout()
        self.xgb_parameter_value = self._metric_card(cards, 0, "XGBoost parameter", "—")
        self.xgb_stress_value = self._metric_card(cards, 1, "XGBoost stress", "—")
        self.dqn_parameter_value = self._metric_card(cards, 2, "DQN parameter", "—")
        self.dqn_stress_value = self._metric_card(cards, 3, "DQN stress", "—")
        layout.addLayout(cards)

        charts = QHBoxLayout()
        self.prediction_chart = ScatterChart()
        self.accuracy_chart = AccuracyBarChart()
        charts.addWidget(self.prediction_chart, 3)
        charts.addWidget(self.accuracy_chart, 2)
        layout.addLayout(charts)

        self.experiment_table = QTableWidget(0, 2)
        self.experiment_table.setHorizontalHeaderLabels(["Метрика", "Значение"])
        self.experiment_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.experiment_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.experiment_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.experiment_table.setMaximumHeight(190)
        layout.addWidget(self.experiment_table)
        self.tabs.insertTab(2, page, "Эксперименты")

    def _build_training_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("Обучение моделей")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        xgb_group = QGroupBox("XGBoost")
        xgb_form = QFormLayout(xgb_group)
        self.xgb_dataset_edit = QLineEdit(
            str(DEFAULT_DATASET)
        )
        self.xgb_output_edit = QLineEdit(
            str(ARTIFACT_ROOT / "models" / "desktop_xgboost")
        )
        self.xgb_tune_check = QCheckBox("Подбирать гиперпараметры")
        self.xgb_tune_check.setChecked(True)
        xgb_form.addRow("Датасет", self._path_row(self.xgb_dataset_edit, False))
        xgb_form.addRow("Каталог результата", self._path_row(self.xgb_output_edit, True))
        xgb_form.addRow("", self.xgb_tune_check)
        self.xgb_train_button = QPushButton("Обучить XGBoost")
        self.xgb_train_button.clicked.connect(self._start_xgb_training)
        xgb_form.addRow("", self.xgb_train_button)
        self.xgb_progress = QProgressBar()
        self.xgb_progress.setRange(0, 100)
        self.xgb_progress.setValue(0)
        xgb_form.addRow("Прогресс", self.xgb_progress)
        self.promote_xgb_button = QPushButton("Назначить кандидата основным")
        self.promote_xgb_button.setEnabled(False)
        self.promote_xgb_button.clicked.connect(
            lambda: self._promote_candidate("xgboost")
        )
        xgb_form.addRow("", self.promote_xgb_button)
        layout.addWidget(xgb_group)

        dqn_group = QGroupBox("DQN")
        dqn_form = QFormLayout(dqn_group)
        self.dqn_dataset_edit = QLineEdit(
            str(DEFAULT_DQN_DATASET)
        )
        self.dqn_output_edit = QLineEdit(
            str(ARTIFACT_ROOT / "models" / "desktop_dqn")
        )
        self.dqn_epochs_spin = QSpinBox()
        self.dqn_epochs_spin.setRange(50, 20_000)
        self.dqn_epochs_spin.setValue(2000)
        self.dqn_split_combo = QComboBox()
        self.dqn_split_combo.addItem("Parameter holdout", "parameter")
        self.dqn_split_combo.addItem("Stress: новые шаблоны", "unseen-template")
        dqn_form.addRow("Опыт JSONL", self._path_row(self.dqn_dataset_edit, False))
        dqn_form.addRow("Каталог результата", self._path_row(self.dqn_output_edit, True))
        dqn_form.addRow("Эпохи", self.dqn_epochs_spin)
        dqn_form.addRow("Режим оценки", self.dqn_split_combo)
        self.dqn_train_button = QPushButton("Обучить DQN")
        self.dqn_train_button.clicked.connect(self._start_dqn_training)
        dqn_form.addRow("", self.dqn_train_button)
        self.dqn_progress = QProgressBar()
        self.dqn_progress.setRange(0, 100)
        self.dqn_progress.setValue(0)
        dqn_form.addRow("Прогресс", self.dqn_progress)
        self.promote_dqn_button = QPushButton("Назначить кандидата основным")
        self.promote_dqn_button.setEnabled(False)
        self.promote_dqn_button.clicked.connect(
            lambda: self._promote_candidate("dqn")
        )
        dqn_form.addRow("", self.promote_dqn_button)
        layout.addWidget(dqn_group)

        self.training_log = QPlainTextEdit()
        self.training_log.setReadOnly(True)
        self.training_log.setPlaceholderText("Здесь появятся метрики обучения.")
        layout.addWidget(self.training_log)
        self.tabs.addTab(page, "Обучение")

    def _path_row(self, editor: QLineEdit, directory: bool) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(editor)
        button = QPushButton("Обзор…")
        button.clicked.connect(lambda: self._choose_path(editor, directory))
        layout.addWidget(button)
        return widget

    def _choose_path(self, editor: QLineEdit, directory: bool) -> None:
        if directory:
            selected = QFileDialog.getExistingDirectory(self, "Выберите каталог")
        else:
            selected, _ = QFileDialog.getOpenFileName(self, "Выберите файл")
        if selected:
            editor.setText(selected)

    def _database_settings(self) -> DatabaseSettings:
        return DatabaseSettings(
            dbname=self.database_edit.text().strip(),
            user=self.user_edit.text().strip(),
            password=self.password_edit.text(),
            host=self.host_edit.text().strip(),
            port=self.port_spin.value(),
            allowed_schemas=frozenset(
                schema.strip()
                for schema in self.allowed_schemas_edit.text().split(",")
                if schema.strip()
            ),
        )

    def _calibration_path(self) -> Path:
        return suggested_profile_path(
            ARTIFACT_ROOT / "calibration",
            self._database_settings(),
            Path(self.xgb_model_edit.text()),
        )

    def _refresh_calibration_status(self) -> None:
        try:
            path = self._calibration_path()
            if not path.is_file():
                self.calibration_status.setText(
                    f"Калибровка: 0/{MINIMUM_ACTIVE_SAMPLES} разных SQL"
                )
                return
            profile = load_profile(path)
        except Exception as exc:
            self.calibration_status.setText(f"Калибровка недоступна: {exc}")
            return
        state = (
            "готова"
            if profile.ready
            else f"нужно {MINIMUM_ACTIVE_SAMPLES} разных SQL"
        )
        self.calibration_status.setText(
            f"Калибровка: {profile.unique_query_count} SQL / "
            f"{profile.sample_count} измер., "
            f"{profile.seen_shape_count} шабл., "
            f"{profile.active_segment_count} активн. групп ({state})"
        )

    def _start_calibration(self) -> None:
        sql_text = self.sql_editor.toPlainText().strip()
        model_path = Path(self.xgb_model_edit.text())
        if not sql_text:
            QMessageBox.warning(self, "Нет SQL", "Введите SELECT или WITH-запрос.")
            return
        if not model_path.is_file():
            QMessageBox.warning(
                self,
                "Модель не найдена",
                "Проверьте путь к XGBoost-модели.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Выполнить запрос для калибровки?",
            "Будет выполнен EXPLAIN ANALYZE: PostgreSQL реально запустит этот "
            "SELECT в read-only-транзакции с заданным таймаутом. Продолжить?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        profile_path = self._calibration_path()
        self.calibrate_button.setEnabled(False)
        self.statusBar().showMessage("Измерение запроса для калибровки…")
        worker = Worker(
            calibrate_query,
            sql_text,
            model_path,
            profile_path,
            self._database_settings(),
        )
        worker.signals.succeeded.connect(self._calibration_finished)
        worker.signals.failed.connect(
            lambda message: self._task_failed(message, self.calibrate_button)
        )
        self.thread_pool.start(worker)

    def _calibration_finished(self, result) -> None:
        self.calibrate_button.setEnabled(True)
        self._refresh_calibration_status()
        self.statusBar().showMessage(
            "Калибровочное измерение добавлено: "
            f"прогноз {result.observation.predicted_time_ms:.2f} мс, "
            f"факт {result.observation.actual_time_ms:.2f} мс"
        )

    def _start_analysis(self) -> None:
        sql_text = self.sql_editor.toPlainText().strip()
        if not sql_text:
            QMessageBox.warning(self, "Нет SQL", "Введите SQL-запрос.")
            return
        xgb_path = Path(self.xgb_model_edit.text())
        dqn_path = Path(self.dqn_model_edit.text())
        strategy_path = Path(self.strategy_model_edit.text())
        if (
            not xgb_path.is_file()
            or not dqn_path.is_file()
            or not strategy_path.is_file()
        ):
            QMessageBox.warning(
                self, "Модель не найдена", "Проверьте пути к моделям в настройках."
            )
            return

        self.analyze_button.setEnabled(False)
        self._reset_structural_feedback()
        self.statusBar().showMessage("Анализ запроса…")
        worker = Worker(
            analyze_query,
            sql_text,
            xgb_path,
            dqn_path,
            self._database_settings(),
            recommendation_threshold_ms=self.threshold_spin.value(),
            persist=self.persist_check.isChecked(),
            calibration_profile_path=(
                self._calibration_path()
                if self.use_calibration_check.isChecked()
                else None
            ),
            strategy_model_path=strategy_path,
        )
        worker.signals.succeeded.connect(self._show_analysis)
        worker.signals.failed.connect(
            lambda message: self._task_failed(message, self.analyze_button)
        )
        self.thread_pool.start(worker)

    def _show_analysis(self, analysis: QueryAnalysis) -> None:
        prediction = analysis.prediction
        self.last_prediction_ms = prediction.predicted_time_ms
        self.last_query_run_id = analysis.query_run_id
        self.last_analyzed_sql = prediction.sql_text
        prediction_text = f"{prediction.predicted_time_ms:.2f} мс"
        if abs(prediction.calibration_factor - 1.0) > 1e-12:
            prediction_text += (
                f"\nбазовый {prediction.uncalibrated_time_ms:.2f} мс"
            )
        self.predicted_value.setText(prediction_text)
        self.cost_value.setText(f"{prediction.estimated_total_cost:,.2f}")
        self.rows_value.setText(f"{prediction.estimated_plan_rows:,.0f}")
        self.node_value.setText(
            f"{prediction.root_node_type}\n{prediction.plan_node_count} узл."
        )
        recommendation = analysis.recommendation
        if recommendation is None:
            text = "Прогноз ниже заданного порога — индексный анализ не запускался."
        elif recommendation.action.kind is IndexActionKind.NOOP:
            text = (
                "NOOP — новый индекс не рекомендуется.\n"
                f"Оценка действия: {recommendation.predicted_reward:.4f}"
            )
        else:
            action = recommendation.action
            keys = ", ".join(f'"{name}"' for name in action.key_columns)
            ddl = f'CREATE INDEX ON "{action.schema_name}"."{action.table_name}" ({keys})'
            if action.include_columns:
                includes = ", ".join(
                    f'"{name}"' for name in action.include_columns
                )
                ddl += f" INCLUDE ({includes})"
            text = (
                f"{ddl};\nОценка улучшения: {recommendation.predicted_reward:.4f}\n"
                f"Рассмотрено кандидатов: {recommendation.candidate_count}"
            )
        strategy = analysis.strategy_prediction
        if strategy is not None:
            selected = strategy["strategy"]
            titles = {
                "NOOP": "ничего не менять",
                "CREATE_INDEX": "проверить создание индекса",
                "REWRITE_QUERY": "проверить переписывание SQL",
            }
            hints = {
                "NOOP": "Существенная оптимизация по известным стратегиям маловероятна.",
                "CREATE_INDEX": "Для фактической проверки используйте глубокий анализ индексов.",
                "REWRITE_QUERY": "Нажмите «Проверить переписывание SQL» для доказательства эквивалентности и замера.",
            }
            probabilities = strategy["probabilities"]
            probability_text = ", ".join(
                f"{name}={probabilities[name] * 100:.1f}%"
                for name in ("NOOP", "CREATE_INDEX", "REWRITE_QUERY")
            )
            text = (
                "Рекомендуемая стратегия: "
                f"{titles[selected]} ({probabilities[selected] * 100:.1f}%).\n"
                f"{hints[selected]}\n"
                f"Вероятности модели: {probability_text}\n\n"
                f"Предварительная индексная оценка:\n{text}"
            )
        text = (
            f"{text}\n\n"
            f"{_format_structural_recommendations(analysis.structural_recommendations)}"
        )
        self.recommendation_text.setPlainText(text)
        self._configure_structural_feedback(analysis.structural_recommendations)
        self.analyze_button.setEnabled(True)
        self.statusBar().showMessage(
            f"Анализ завершён · запись #{analysis.query_run_id or 'не сохранена'}"
        )

    def _reset_structural_feedback(self) -> None:
        self.last_structural_recommendations = ()
        self.structural_feedback_state.clear()
        self.structural_recommendation_combo.clear()
        self.structural_recommendation_combo.setEnabled(False)
        self.accept_structural_button.setEnabled(False)
        self.reject_structural_button.setEnabled(False)
        self.save_structural_measurement_button.setEnabled(False)
        self.structural_feedback_note.clear()
        self.structural_feedback_status.setText(
            "Сохраните анализ в историю, чтобы оставить обратную связь."
        )

    def _configure_structural_feedback(self, recommendations) -> None:
        self._reset_structural_feedback()
        self.last_structural_recommendations = tuple(recommendations)
        persisted = [
            item for item in recommendations if item.recommendation_id is not None
        ]
        for position, item in enumerate(persisted, start=1):
            self.structural_recommendation_combo.addItem(
                f"{position}. [{item.category.value}] {item.title}",
                item.recommendation_id,
            )
            self.structural_feedback_state[item.recommendation_id] = "proposed"
        if persisted:
            self.structural_recommendation_combo.setEnabled(True)
            self.structural_feedback_status.setText(
                "Выберите рекомендацию и зафиксируйте решение."
            )
            self._sync_structural_feedback_controls()
        elif recommendations:
            self.structural_feedback_status.setText(
                "Рекомендации не сохранены: включите «Сохранять в историю pqo»."
            )
        else:
            self.structural_feedback_status.setText(
                "Для этого запроса структурных рекомендаций нет."
            )

    def _selected_structural_recommendation_id(self) -> int | None:
        value = self.structural_recommendation_combo.currentData()
        return int(value) if value is not None else None

    def _sync_structural_feedback_controls(self) -> None:
        recommendation_id = self._selected_structural_recommendation_id()
        available = recommendation_id is not None
        status = self.structural_feedback_state.get(recommendation_id, "proposed")
        self.accept_structural_button.setEnabled(available)
        self.reject_structural_button.setEnabled(available)
        self.save_structural_measurement_button.setEnabled(
            available and status == RecommendationDecision.ACCEPTED.value
        )
        if available:
            labels = {
                "proposed": "Решение ещё не принято.",
                "accepted": "Рекомендация принята; можно сохранить независимый замер.",
                "rejected": "Рекомендация отклонена.",
            }
            self.structural_feedback_status.setText(labels[status])

    def _submit_structural_decision(self, decision: RecommendationDecision) -> None:
        recommendation_id = self._selected_structural_recommendation_id()
        if recommendation_id is None:
            return
        self.accept_structural_button.setEnabled(False)
        self.reject_structural_button.setEnabled(False)
        self.save_structural_measurement_button.setEnabled(False)
        self.structural_feedback_status.setText("Сохраняю решение…")
        worker = Worker(
            record_recommendation_decision,
            recommendation_id,
            decision,
            self.structural_feedback_note.text(),
            self._database_settings(),
        )
        worker.signals.succeeded.connect(self._structural_decision_saved)
        worker.signals.failed.connect(self._structural_feedback_failed)
        self.thread_pool.start(worker)

    def _structural_decision_saved(self, update) -> None:
        self.structural_feedback_state[update.recommendation_id] = update.status
        self._sync_structural_feedback_controls()
        self.statusBar().showMessage(
            f"Обратная связь по рекомендации #{update.recommendation_id} сохранена"
        )

    def _submit_structural_measurement(self) -> None:
        recommendation_id = self._selected_structural_recommendation_id()
        before = self.structural_before_spin.value()
        after = self.structural_after_spin.value()
        if recommendation_id is None:
            return
        if before <= 0:
            QMessageBox.warning(
                self,
                "Некорректный замер",
                "Фактическое время до оптимизации должно быть больше нуля.",
            )
            return
        self.save_structural_measurement_button.setEnabled(False)
        self.structural_feedback_status.setText("Сохраняю фактический результат…")
        worker = Worker(
            record_recommendation_measurement,
            recommendation_id,
            before,
            after,
            self.structural_feedback_note.text(),
            self._database_settings(),
        )
        worker.signals.succeeded.connect(self._structural_measurement_saved)
        worker.signals.failed.connect(self._structural_feedback_failed)
        self.thread_pool.start(worker)

    def _structural_measurement_saved(self, update) -> None:
        outcome_labels = {
            "improved": "улучшение",
            "unchanged": "без существенного изменения",
            "regressed": "ухудшение",
        }
        ratio = update.measured_improvement_ratio or 0.0
        outcome = outcome_labels[update.measurement_outcome.value]
        self.structural_feedback_status.setText(
            f"Замер сохранён: {outcome}, эффект {ratio:+.1%}."
        )
        self.save_structural_measurement_button.setEnabled(True)
        self.statusBar().showMessage(
            f"Фактический эффект рекомендации #{update.recommendation_id} сохранён"
        )

    def _structural_feedback_failed(self, message: str) -> None:
        self._sync_structural_feedback_controls()
        self._task_failed(message)

    def _start_sequential_analysis(self) -> None:
        sql_text = self.sql_editor.toPlainText().strip()
        if not sql_text:
            QMessageBox.warning(self, "Нет SQL", "Введите SQL-запрос.")
            return
        model_path = Path(self.sequential_dqn_model_edit.text())
        if not model_path.is_file():
            QMessageBox.warning(
                self,
                "Модель не найдена",
                "Проверьте путь к последовательной DQN в настройках.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Запустить глубокий анализ?",
            "PostgreSQL реально выполнит SELECT через EXPLAIN ANALYZE и временно "
            "создаст до двух пробных индексов. Все изменения будут полностью "
            "откачены. Для тяжёлого запроса операция может занять время.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.deep_analyze_button.setEnabled(False)
        self.recommendation_text.setPlainText(
            "Проверяю кандидаты в изолированной транзакции…"
        )
        self.statusBar().showMessage("Глубокий анализ индексов…")
        worker = Worker(
            recommend_sequential_indexes,
            sql_text,
            model_path,
            self._database_settings(),
            max_steps=2,
            storage_budget_bytes=64 * 1024 * 1024,
            repetitions=1,
            minimum_baseline_time_ms=self.threshold_spin.value(),
            minimum_absolute_improvement_ms=5.0,
            persist=self.persist_check.isChecked(),
            query_run_id=(
                self.last_query_run_id
                if self.last_query_run_id is not None
                and self.last_analyzed_sql == sql_text
                else None
            ),
        )
        worker.signals.succeeded.connect(self._show_sequential_analysis)
        worker.signals.failed.connect(
            lambda message: self._task_failed(message, self.deep_analyze_button)
        )
        self.thread_pool.start(worker)

    def _show_sequential_analysis(
        self, plan: SequentialRecommendationPlan
    ) -> None:
        self.deep_analyze_button.setEnabled(True)
        if plan.query_run_id is not None:
            self.last_query_run_id = plan.query_run_id
            self.last_analyzed_sql = self.sql_editor.toPlainText().strip()
        prediction_text = (
            "—"
            if self.last_prediction_ms is None
            else f"ML-прогноз {self.last_prediction_ms:.2f} мс"
        )
        self.predicted_value.setText(
            f"{prediction_text}\nфакт {plan.baseline_time_ms:.2f} мс"
        )
        if not plan.steps:
            reasons = {
                "model_stop": "модель не нашла достаточно надёжного улучшения",
                "no_candidates": "для запроса не сформированы индексные кандидаты",
                "below_runtime_threshold": (
                    "фактическое время ниже порога рекомендации "
                    f"{plan.minimum_baseline_time_ms:.1f} мс"
                ),
                "index_not_used": "PostgreSQL не использовал пробный индекс",
                "non_positive_reward": "пробный индекс не дал положительной награды",
                "absolute_gain_too_small": (
                    "абсолютная экономия меньше "
                    f"{plan.minimum_absolute_improvement_ms:.1f} мс"
                ),
                "budget_exceeded": "кандидат превысил лимит 64 МиБ",
            }
            reason = reasons.get(plan.terminal_reason, plan.terminal_reason)
            self.recommendation_text.setPlainText(
                "Создавать новые индексы не рекомендуется.\n"
                f"Причина: {reason}.\n"
                f"Фактическое исходное время: {plan.baseline_time_ms:.2f} мс."
            )
        else:
            lines = [
                f"Проверенный план: {len(plan.steps)} индекс(а/ов)",
                (
                    f"Фактическое время: {plan.baseline_time_ms:.2f} → "
                    f"{plan.final_time_ms:.2f} мс "
                    f"({plan.measured_improvement_ratio:+.1%})"
                ),
            ]
            for number, step in enumerate(plan.steps, start=1):
                lines.extend(
                    (
                        "",
                        f"{number}. {_format_index_action(step.action)}",
                        (
                            f"   Q={step.predicted_q:.4f}; reward="
                            f"{step.measured_reward:+.4f}; размер="
                            f"{step.index_size_bytes / 1024 / 1024:.2f} МиБ"
                        ),
                    )
                )
            lines.extend(
                (
                    "",
                    "Индексы были только проверены и откачены; команда выше "
                    "сама базу не изменяет.",
                )
            )
            self.recommendation_text.setPlainText("\n".join(lines))
        saved = (
            f"; запись #{plan.query_run_id}, глубокий результат "
            f"#{plan.sequential_analysis_id} сохранён"
            if plan.sequential_analysis_id is not None
            else ""
        )
        self.statusBar().showMessage(
            f"Глубокий анализ завершён; изменения откачены{saved}"
        )

    def _start_rewrite_analysis(self) -> None:
        sql_text = self.sql_editor.toPlainText().strip()
        if not sql_text:
            QMessageBox.warning(self, "Нет SQL", "Введите SQL-запрос.")
            return
        answer = QMessageBox.question(
            self,
            "Проверить переписывание SQL?",
            "PostgreSQL реально выполнит исходный и переписанные SELECT через "
            "EXPLAIN ANALYZE. Результаты будут сравнены в транзакции только для "
            "чтения. Для тяжёлого запроса операция может занять время.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.rewrite_analyze_button.setEnabled(False)
        self.recommendation_text.setPlainText(
            "Формирую варианты, проверяю эквивалентность и измеряю время…"
        )
        self.statusBar().showMessage("Проверка переписывания SQL…")
        worker = Worker(
            evaluate_sql_rewrites,
            sql_text,
            self._database_settings(),
            repetitions=3,
            minimum_baseline_time_ms=self.threshold_spin.value(),
            minimum_absolute_improvement_ms=5.0,
            minimum_improvement_ratio=0.05,
        )
        worker.signals.succeeded.connect(self._show_rewrite_analysis)
        worker.signals.failed.connect(
            lambda message: self._task_failed(message, self.rewrite_analyze_button)
        )
        self.thread_pool.start(worker)

    def _show_rewrite_analysis(self, plan: SQLRewritePlan) -> None:
        self.rewrite_analyze_button.setEnabled(True)
        if plan.recommended is not None:
            result = plan.recommended
            self.recommendation_text.setPlainText(
                "Проверенное переписывание SQL:\n"
                f"Правило: {result.candidate.title}\n"
                f"Фактическое время: {result.baseline_time_ms:.2f} → "
                f"{result.rewritten_time_ms:.2f} мс "
                f"({result.improvement_ratio:+.1%})\n\n"
                f"{result.candidate.sql_text}\n\n"
                "Эквивалентность результатов подтверждена через EXCEPT ALL."
            )
            self.statusBar().showMessage(
                "Найдено и измерено безопасное переписывание SQL"
            )
            return

        reasons = {
            "no_candidates": "для этого SQL нет поддерживаемых безопасных правил",
            "below_runtime_threshold": (
                "фактическое время запроса ниже установленного порога"
            ),
            "no_measured_improvement": (
                "варианты эквивалентны, но не дали достаточного ускорения"
            ),
        }
        lines = [
            "Переписывать SQL не рекомендуется.",
            f"Причина: {reasons.get(plan.terminal_reason, plan.terminal_reason)}.",
        ]
        for evaluation in plan.evaluations:
            if evaluation.equivalent and evaluation.baseline_time_ms is not None:
                lines.append(
                    f"{evaluation.candidate.title}: "
                    f"{evaluation.baseline_time_ms:.2f} → "
                    f"{evaluation.rewritten_time_ms:.2f} мс "
                    f"({evaluation.improvement_ratio:+.1%})."
                )
            else:
                lines.append(
                    f"{evaluation.candidate.title}: "
                    f"{evaluation.rejection_reason or 'отклонено'}."
                )
        self.recommendation_text.setPlainText("\n".join(lines))
        self.statusBar().showMessage("Безопасное ускоряющее переписывание не найдено")

    def _refresh_history(self) -> None:
        self.statusBar().showMessage("Загрузка истории…")
        worker = Worker(load_analysis_history, self._database_settings(), limit=200)
        worker.signals.succeeded.connect(self._show_history)
        worker.signals.failed.connect(self._task_failed)
        self.thread_pool.start(worker)

    def _show_history(self, records) -> None:
        self.history_records = list(records)
        self.history_export_button.setEnabled(bool(records))
        self.history_table.setRowCount(len(records))
        for row_index, record in enumerate(records):
            sql_preview = " ".join(record.sql_text.split())
            recommendation = record.recommended_table or "—"
            if record.recommended_columns:
                recommendation += f" ({', '.join(record.recommended_columns)})"
            if record.sequential_steps:
                recommendation = "\n".join(
                    step.proposed_ddl for step in record.sequential_steps
                )
            measured_time = "—"
            if record.measured_baseline_time_ms is not None:
                measured_time = (
                    f"{record.measured_baseline_time_ms:.2f} → "
                    f"{record.measured_final_time_ms:.2f} мс"
                )
            measured_gain = (
                "—"
                if record.measured_improvement_ratio is None
                else f"{record.measured_improvement_ratio:+.1%}"
            )
            score = (
                "—"
                if record.predicted_reward is None
                else f"{record.predicted_reward:.4f}"
            )
            if record.sequential_steps:
                score = "; ".join(
                    f"Q={step.predicted_q:.4f}, reward={step.measured_reward:+.4f}"
                    for step in record.sequential_steps
                )
            structural_feedback = "—"
            if record.structural_feedback:
                feedback_lines = []
                outcome_labels = {
                    "improved": "улучшение",
                    "unchanged": "без изменения",
                    "regressed": "ухудшение",
                }
                status_labels = {
                    "proposed": "предложено",
                    "accepted": "принято",
                    "rejected": "отклонено",
                }
                for feedback in record.structural_feedback:
                    line = (
                        f"{feedback.rule_id}: "
                        f"{status_labels.get(feedback.status, feedback.status)}"
                    )
                    if feedback.measurement_outcome:
                        line += (
                            f", {outcome_labels.get(feedback.measurement_outcome, feedback.measurement_outcome)} "
                            f"{feedback.measured_improvement_ratio:+.1%}"
                        )
                    feedback_lines.append(line)
                structural_feedback = "\n".join(feedback_lines)
            values = (
                (
                    str(record.query_run_id)
                    if record.sequential_analysis_id is None
                    else f"{record.query_run_id}/{record.sequential_analysis_id}"
                ),
                record.started_at.astimezone().strftime("%d.%m.%Y %H:%M:%S"),
                sql_preview,
                "—" if record.predicted_time_ms is None else f"{record.predicted_time_ms:.2f} мс",
                record.root_node_type or "—",
                recommendation,
                score,
                measured_time,
                measured_gain,
                record.sequential_terminal_reason or "—",
                structural_feedback,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(record.sql_text if column == 2 else value)
                self.history_table.setItem(row_index, column, item)
        self.statusBar().showMessage(f"Загружено записей: {len(records)}")

    def _export_history(self) -> None:
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Экспорт истории",
            str(ARTIFACT_ROOT / "analysis_history.csv"),
            "CSV (*.csv);;JSON (*.json)",
        )
        if not selected:
            return
        try:
            export_history_records(self.history_records, selected)
        except Exception as exc:
            self._task_failed(f"{type(exc).__name__}: {exc}")
        else:
            self.statusBar().showMessage(f"История экспортирована: {selected}")

    def _load_experiments(self) -> None:
        xgb_model = Path(self.xgb_model_edit.text())
        dqn_model = Path(self.dqn_model_edit.text())
        dqn_stress = dqn_model.parent / "stress_metrics.json"
        if not dqn_stress.is_file():
            dqn_stress = DEFAULT_DQN_STRESS_METRICS
        self.load_experiments_button.setEnabled(False)
        self.statusBar().showMessage("Расчёт эксперимента на уникальных SQL…")
        worker = Worker(
            build_experiment_report,
            xgb_model.parent / "metrics.json",
            dqn_model.parent / "metrics.json",
            dqn_stress,
            xgb_model,
            Path(self.xgb_dataset_edit.text()),
        )
        worker.signals.succeeded.connect(self._show_experiment_report)
        worker.signals.failed.connect(
            lambda message: self._task_failed(message, self.load_experiments_button)
        )
        self.thread_pool.start(worker)

    def _show_experiment_report(self, report: ExperimentReport) -> None:
        self.experiment_report = report
        self.load_experiments_button.setEnabled(True)
        self.experiment_export_button.setEnabled(True)
        parameter = report.xgboost_metrics["parameter_holdout"]
        stress = report.xgboost_metrics["unseen_template_stress"]
        dqn = report.dqn_metrics
        dqn_stress = report.dqn_stress_metrics
        self.xgb_parameter_value.setText(
            f"R² {parameter['r2']:.4f}\nMAE {parameter['mae_ms']:.2f} мс"
        )
        self.xgb_stress_value.setText(
            f"R² {stress['r2']:.4f}\nMAE {stress['mae_ms']:.2f} мс"
        )
        self.dqn_parameter_value.setText(
            f"Accuracy {dqn['recommendation_accuracy'] * 100:.1f}%\n"
            f"Regret {dqn['mean_regret']:.4f}"
        )
        self.dqn_stress_value.setText(
            f"Accuracy {dqn_stress['recommendation_accuracy'] * 100:.1f}%\n"
            f"Regret {dqn_stress['mean_regret']:.4f}"
        )
        self.prediction_chart.set_points(report.prediction_points)
        self.accuracy_chart.set_bars(
            (
                ("DQN", dqn["recommendation_accuracy"], "#2563eb"),
                ("Случ.", dqn["random_accuracy"], "#64748b"),
                ("NOOP", dqn["noop_accuracy"], "#0f766e"),
                ("DQN stress", dqn_stress["recommendation_accuracy"], "#8b5cf6"),
                ("Случ. stress", dqn_stress["random_accuracy"], "#475569"),
                ("NOOP stress", dqn_stress["noop_accuracy"], "#115e59"),
            )
        )
        rows = [
            ("Точек «факт → прогноз»", len(report.prediction_points)),
            ("MAE на полном датасете, мс", f"{report.dataset_mae_ms:.4f}"),
            ("RMSE на полном датасете, мс", f"{report.dataset_rmse_ms:.4f}"),
            ("R² на полном датасете", f"{report.dataset_r2:.6f}"),
            ("XGBoost within 20%, parameter", f"{parameter['within_20_percent']:.2f}%"),
            ("DQN accuracy / random", f"{dqn['recommendation_accuracy']:.4f} / {dqn['random_accuracy']:.4f}"),
        ]
        control_directory = PROJECT_ROOT / "models" / "control"
        try:
            xgb_control = json.loads(
                (control_directory / "xgboost_control_metrics.json").read_text(encoding="utf-8")
            )
            dqn_control = json.loads(
                (control_directory / "dqn_control_metrics.json").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        else:
            rows.extend(
                [
                    ("Production control XGBoost R²", f"{xgb_control['r2']:.6f}"),
                    ("Production control XGBoost MAE, мс", f"{xgb_control['mae_ms']:.4f}"),
                    ("Production control DQN accuracy", f"{dqn_control['recommendation_accuracy']:.4f}"),
                    ("Production control DQN regret", f"{dqn_control['mean_regret']:.4f}"),
                ]
            )
        self.experiment_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, value in enumerate(row):
                self.experiment_table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self.statusBar().showMessage(
            f"Эксперимент загружен: {len(report.prediction_points)} уникальных SQL"
        )

    def _export_experiments(self) -> None:
        if self.experiment_report is None:
            return
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Экспорт эксперимента",
            str(ARTIFACT_ROOT / "experiment_report.json"),
            "JSON (*.json);;CSV (*.csv)",
        )
        if not selected:
            return
        try:
            export_experiment_report(self.experiment_report, selected)
        except Exception as exc:
            self._task_failed(f"{type(exc).__name__}: {exc}")
        else:
            self.statusBar().showMessage(f"Эксперимент экспортирован: {selected}")

    def _test_connection(self) -> None:
        self.statusBar().showMessage("Проверка PostgreSQL…")
        worker = Worker(check_database_connection, self._database_settings())
        worker.signals.succeeded.connect(
            lambda message: QMessageBox.information(self, "Подключение работает", message)
        )
        worker.signals.failed.connect(self._task_failed)
        self.thread_pool.start(worker)

    def _save_settings(self) -> None:
        self.preferences.setValue("db_host", self.host_edit.text().strip())
        self.preferences.setValue("db_port", self.port_spin.value())
        self.preferences.setValue("db_name", self.database_edit.text().strip())
        self.preferences.setValue("db_user", self.user_edit.text().strip())
        self.preferences.setValue(
            "allowed_schemas", self.allowed_schemas_edit.text().strip()
        )
        self.preferences.setValue("xgb_model", self.xgb_model_edit.text().strip())
        self.preferences.setValue("dqn_model", self.dqn_model_edit.text().strip())
        self.preferences.setValue(
            "sequential_dqn_model", self.sequential_dqn_model_edit.text().strip()
        )
        self.preferences.setValue(
            "strategy_model", self.strategy_model_edit.text().strip()
        )
        self.preferences.setValue("threshold_ms", self.threshold_spin.value())
        self.preferences.setValue("persist_analysis", self.persist_check.isChecked())
        self.preferences.setValue(
            "use_calibration", self.use_calibration_check.isChecked()
        )
        self.preferences.sync()
        self._refresh_calibration_status()
        self.statusBar().showMessage("Настройки сохранены (пароль не сохранялся)")

    def _start_xgb_training(self) -> None:
        output_directory = Path(self.xgb_output_edit.text())
        self.xgb_train_button.setEnabled(False)
        self.promote_xgb_button.setEnabled(False)
        self.xgb_progress.setRange(0, 0)
        self.training_log.setPlainText("Обучение XGBoost…")
        worker = Worker(
            train_xgboost,
            Path(self.xgb_dataset_edit.text()),
            output_directory,
            tune=self.xgb_tune_check.isChecked(),
        )
        worker.signals.succeeded.connect(
            lambda metrics: self._training_finished(
                "xgboost",
                metrics,
                output_directory,
                self.xgb_train_button,
                self.xgb_progress,
                self.promote_xgb_button,
            )
        )
        worker.signals.failed.connect(
            lambda message: self._training_failed(
                message, self.xgb_train_button, self.xgb_progress
            )
        )
        self.thread_pool.start(worker)

    def _start_dqn_training(self) -> None:
        output_directory = Path(self.dqn_output_edit.text())
        self.dqn_train_button.setEnabled(False)
        self.promote_dqn_button.setEnabled(False)
        self.dqn_progress.setRange(0, 0)
        self.training_log.setPlainText("Обучение DQN…")
        worker = Worker(
            train_dqn,
            Path(self.dqn_dataset_edit.text()),
            output_directory,
            epochs=self.dqn_epochs_spin.value(),
            batch_size=128,
            learning_rate=0.001,
            split_mode=self.dqn_split_combo.currentData(),
            ranking_weight=0.10,
        )
        worker.signals.succeeded.connect(
            lambda metrics: self._training_finished(
                "dqn",
                metrics,
                output_directory,
                self.dqn_train_button,
                self.dqn_progress,
                self.promote_dqn_button,
            )
        )
        worker.signals.failed.connect(
            lambda message: self._training_failed(
                message, self.dqn_train_button, self.dqn_progress
            )
        )
        self.thread_pool.start(worker)

    def _training_finished(
        self,
        model_kind: str,
        metrics,
        output_directory: Path,
        button: QPushButton,
        progress: QProgressBar,
        promote_button: QPushButton,
    ) -> None:
        button.setEnabled(True)
        progress.setRange(0, 100)
        progress.setValue(100)
        metrics_dict = asdict(metrics)
        self.training_log.setPlainText(
            json.dumps(metrics_dict, ensure_ascii=False, indent=2)
        )
        primary_directory = (
            DEFAULT_XGB_MODEL.parent if model_kind == "xgboost" else DEFAULT_DQN_MODEL.parent
        )
        try:
            baseline = json.loads(
                (primary_directory / "metrics.json").read_text(encoding="utf-8")
            )
            decision = assess_candidate(model_kind, metrics_dict, baseline)
        except Exception as exc:
            decision_text = f"Не удалось сравнить модели: {exc}"
            promote_button.setEnabled(False)
        else:
            decision_text = decision.explanation
            promote_button.setEnabled(decision.allowed)
            promote_button.setToolTip(decision.explanation)
            self.candidate_directories[model_kind] = output_directory
        self.training_log.appendPlainText(f"\nВердикт: {decision_text}")
        self.statusBar().showMessage("Обучение завершено; кандидат сравнён с основной моделью")

    def _training_failed(
        self, message: str, button: QPushButton, progress: QProgressBar
    ) -> None:
        progress.setRange(0, 100)
        progress.setValue(0)
        self._task_failed(message, button)

    def _promote_candidate(self, model_kind: str) -> None:
        candidate = self.candidate_directories.get(model_kind)
        if candidate is None:
            return
        primary = (
            DEFAULT_XGB_MODEL.parent if model_kind == "xgboost" else DEFAULT_DQN_MODEL.parent
        )
        try:
            decision = promote_candidate_model(model_kind, candidate, primary)
        except Exception as exc:
            self._task_failed(f"{type(exc).__name__}: {exc}")
            return
        if not decision.allowed:
            QMessageBox.warning(self, "Замена запрещена", decision.explanation)
            return
        if model_kind == "xgboost":
            self.xgb_model_edit.setText(str(DEFAULT_XGB_MODEL))
            self.promote_xgb_button.setEnabled(False)
        else:
            self.dqn_model_edit.setText(str(DEFAULT_DQN_MODEL))
            self.promote_dqn_button.setEnabled(False)
        self._save_settings()
        QMessageBox.information(
            self,
            "Модель обновлена",
            f"{decision.explanation}\nПредыдущая версия сохранена в {primary / 'previous'}.",
        )

    def _task_failed(self, message: str, button: QPushButton | None = None) -> None:
        if button is not None:
            button.setEnabled(True)
        self.statusBar().showMessage("Операция завершилась ошибкой")
        QMessageBox.critical(self, "Ошибка", message)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #111827; color: #e5e7eb; }
            QTabWidget::pane { border: 1px solid #334155; }
            QTabBar::tab { background: #1f2937; padding: 10px 22px; }
            QTabBar::tab:selected { background: #2563eb; color: white; }
            QGroupBox { border: 1px solid #334155; border-radius: 7px;
                        margin-top: 12px; padding-top: 10px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QTableWidget {
                background: #0f172a; border: 1px solid #475569; border-radius: 5px;
                padding: 7px; selection-background-color: #2563eb;
            }
            QPushButton { background: #334155; border: 0; border-radius: 5px;
                          padding: 8px 15px; }
            QPushButton:hover { background: #475569; }
            QPushButton:disabled { color: #64748b; }
            QPushButton#primaryButton { background: #2563eb; color: white; font-weight: 600; }
            QPushButton#primaryButton:hover { background: #1d4ed8; }
            QLabel#pageTitle { font-size: 22px; font-weight: 700; color: white; }
            QLabel#metricValue { font-size: 18px; font-weight: 700; color: #60a5fa; }
            QLabel#mutedLabel { color: #94a3b8; }
            QHeaderView::section { background: #1f2937; padding: 7px; border: 0; }
            """
        )


def main() -> int:
    smoke_test = "--smoke-test" in sys.argv
    if smoke_test:
        sys.argv.remove("--smoke-test")
    application = QApplication(sys.argv)
    application.setApplicationName("Predictive Query Optimization")
    application.setOrganizationName("PQO")
    if APP_ICON.is_file():
        application.setWindowIcon(QIcon(str(APP_ICON)))
    if smoke_test:
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        smoke_log = ARTIFACT_ROOT / "smoke-test.log"
        smoke_log.write_text("Starting resource check...", encoding="utf-8")
        required = (
            DEFAULT_XGB_MODEL,
            DEFAULT_DQN_MODEL,
            DEFAULT_SEQUENTIAL_DQN_MODEL,
            DEFAULT_STRATEGY_MODEL,
            DEFAULT_DATASET,
        )
        if not all(path.is_file() for path in required):
            missing = [str(path) for path in required if not path.is_file()]
            smoke_log.write_text(
                "Missing packaged resources:\n" + "\n".join(missing),
                encoding="utf-8",
            )
            return 2
        try:
            smoke_log.write_text("Loading Python dependencies...", encoding="utf-8")
            import joblib
            import torch

            from .dqn import dqn_action_encoding_version
            from .sql_features import extract_sql_features

            smoke_log.write_text("Checking PostgreSQL SQL parser...", encoding="utf-8")
            sql_features = extract_sql_features(
                "SELECT f.flight_id FROM aviation.flights AS f "
                "WHERE f.departure_airport = 'MSQ'"
            )
            if sql_features.where_condition_count != 1:
                smoke_log.write_text(
                    "PostgreSQL SQLGlot smoke query produced invalid features.",
                    encoding="utf-8",
                )
                return 3
            smoke_log.write_text("Loading XGBoost artifact...", encoding="utf-8")
            xgboost_artifact = joblib.load(DEFAULT_XGB_MODEL)
            if "pipeline" not in xgboost_artifact:
                smoke_log.write_text(
                    "XGBoost artifact has no pipeline.", encoding="utf-8"
                )
                return 3
            smoke_log.write_text("Loading DQN artifact...", encoding="utf-8")
            dqn_action_encoding_version(DEFAULT_DQN_MODEL)
            strategy_artifact = joblib.load(DEFAULT_STRATEGY_MODEL)
            if "classifier" not in strategy_artifact:
                smoke_log.write_text(
                    "Strategy artifact has no classifier.", encoding="utf-8"
                )
                return 3
            sequential_artifact = torch.load(
                DEFAULT_SEQUENTIAL_DQN_MODEL,
                map_location="cpu",
                weights_only=True,
            )
            if sequential_artifact.get("training_kind") != (
                "offline-sequential-bellman-dqn"
            ):
                smoke_log.write_text(
                    "Sequential DQN artifact has an invalid type.", encoding="utf-8"
                )
                return 3
            smoke_log.write_text("Constructing the main window...", encoding="utf-8")
            MainWindow()
        except Exception:
            smoke_log.write_text(traceback.format_exc(), encoding="utf-8")
            return 3
        smoke_log.write_text(
            "OK: GUI, SQL parser, XGBoost, DQN, sequential DQN and strategy selector loaded.",
            encoding="utf-8",
        )
        return 0
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
