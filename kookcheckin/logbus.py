"""日志：单线程串行写盘 + 按大小轮转 + 异常降噪。

v6 的线上日志里出现过一整段 playwright 的 "Browser logs:" 与 msedge 启动
命令行（每条约 2KB，几十条），把真正有用的信息淹掉了。v7 的规定：

  * 用户日志（kook_checkin.log）每个事件只有一行，异常写 "类型: 摘要"；
  * 完整 traceback / 浏览器原始输出进 debug 日志（kook_checkin.debug.log），
    同样做大小轮转，默认 1MB；
  * 所有线程只往队列里丢字符串，唯一消费线程负责写盘和轮转。
"""

from __future__ import annotations

import os
import queue
import threading
import time
import traceback
from datetime import datetime

from PySide6.QtCore import QObject, Signal

from .paths import DEBUG_LOG_PATH, LOG_PATH

_MAX_DEBUG_BYTES = 1024 * 1024


def _one_line(text: str) -> str:
    return " ".join(str(text).replace("\r", " ").replace("\n", " ").split())


def summarize_exception(exc: BaseException, limit: int = 220) -> str:
    """把异常压成一行：类型 + 摘要。WinError 2 这类只有后半句有用。"""
    head = _one_line(exc)
    name = type(exc).__name__
    text = f"{name}: {head}" if head else name
    return text[:limit]


def _rotate(path, max_bytes: int, keep: int = 2) -> None:
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_bytes:
            return
    except OSError:
        return
    for idx in range(keep, 0, -1):
        src = f"{path}.{idx}"
        dst = f"{path}.{idx + 1}"
        try:
            if os.path.exists(dst):
                os.remove(dst)
            if os.path.exists(src):
                os.replace(src, dst)
        except OSError:
            pass
    try:
        os.replace(path, f"{path}.1")
    except OSError:
        pass


class LogBus(QObject):
    """线程安全的日志入口；UI 通过 line 信号拿最新一行。"""

    line = Signal(str)

    def __init__(self, path=LOG_PATH, debug_path=DEBUG_LOG_PATH, max_bytes: int = 524288, parent=None):
        super().__init__(parent)
        self._path = str(path)
        self._debug_path = str(debug_path)
        self._max_bytes = max(65536, int(max_bytes))
        self._q: queue.Queue[str | None] = queue.Queue(maxsize=4096)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest = ""

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="log-sink", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout)

    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=0.3)
            except queue.Empty:
                continue
            if item is None:
                break
            _rotate(self._path, self._max_bytes)
            try:
                with open(self._path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(item + "\n")
            except OSError:
                pass

    # ------------------------------------------------------------ public API
    @property
    def latest(self) -> str:
        return self._latest

    def write(self, message: str) -> str:
        text = _one_line(message)
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}"
        self._latest = line
        try:
            self._q.put_nowait(line)
        except queue.Full:
            pass
        self.line.emit(line)
        return line

    def write_exception(self, exc: BaseException, context: str = "", user_hint: str = "") -> str:
        summary = summarize_exception(exc)
        msg = f"{context}: {summary}" if context else summary
        if user_hint:
            msg = f"{msg}（{user_hint}）"
        self.debug(traceback.format_exc() if exc.__traceback__ else f"{type(exc).__name__}: {exc}")
        return self.write(msg)

    def debug(self, text: str) -> None:
        """完整诊断信息，只进 debug 日志，不打扰主日志。"""
        with self._lock:
            try:
                _rotate(self._debug_path, _MAX_DEBUG_BYTES)
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(self._debug_path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(f"\n===== {stamp} =====\n")
                    fh.write(text.rstrip() + "\n")
            except OSError:
                pass

    def flush(self) -> None:
        deadline = time.time() + 1.0
        while not self._q.empty() and time.time() < deadline:
            time.sleep(0.01)
