"""主窗口与托盘：无边框深色卡片 + 倒计时圆环 + 开关面板。"""

from __future__ import annotations

import sys
from datetime import datetime

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .config import Config, is_autostart_enabled, set_autostart
from .engine import CheckinEngine
from .logbus import LogBus
from .paths import APP_NAME, LOG_PATH, QUIT_FLAG, asset_path, is_frozen
from .widgets import (
    ACCENT,
    GREEN,
    RED,
    TEXT,
    TEXT_DIM,
    InfoRow,
    Ring,
    Switch,
    TrafficLight,
    add_shadow,
    font,
    sep_line,
)

CARD_TOP = "#1F2024"
CARD_BOTTOM = "#131417"


def _fmt_interval(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds % 3600 == 0:
        return f"每 {seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"每 {seconds // 60} 分钟"
    return f"每 {seconds} 秒"


def _fmt_countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class MainWindow(QWidget):
    closing = Signal()

    def __init__(self, cfg: Config, log: LogBus, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.log = log
        self._drag_pos: QPoint | None = None
        self._next_at: datetime | None = None
        self._last_ok: bool | None = None
        self._sending = False
        self._force_quit = False
        self._tray_hint_shown = False

        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(424, 646)
        icon_file = asset_path("kook_icon.ico")
        if icon_file.exists():
            self.setWindowIcon(QIcon(str(icon_file)))

        self.engine = CheckinEngine(cfg, log, parent=self)
        self._build_ui()
        self._setup_tray()
        self._wire_engine()

        self._tick = QTimer(self)
        self._tick.setInterval(500)
        self._tick.timeout.connect(self._poll)
        self._tick.start()

        # 升级/重装时用 `KOOK打卡.exe --quit` 让后台实例退出，不必去托盘点菜单
        self._quit_watch = QTimer(self)
        self._quit_watch.setInterval(2000)
        self._quit_watch.timeout.connect(self._check_quit_flag)
        self._quit_watch.start()

    # ------------------------------------------------------------ 布局
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)

        self.card = QWidget(self)
        self.card.setObjectName("card")
        add_shadow(self.card, blur=48, dy=12, alpha=170)
        outer.addWidget(self.card)

        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(24, 14, 24, 20)
        lay.setSpacing(0)

        # 顶部：红绿灯 + 标题 + 版本
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        self.lights = TrafficLight(self.card)
        self.lights.actionTriggered.connect(self._on_traffic)
        top.addWidget(self.lights, 0, Qt.AlignVCenter)
        top.addStretch(1)
        self.title = QLabel(APP_NAME)
        self.title.setFont(font(10.5, 600))
        self.title.setStyleSheet(f"color: {TEXT}; background: transparent;")
        top.addWidget(self.title)
        top.addStretch(1)
        self.ver = QLabel(f"v{__version__}")
        self.ver.setFont(font(8.5))
        self.ver.setStyleSheet(f"color: {TEXT_DIM}; background: transparent;")
        self.ver.setFixedWidth(56)
        self.ver.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(self.ver, 0, Qt.AlignVCenter)
        lay.addLayout(top)
        lay.addSpacing(10)

        # 频道胶囊
        chip_row = QHBoxLayout()
        chip_row.setContentsMargins(0, 0, 0, 0)
        self.chip = QLabel()
        self.chip.setTextFormat(Qt.RichText)
        self.chip.setCursor(Qt.PointingHandCursor)
        self.chip.setStyleSheet(
            "QLabel { background: rgba(255,255,255,0.07); border-radius: 11px;"
            f" padding: 4px 12px; color: {TEXT}; }}"
        )
        self.chip.setFont(font(9.2, 500))
        self.chip.mousePressEvent = self._open_channel  # type: ignore[assignment]
        chip_row.addStretch(1)
        chip_row.addWidget(self.chip)
        chip_row.addStretch(1)
        lay.addLayout(chip_row)
        lay.addSpacing(4)

        # 圆环
        ring_row = QHBoxLayout()
        ring_row.setContentsMargins(0, 0, 0, 0)
        self.ring = Ring(self.card)
        self.ring.clicked.connect(self._on_send_now)
        ring_row.addStretch(1)
        ring_row.addWidget(self.ring)
        ring_row.addStretch(1)
        lay.addLayout(ring_row)
        lay.addSpacing(2)

        # 信息行
        self.row_channel = InfoRow("打卡频道", self.cfg.channel_display or "未设置")
        self.row_interval = InfoRow("发送间隔", _fmt_interval(self.cfg.interval_seconds))
        self.row_last = InfoRow("上次发送", "—")
        for row in (self.row_channel, self.row_interval, self.row_last):
            lay.addWidget(row)

        lay.addSpacing(8)
        lay.addWidget(sep_line(self.card))
        lay.addSpacing(6)

        # 开关行
        self.sw_auto = Switch(self.engine.state in ("running", "starting"))
        self.sw_auto.toggled.connect(self._on_auto_toggled)
        self.sw_boot = Switch(is_autostart_enabled() and is_frozen())
        self.sw_boot.toggled.connect(self._on_boot_toggled)
        lay.addWidget(self._switch_row("自动打卡", self.sw_auto))
        lay.addWidget(self._switch_row("开机自启", self.sw_boot))
        if not is_frozen():
            self.sw_boot.setEnabled(False)
            self.sw_boot.setToolTip("源码运行时不开机自启，打包后生效")

        lay.addStretch(1)

        # 底部状态
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        self.status_dot = QLabel("●")
        self.status_dot.setFont(font(9))
        self.status_dot.setStyleSheet(f"color: {TEXT_DIM}; background: transparent;")
        footer.addWidget(self.status_dot, 0, Qt.AlignVCenter)
        self.status = QLabel("准备就绪")
        self.status.setFont(font(8.8))
        self.status.setStyleSheet(f"color: {TEXT_DIM}; background: transparent;")
        self.status.setWordWrap(False)
        footer.addWidget(self.status, 1)
        self.btn_now = QPushButton("立即打卡")
        self.btn_now.setCursor(Qt.PointingHandCursor)
        self.btn_now.setFixedHeight(26)
        self.btn_now.setFont(font(8.8, 600))
        self.btn_now.setStyleSheet(
            "QPushButton { background: rgba(98,224,140,0.16); color: #62E08C;"
            " border: 1px solid rgba(98,224,140,0.35); border-radius: 13px; padding: 0 12px; }"
            "QPushButton:hover { background: rgba(98,224,140,0.26); }"
            "QPushButton:pressed { background: rgba(98,224,140,0.34); }"
        )
        self.btn_now.clicked.connect(self._on_send_now)
        footer.addWidget(self.btn_now, 0, Qt.AlignVCenter)
        lay.addLayout(footer)

        self._refresh_chip()

    def _switch_row(self, title: str, switch: Switch) -> QWidget:
        row = QWidget(self.card)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(10)
        label = QLabel(title)
        label.setFont(font(9.8, 500))
        label.setStyleSheet(f"color: {TEXT}; background: transparent;")
        layout.addWidget(label)
        layout.addStretch(1)
        layout.addWidget(switch)
        return row

    def _refresh_chip(self) -> None:
        name = self.cfg.channel_display or "打卡频道"
        if not str(self.cfg.channel_url or "").strip():
            name = "未设置频道"
        self.chip.setText(
            f'<span style="color:{ACCENT};">●</span>&nbsp;&nbsp;{name}'
        )

    def start_engine(self, immediate: bool = True) -> None:
        """immediate=False 用于开机自启/打开窗口：续上次节拍，不重复打卡。"""
        self.engine.start(immediate=immediate)
        # 引擎已接管调度，开关要跟着亮起来（Switch 是自绘控件，用 set_on；不触发 toggled）
        if self.engine.state in ("running", "starting") and not self.sw_auto.is_on():
            self.sw_auto.set_on(True)

    # ------------------------------------------------------------ 托盘
    def _setup_tray(self) -> None:
        icon_file = asset_path("kook_icon.ico")
        icon = QIcon(str(icon_file)) if icon_file.exists() else self.style().standardIcon(
            QStyle.StandardPixmap.SP_ComputerIcon
        )
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        self.act_show = QAction("显示窗口", self)
        self.act_show.triggered.connect(self._show_from_tray)
        self.act_now = QAction("立即打卡", self)
        self.act_now.triggered.connect(self._on_send_now)
        self.act_auto = QAction("开启打卡", self, checkable=True)
        self.act_auto.triggered.connect(self._on_tray_auto)
        self.act_log = QAction("查看日志", self)
        self.act_log.triggered.connect(self._open_log)
        self.act_quit = QAction("退出", self)
        self.act_quit.triggered.connect(self._quit)
        for action in (self.act_show, self.act_now):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.act_auto)
        menu.addAction(self.act_log)
        menu.addSeparator()
        menu.addAction(self.act_quit)
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _open_log(self) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_PATH)))

    def _open_channel(self, event=None) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if not str(self.cfg.channel_url or "").strip():
            self._set_status("尚未配置 channel_url，请先编辑 config.json", RED)
            return
        QDesktopServices.openUrl(QUrl(self.cfg.channel_url))

    def _check_quit_flag(self) -> None:
        """轮询 quit.flag：另一份程序用 --quit 请求本实例退出。"""
        try:
            if not QUIT_FLAG.exists():
                return
            QUIT_FLAG.unlink(missing_ok=True)
        except OSError:
            return
        self.log.write("收到退出请求（--quit），正在退出")
        self._quit()

    def _quit(self) -> None:
        self._force_quit = True
        self.engine.shutdown()
        self.tray.hide()
        QApplication.quit()

    def _on_traffic(self, act: str) -> None:
        if act == "close":
            self.close()
        elif act == "min":
            self.showMinimized()
        else:
            on_top = bool(self.windowFlags() & Qt.WindowStaysOnTopHint)
            self.setWindowFlag(Qt.WindowStaysOnTopHint, not on_top)
            self.show()

    # ------------------------------------------------------------ 引擎联动
    def _wire_engine(self) -> None:
        self.engine.state_changed.connect(self._on_state)
        self.engine.checkin_done.connect(self._on_done)
        self.engine.next_send_at.connect(self._on_next_at)
        self.engine.sending_changed.connect(self._on_sending)
        self.log.line.connect(self._on_log_line)

    def _on_state(self, state: str) -> None:
        running = state in ("running", "starting")
        self.sw_auto.set_on(running, animate=True)
        self.act_auto.setChecked(running)
        self.act_auto.setText("停止打卡" if running else "开启打卡")
        self.ring.set_state("error" if state == "error" else ("running" if running else "stopped"))
        if state == "starting":
            self.ring.set_caption("正在发送")
            self.ring.set_time_text("--:--")
            self.ring.set_progress(0)
            self._set_status("正在打开浏览器并发送…", ACCENT)
        elif state == "running":
            self.ring.set_caption("距下次打卡")
            self._set_status("已开启：立即发送一次，之后" + _fmt_interval(self.cfg.interval_seconds), GREEN)
        elif state == "stopping":
            self.ring.set_caption("正在停止")
            self._set_status("正在停止后台线程…", TEXT_DIM)
        elif state == "error":
            # 已经停下来了，倒计时就不该继续走，否则看上去像还在等下一次
            self.ring.set_caption("已暂停")
            self.ring.set_time_text("--:--")
            self.ring.set_progress(0)
            self._next_at = None
            detail = self.engine.last_error or "未知错误"
            self._set_status(detail, RED)
        elif state == "stopped":
            self.ring.set_caption("已暂停")
            self.ring.set_time_text("--:--")
            self.ring.set_progress(0)
            self._next_at = None
            self._set_status("已停止定时打卡", TEXT_DIM)

    def _on_auto_toggled(self, on: bool) -> None:
        if on:
            self.engine.start(immediate=True, dedupe=True)
        else:
            self.engine.stop()

    def _on_boot_toggled(self, on: bool) -> None:
        ok = set_autostart(on)
        if not ok:
            self.sw_boot.set_on(not on, animate=True)
            self._set_status("开机自启设置失败（需要打包运行）", RED)
            return
        self._set_status("已开启开机自启" if on else "已关闭开机自启", TEXT)

    def _on_tray_auto(self, checked: bool) -> None:
        if checked:
            self.engine.start(immediate=True, dedupe=True)
        else:
            self.engine.stop()

    def _on_sending(self, active: bool) -> None:
        """每次真正发送（不只是启动时）都让圆环显示进度，而不是继续走倒计时。"""
        self._sending = active
        if active:
            self.ring.set_caption("正在发送")
            self.ring.set_time_text("--:--")
            self.ring.set_progress(0.08)
            self._set_status("正在发送…", ACCENT)

    def _on_send_now(self) -> None:
        if self.engine.sending:
            self._set_status("正在发送，请稍候…", ACCENT)
            return
        if self.engine.state in ("running", "starting"):
            self.engine.trigger_now()
            self._set_status("正在立即发送…", ACCENT)
        else:
            self._set_status("正在立即发送…", ACCENT)
            self.engine.start(immediate=True)

    def _on_done(self, ok: bool, detail: str) -> None:
        self._last_ok = ok
        stamp = datetime.now().strftime("%H:%M")
        if ok:
            self.row_last.set_value(f"{stamp} · 成功", GREEN)
            self._set_status(detail or "发送成功", GREEN)
        else:
            self.row_last.set_value(f"{stamp} · 失败", RED)
            self._set_status(detail or "发送失败", RED)

    def _on_next_at(self, dt) -> None:
        self._next_at = dt

    def _on_log_line(self, line: str) -> None:
        # 日志行最后一段作为状态提示，避免 UI 空白
        if not line:
            return
        text = line.split("] ", 1)[-1]
        if text:
            self.status.setToolTip(text)

    def _set_status(self, text: str, color: str) -> None:
        self.status.setText(text)
        self.status.setToolTip(text)
        self.status.setStyleSheet(f"color: {color}; background: transparent;")
        self.status_dot.setStyleSheet(f"color: {color}; background: transparent;")

    # ------------------------------------------------------------ 轮询
    def _poll(self) -> None:
        if self.engine.state not in ("running", "starting") or self._next_at is None:
            return
        if self._sending:
            return
        remaining = (self._next_at - datetime.now()).total_seconds()
        total = max(1, int(self.cfg.interval_seconds))
        self.ring.set_time_text(_fmt_countdown(remaining))
        self.ring.set_progress(min(1.0, max(0.0, 1 - remaining / total)))

    # ------------------------------------------------------------ 窗口事件
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event) -> None:
        if self._force_quit:
            event.accept()
            return
        event.ignore()
        self.hide()
        if not self._tray_hint_shown and self.tray.isVisible():
            self._tray_hint_shown = True
            self.tray.showMessage(APP_NAME, "已驻留后台，打卡任务继续运行", QSystemTrayIcon.Information, 2500)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.card.geometry())
        path = QPainterPath()
        path.addRoundedRect(rect, 22, 22)

        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        grad.setColorAt(0.0, QColor(CARD_TOP))
        grad.setColorAt(1.0, QColor(CARD_BOTTOM))
        p.fillPath(path, grad)

        pen = QPen(QColor(255, 255, 255, 26), 1)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()
