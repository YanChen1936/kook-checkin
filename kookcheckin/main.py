"""入口：单实例锁 + 日志 + 主窗口。--auto 用于开机自启（静默启动并立即打卡）。"""

from __future__ import annotations

import ctypes
import sys
import traceback

from PySide6.QtWidgets import QApplication, QMessageBox

from . import __version__
from .config import load_config
from .logbus import LogBus
from .paths import APP_NAME, CONFIG_PATH, LOG_PATH, MUTEX_NAME, QUIT_FLAG, ensure_dirs
from .window import MainWindow

_mutex_handle = None


def acquire_single_instance() -> bool:
    """命名互斥体；已存在同名实例时返回 False。

    用 use_last_error=True 让 ctypes 在调用返回的瞬间抓取错误码，
    不能再调一次 kernel32.GetLastError()——中间任何一次 ctypes 调用都可能把它冲掉。
    """
    global _mutex_handle
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    _mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def _install_excepthook(log: LogBus) -> None:
    def hook(exc_type, exc_value, exc_tb):
        log.debug("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        log.write(f"未捕获异常: {exc_type.__name__}: {exc_value}")

    sys.excepthook = hook


def request_quit() -> int:
    """`--quit`：让正在运行的实例退出（升级前不必去托盘点"退出"）。

    没有实例在跑时顺手清掉残留标记，避免下次启动读到一个假的退出请求。
    """
    if acquire_single_instance():
        try:
            QUIT_FLAG.unlink(missing_ok=True)
        except OSError:
            pass
        return 0
    ensure_dirs()
    QUIT_FLAG.write_text("quit", encoding="utf-8")
    return 0


def main() -> int:
    if "--quit" in sys.argv:
        return request_quit()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)

    if not acquire_single_instance():
        QMessageBox.information(None, APP_NAME, "KOOK 定时打卡已在运行，请勿重复打开。")
        return 0

    ensure_dirs()
    cfg = load_config()
    log = LogBus(LOG_PATH, max_bytes=cfg.log_max_bytes)
    log.start()
    _install_excepthook(log)
    log.write(f"程序启动 v{__version__}（配置 {CONFIG_PATH}）")

    win = MainWindow(cfg, log)
    auto = "--auto" in sys.argv

    def _shutdown() -> None:
        try:
            win.engine.shutdown()
        except Exception:
            pass
        log.write("程序退出")
        log.stop()

    app.aboutToQuit.connect(_shutdown)

    if auto:
        log.write("开机自启模式：后台启动，续上次节拍")
        win.hide()
        win.start_engine(immediate=False)
    else:
        win.show()
        # 打开窗口即续上次节拍：state.json 说还没到点就不补发，早过期才补一条
        win.start_engine(immediate=False)

    code = app.exec()
    return code


if __name__ == "__main__":
    sys.exit(main())
