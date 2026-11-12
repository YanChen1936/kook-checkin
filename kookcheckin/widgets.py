"""自绘控件：胶囊开关、红绿灯、倒计时圆环、信息行。"""

from __future__ import annotations

import math

from PySide6.QtCore import QEasingCurve, QPointF, QPropertyAnimation, QRectF, Qt, Property, Signal
from PySide6.QtGui import QColor, QConicalGradient, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QWidget

TEXT = "#F5F5F7"
TEXT_DIM = "rgba(255,255,255,0.55)"
GREEN = "#30D158"
AMBER = "#FF9F0A"
RED = "#FF453A"
ACCENT = "#62E08C"

FONT_FAMILY = "Microsoft YaHei UI"


def font(size: float, weight: int = 400) -> QFont:
    f = QFont(FONT_FAMILY)
    f.setPointSizeF(size)
    f.setWeight(QFont.Weight(weight))
    return f


def add_shadow(widget: QWidget, blur: int = 40, dy: int = 8, alpha: int = 150) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    effect.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(effect)


class Switch(QWidget):
    """macOS 胶囊开关：点击切换 + 位置动画 + 关闭态灰色。"""

    toggled = Signal(bool)

    def __init__(self, on: bool = False, parent=None):
        super().__init__(parent)
        self.setFixedSize(46, 28)
        self.setCursor(Qt.PointingHandCursor)
        self._on = on
        self._pos = 1.0 if on else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    # knob 位置 0..1
    def _get_knob(self) -> float:
        return self._pos

    def _set_knob(self, value: float) -> None:
        self._pos = value
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def is_on(self) -> bool:
        return self._on

    def set_on(self, on: bool, animate: bool = True) -> None:
        on = bool(on)
        if on == self._on and not animate:
            self._pos = 1.0 if on else 0.0
            self.update()
            return
        self._on = on
        target = 1.0 if on else 0.0
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self._pos)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._pos = target
            self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.toggled.emit(not self._on)
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(0, 0, self.width(), self.height())
        radius = rect.height() / 2

        track = QColor(GREEN if self._on else "rgba(255,255,255,0.16)")
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(rect, radius, radius)

        knob_d = self.height() - 6
        x = 3 + self._pos * (self.width() - knob_d - 6)
        shadow = QColor(0, 0, 0, 60)
        p.setBrush(shadow)
        p.drawEllipse(QRectF(x, 4.5, knob_d, knob_d))
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QRectF(x, 3, knob_d, knob_d))
        p.end()


class TrafficLight(QWidget):
    """红黄绿三颗圆点：关闭/最小化/置顶，悬停显示符号。"""

    actionTriggered = Signal(str)  # close / min / pin

    COLORS = ("#FF5F57", "#FEBC2E", "#28C840")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(56, 16)
        self.setMouseTracking(True)
        self._hover = -1
        self.setCursor(Qt.PointingHandCursor)

    def _index_at(self, pos) -> int:
        step = 18
        for i in range(3):
            cx = 8 + i * step
            if math.hypot(pos.x() - cx, pos.y() - 8) <= 7:
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:
        idx = self._index_at(event.position())
        if idx != self._hover:
            self._hover = idx
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        idx = self._index_at(event.position())
        if idx >= 0:
            self.actionTriggered.emit(("close", "min", "pin")[idx])
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        for i, color in enumerate(self.COLORS):
            cx = 8 + i * 18
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawEllipse(QPointF(cx, 8), 6, 6)
            if self._hover == i:
                p.setPen(QPen(QColor(60, 30, 20 if i == 0 else 40), 1.4))
                p.setBrush(Qt.NoBrush)
                if i == 0:
                    p.drawLine(QPointF(cx - 2.2, 5.8), QPointF(cx + 2.2, 10.2))
                    p.drawLine(QPointF(cx - 2.2, 10.2), QPointF(cx + 2.2, 5.8))
                elif i == 1:
                    p.drawLine(QPointF(cx - 2.6, 8), QPointF(cx + 2.6, 8))
                else:
                    p.drawLine(QPointF(cx - 2.2, 8), QPointF(cx + 2.2, 8))
                    p.drawLine(QPointF(cx, 5.8), QPointF(cx, 10.2))
        p.end()


class Ring(QWidget):
    """倒计时圆环：进度 + 中央时间 + 状态文案。"""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(228, 228)
        self._progress = 0.0
        self._time_text = "--:--"
        self._caption = "已暂停"
        self._state = "stopped"

    def set_progress(self, value: float) -> None:
        self._progress = max(0.0, min(1.0, float(value)))
        self.update()

    def set_time_text(self, text: str) -> None:
        if text != self._time_text:
            self._time_text = text
            self.update()

    def set_caption(self, text: str) -> None:
        if text != self._caption:
            self._caption = text
            self.update()

    def set_state(self, state: str) -> None:
        self._state = state
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        side = min(self.width(), self.height())
        margin = 14
        rect = QRectF((self.width() - side) / 2 + margin, (self.height() - side) / 2 + margin,
                      side - margin * 2, side - margin * 2)

        track_pen = QPen(QColor(255, 255, 255, 26), 11)
        track_pen.setCapStyle(Qt.RoundCap)
        p.setPen(track_pen)
        p.setBrush(Qt.NoBrush)
        p.drawArc(rect, 90 * 16, -360 * 16)

        if self._state == "error":
            color = QColor(RED)
        elif self._state == "running":
            color = QColor(ACCENT)
        else:
            color = QColor(255, 255, 255, 60)

        if self._progress > 0.001:
            gradient = QConicalGradient(rect.center(), 90)
            gradient.setColorAt(0.0, color)
            gradient.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 90))
            pen = QPen(gradient, 11)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            span = int(-360 * 16 * self._progress)
            p.drawArc(rect, 90 * 16, span)

        p.setPen(QColor(TEXT))
        p.setFont(font(27, QFont.Bold))
        p.drawText(rect.adjusted(0, -12, 0, -12), Qt.AlignCenter, self._time_text)

        p.setPen(QColor(255, 255, 255, 145))
        p.setFont(font(9.2, QFont.Medium))
        p.drawText(rect.adjusted(0, 46, 0, 46), Qt.AlignCenter, self._caption)
        p.end()


class InfoRow(QWidget):
    """左标题右值的一行信息。"""

    def __init__(self, title: str, value: str = "—", parent=None):
        super().__init__(parent)
        self.setFixedHeight(30)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self._title = QLabel(title)
        self._title.setFont(font(9.5, QFont.Medium))
        self._title.setStyleSheet(f"color: {TEXT_DIM}; background: transparent;")
        self._value = QLabel(value)
        self._value.setFont(font(9.8, QFont.DemiBold))
        self._value.setStyleSheet(f"color: {TEXT}; background: transparent;")
        self._value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self._title)
        layout.addStretch(1)
        layout.addWidget(self._value)

    def set_value(self, value: str, color: str | None = None) -> None:
        self._value.setText(value)
        self._value.setStyleSheet(f"color: {color or TEXT}; background: transparent;")


def sep_line(parent=None) -> QWidget:
    line = QWidget(parent)
    line.setFixedHeight(1)
    line.setStyleSheet("background: rgba(255,255,255,0.08);")
    return line
