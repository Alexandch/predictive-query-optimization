"""Small dependency-free Qt charts used by the desktop dashboard."""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from .experiments import PredictionPoint


class ScatterChart(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._points: tuple[PredictionPoint, ...] = ()
        self.setMinimumHeight(240)

    def set_points(self, points: tuple[PredictionPoint, ...]) -> None:
        self._points = points
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0f172a"))
        area = QRectF(54, 18, max(10, self.width() - 76), max(10, self.height() - 58))
        painter.setPen(QPen(QColor("#475569"), 1))
        painter.drawLine(area.bottomLeft(), area.topLeft())
        painter.drawLine(area.bottomLeft(), area.bottomRight())
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(0, 0, self.width(), 18), Qt.AlignmentFlag.AlignCenter, "Факт → прогноз, log(1 + мс)")
        if not self._points:
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "Загрузите отчёт эксперимента")
            return

        maximum = max(
            1.0,
            *(math.log1p(max(point.actual_ms, point.predicted_ms)) for point in self._points),
        )
        painter.setPen(QPen(QColor("#64748b"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(area.bottomLeft(), area.topRight())
        painter.setPen(QPen(QColor(96, 165, 250, 150), 3))
        stride = max(1, len(self._points) // 600)
        for point in self._points[::stride]:
            x = area.left() + math.log1p(point.actual_ms) / maximum * area.width()
            y = area.bottom() - math.log1p(point.predicted_ms) / maximum * area.height()
            painter.drawPoint(QPointF(x, y))
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(6, int(area.top() + 12), "прогноз")
        painter.drawText(int(area.right() - 30), self.height() - 10, "факт")


class AccuracyBarChart(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._bars: tuple[tuple[str, float, str], ...] = ()
        self.setMinimumHeight(240)

    def set_bars(self, bars: tuple[tuple[str, float, str], ...]) -> None:
        self._bars = bars
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0f172a"))
        title_font = QFont(painter.font())
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor("#e5e7eb"))
        painter.drawText(QRectF(0, 8, self.width(), 22), Qt.AlignmentFlag.AlignCenter, "Точность выбора действия DQN")
        if not self._bars:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Загрузите отчёт эксперимента")
            return

        top, bottom = 42.0, float(self.height() - 38)
        available = max(20.0, bottom - top)
        slot = self.width() / len(self._bars)
        for index, (label, value, color) in enumerate(self._bars):
            width = min(72.0, slot * 0.58)
            height = max(1.0, min(1.0, value) * available)
            x = slot * index + (slot - width) / 2
            rectangle = QRectF(x, bottom - height, width, height)
            painter.fillRect(rectangle, QColor(color))
            painter.setPen(QColor("#e5e7eb"))
            painter.drawText(
                QRectF(slot * index, bottom - height - 24, slot, 20),
                Qt.AlignmentFlag.AlignCenter,
                f"{value * 100:.1f}%",
            )
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(
                QRectF(slot * index, bottom + 5, slot, 24),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )
