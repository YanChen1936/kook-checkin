"""调度：CheckinWorker（线程）+ CheckinEngine（状态机）。

状态机：stopped → starting → running → stopping → stopped，v7 增加 error 状态：
遇到致命错误（驱动缺失等）时不再每小时空转四次，而是停下并在 UI 上告诉用户怎么修。

v7.1 调度改动（都是对着实际日志缺陷改的）：
  * 计划时刻制：下一次发送 = 上一次计划时刻 + 间隔，发送耗时不再累积成漂移；
  * 休眠补发：越过计划时刻太多（默认容差 max(60s, 间隔 5%)）时立即补发一次并重新
    对齐，而不是把整轮跳过去（v7.0 会白丢一次打卡）；
  * 立即打卡竞态：每轮发送前清空唤醒标志，"发送中再点一次"不会再排队连发两次；
  * 空闲释放浏览器：连续空闲超过 idle_close_seconds 就关掉 Edge 与驱动，
    不再为了每小时一次的发送常驻 200-400MB 内存。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QThread, Signal

from .config import Config
from .logbus import LogBus
from .sender import BrowserSender, SendResult
from .state import load_last_send, recently_sent, resume_target, save_last_send


def _now() -> datetime:
    """单点获取时间，便于测试替换。"""
    return datetime.now()


def catch_up_tolerance(interval_s: int) -> float:
    """判定"只是慢了一点"还是"真的错过了"的容差（秒）。"""
    return max(60.0, interval_s * 0.05)


def next_target(target: datetime, started: datetime, tolerance: float, interval_s: int) -> datetime:
    """下一次发送时刻。

    正常情况（发送耗时在容差内）= 原计划时刻 + 间隔，所以发送耗时不会累积成漂移；
    只有真的错过了一个整点（休眠/长阻塞）才从当前时刻重新对齐。
    """
    late = (started - target).total_seconds()
    base = started if late > tolerance else target
    return base + timedelta(seconds=interval_s)


def human_span(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if minutes:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{hours} 小时"


class CheckinWorker(QThread):
    checkin_done = Signal(bool, str)
    next_send_at = Signal(object)
    sending = Signal(bool)
    engine_log = Signal(str)
    fatal = Signal(str)

    def __init__(self, cfg: Config, sender: BrowserSender, log: LogBus, immediate: bool = True, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._sender = sender
        self._log = log
        self._immediate = immediate
        self._wake = threading.Event()

    def trigger_now(self) -> None:
        """让等待中的线程提前进入下一轮发送（发送进行中调用会被忽略）。"""
        self._wake.set()

    def _should_stop(self) -> bool:
        return self.isInterruptionRequested()

    # ------------------------------------------------------------ 主循环
    def run(self) -> None:
        cfg = self._cfg
        log = self._log
        interval = max(30, int(cfg.interval_seconds))
        tolerance = catch_up_tolerance(interval)
        idle_close = max(0, int(getattr(cfg, "idle_close_seconds", 0)))
        catch_up = bool(getattr(cfg, "catch_up_missed", True))

        target = self._first_target(interval, log)
        while not self._should_stop():
            if _now() < target:  # 开机自启续节拍：先等到计划时刻，不急着发
                next_at = self._wait_until(target, interval, tolerance, catch_up, idle_close)
                if next_at is None:
                    break
                target = next_at
                continue

            self._wake.clear()  # 发送期间积攒的"立即打卡"不再排队成第二次发送
            self.sending.emit(True)
            try:
                result = self._sender.send(cfg, self._should_stop)
            except Exception as exc:  # 兜底：线程内任何异常都不能让线程静默死掉
                log.write_exception(exc, "发送失败")
                result = SendResult(False, str(exc))
            finally:
                self.sending.emit(False)
            # 发送期间点到的"立即打卡"在这里丢弃：这一轮已经发过了，
            # 留着它会让等待循环立刻再发一次（v7.0 的连发缺陷）。
            self._wake.clear()

            if self._should_stop():
                break

            started = _now()
            if result.ok:
                save_last_send(started)
            self.checkin_done.emit(result.ok, result.detail)
            if result.fatal:
                self.fatal.emit(result.detail)
                break

            # 补发过就按当前时刻重新对齐，否则严格按原节拍顺延（零漂移）
            target = next_target(target, started, tolerance, interval)
            self.next_send_at.emit(target)

        self._sender.stop()
        self.engine_log.emit("后台线程已结束")

    def _first_target(self, interval: int, log: LogBus) -> datetime:
        """第一轮什么时候发：显式开启就立刻发，开机自启则续上次节拍。"""
        now = _now()
        if self._immediate:
            log.write(f"已开启：立即发送一次，之后每 {max(1, interval // 60)} 分钟自动打卡")
            return now
        last = load_last_send()
        target = resume_target(last, now, interval)
        if target is not None and last is not None:
            log.write(
                f"续上次进度：上次发送于 {last:%m-%d %H:%M}，下次 {target:%m-%d %H:%M}"
            )
            return target
        if last is not None:
            log.write(f"距上次发送已超过 {max(1, interval // 60)} 分钟，立即补发一次")
        else:
            log.write("没有历史发送记录，立即发送一次")
        return now

    def _wait_until(self, target, interval, tolerance, catch_up, idle_close):
        """等到 target；返回下一轮要发送的时刻（可能被"立即打卡"拉前）。"""
        log = self._log
        idle_since = _now()
        released = False
        while not self._should_stop():
            now = _now()
            if now >= target:
                late = (now - target).total_seconds()
                if late > tolerance:
                    if not catch_up:
                        log.write(f"错过计划时刻约 {human_span(late)}，已跳过本次（未开启补发）")
                        target = now + timedelta(seconds=interval)
                        self.next_send_at.emit(target)
                        idle_since = now
                        continue
                    log.write(f"错过计划时刻约 {human_span(late)}，立即补发一次")
                return target
            if self._wake.wait(0.2):
                self._wake.clear()
                if self._should_stop():
                    return None
                log.write("收到立即打卡请求，提前发送")
                return _now()
            if idle_close and not released:
                if (_now() - idle_since).total_seconds() >= idle_close and self._sender.is_open:
                    self._sender.release()
                    released = True
                    log.write(f"已空闲 {human_span(idle_close)}，关闭浏览器释放内存")
        return None


class CheckinEngine(QObject):
    state_changed = Signal(str)
    checkin_done = Signal(bool, str)
    next_send_at = Signal(object)
    sending_changed = Signal(bool)  # 不能叫 sending：会和下面的 sending 属性撞名

    def __init__(self, cfg: Config, log: LogBus, sender_factory=None, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._log = log
        self._state = "stopped"
        self._worker: CheckinWorker | None = None
        self._sender = sender_factory() if sender_factory else BrowserSender(log)
        self._last_error = ""
        self._sending = False

    # ------------------------------------------------------------ 状态
    @property
    def state(self) -> str:
        return self._state

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def sending(self) -> bool:
        return self._sending

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self.state_changed.emit(state)

    # ------------------------------------------------------------ 控制
    def start(self, immediate: bool = True, dedupe: bool = False) -> None:
        """immediate=True 立刻发一次；dedupe=True 时最近 60 秒发过就不再重复发。"""
        if self._state in ("starting", "running"):
            return
        if not str(self._cfg.channel_url or "").strip():
            # 没配频道就别启动浏览器：否则只会拿到一个空白页，日志里只剩下不可读的超时。
            msg = ("尚未配置打卡频道：请编辑 "
                   "config.json 里的 channel_url（KOOK 网页版地址栏的 "
                   "/app/channels/<服务器ID>/<频道ID>）")
            self._log.write(msg)
            self._last_error = msg
            self._set_state("error")
            return
        if immediate and dedupe and recently_sent(load_last_send(), _now()):
            immediate = False
            self._log.write("刚才已经发送过，本次不再重复发送，只按间隔排下一次")
        self._set_state("starting")
        self._last_error = ""
        self._worker = CheckinWorker(self._cfg, self._sender, self._log, immediate, self)
        self._worker.checkin_done.connect(self._on_done)
        self._worker.next_send_at.connect(self.next_send_at.emit)
        self._worker.sending.connect(self._on_sending)
        self._worker.engine_log.connect(self._log.write)
        self._worker.fatal.connect(self._on_fatal)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()
        self._set_state("running")

    def trigger_now(self) -> None:
        """已运行时提前打卡；未运行时直接启动（启动即先发一次）。"""
        if self._sending:
            self._log.write("当前正在发送，忽略本次立即打卡")
            return
        if self._state in ("running", "starting") and self._worker is not None:
            self._worker.trigger_now()
        else:
            self.start()

    def stop(self) -> None:
        if self._state == "stopped":
            return
        self._set_state("stopping")
        worker = self._worker
        if worker and worker.isRunning():
            worker.requestInterruption()
            if not worker.wait(15000):
                self._log.write("后台线程未在 15 秒内结束，已强制终止")
                worker.terminate()
                worker.wait(3000)
        self._worker = None
        self._set_state("stopped")
        self._log.write("已停止定时打卡")

    def shutdown(self) -> None:
        self.stop()
        try:
            self._sender.stop()
        except Exception:
            pass

    # ------------------------------------------------------------ 回调
    def _on_sending(self, active: bool) -> None:
        self._sending = active
        self.sending_changed.emit(active)

    def _on_done(self, ok: bool, detail: str) -> None:
        if not ok:
            self._last_error = detail
        self.checkin_done.emit(ok, detail)

    def _on_fatal(self, detail: str) -> None:
        self._last_error = detail
        self._set_state("error")
        self._log.write(f"已停止自动打卡：{detail}")
        self.checkin_done.emit(False, detail)

    def _on_finished(self) -> None:
        if self._state == "error":
            return
        if self._state in ("running", "starting"):
            self._set_state("error")
            self._log.write("后台线程异常退出，已自动复位为暂停")
        elif self._state == "stopping":
            self._set_state("stopped")
