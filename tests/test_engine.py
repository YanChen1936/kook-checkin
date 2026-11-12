r"""调度与配置的回归测试（stdlib unittest，不依赖 pytest）。

覆盖 v7.1/v7.2 修掉的实际缺陷：
  * 计划时刻漂移（发送耗时累积进间隔）；
  * 休眠跨过发送点整轮跳过；
  * 发送过程中点"立即打卡"导致连发两次；
  * 保活浏览器、空闲却不释放。

运行：\.venv\Scripts\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from kookcheckin import config as config_mod  # noqa: E402
from kookcheckin import engine as engine_mod  # noqa: E402
from kookcheckin import state as state_mod  # noqa: E402
from kookcheckin.config import Config, load_config  # noqa: E402
from kookcheckin.engine import CheckinWorker, catch_up_tolerance, human_span, next_target  # noqa: E402
from kookcheckin.sender import SendResult  # noqa: E402


_app = QCoreApplication.instance() or QCoreApplication(sys.argv)

# 真机上的 state.json 记录着上一次成功打卡，测试里的假时钟绝不能写进去
# （否则会把 %LOCALAPPDATA%\KOOKCheckin\state.json 写成 2026-11-13 09:00 这种假时刻，
# 真程序启动后会以为"还没到点"而白等一整轮）。
_REAL_SAVE = engine_mod.save_last_send
_REAL_LOAD = engine_mod.load_last_send


def setUpModule() -> None:
    engine_mod.save_last_send = lambda *a, **k: None
    engine_mod.load_last_send = lambda *a, **k: None


def tearDownModule() -> None:
    engine_mod.save_last_send = _REAL_SAVE
    engine_mod.load_last_send = _REAL_LOAD


class Clock:
    """可手动推进的假时钟。"""

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 11, 13, 9, 0, 0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakeLog:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)

    def write_exception(self, exc, context: str = "", hint: str = "") -> None:
        self.lines.append(f"{context}: {exc}")

    def debug(self, text: str) -> None:
        pass

    def has(self, needle: str) -> bool:
        return any(needle in line for line in self.lines)


class FakeSender:
    def __init__(self, ok: bool = True, delay: float = 0.0):
        self.ok = ok
        self.delay = delay
        self.calls: list[float] = []
        self.released = 0
        self._open = True
        self.on_send = None  # 测试钩子：发送过程中执行

    @property
    def is_open(self) -> bool:
        return self._open

    def release(self) -> None:
        self.released += 1
        self._open = False

    def send(self, cfg, should_stop=None):
        self.calls.append(time.monotonic())
        if self.on_send is not None:
            self.on_send()
        else:
            if self.delay:
                time.sleep(self.delay)
        return SendResult(self.ok, "已发送" if self.ok else "失败")

    def stop(self) -> None:
        self._open = False


def make_worker(cfg: Config, clock: Clock, sender: FakeSender, immediate: bool = True) -> tuple[CheckinWorker, FakeLog]:
    log = FakeLog()
    worker = CheckinWorker(cfg, sender, log, immediate)
    engine_mod._now = clock
    return worker, log


def run_worker(worker: CheckinWorker) -> threading.Thread:
    t = threading.Thread(target=worker.run, daemon=True)
    worker._test_thread = t  # 便于 teardown 时 join，避免上一个用例的线程干扰下一个用例
    t.start()
    return t


def settle(delay: float = 0.4) -> None:
    """等发送块执行完、线程进入等待循环，再推进假时钟。

    否则会在 send() 返回与 started = _now() 之间推进时钟：
    started 读到的还是旧时刻，next_target() 算出的计划时刻落在"现在"之前，
    循环顶部直接越过等待再发一次（测试竞态，不是产品缺陷）。
    """
    time.sleep(delay)


def wait_calls(sender: FakeSender, count: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while len(sender.calls) < count and time.monotonic() < deadline:
        time.sleep(0.02)


def teardown_worker(worker: CheckinWorker) -> None:
    worker.requestInterruption()
    t = getattr(worker, "_test_thread", None)
    if t is not None:
        t.join(2.0)


class ScheduleTests(unittest.TestCase):
    def test_next_target_has_no_drift(self):
        base = datetime(2026, 11, 13, 9, 0, 0)
        tol = catch_up_tolerance(3600)
        # 发送花了 18 秒：下一次仍然是 10:00，而不是 10:00:18
        self.assertEqual(next_target(base, base + timedelta(seconds=18), tol, 3600),
                         base + timedelta(seconds=3600))

    def test_next_target_realigns_after_long_overrun(self):
        base = datetime(2026, 11, 13, 9, 0, 0)
        tol = catch_up_tolerance(3600)
        started = base + timedelta(hours=2)
        self.assertEqual(next_target(base, started, tol, 3600), started + timedelta(seconds=3600))

    def test_catch_up_after_sleep(self):
        '''休眠 2 小时跨过发送点：立即补发一次，而不是白丢一轮。'''
        cfg = Config(interval_seconds=3600)
        clock = Clock()
        sender = FakeSender()
        worker, log = make_worker(cfg, clock, sender)
        run_worker(worker)
        try:
            deadline = time.monotonic() + 3
            while len(sender.calls) < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(sender.calls), 1)
            settle()
            clock.advance(7200)  # 机器睡了两小时
            deadline = time.monotonic() + 3
            while len(sender.calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(sender.calls), 2, "错过的打卡没有被补发")
            self.assertTrue(log.has("立即补发"), log.lines)
        finally:
            teardown_worker(worker)

    def test_miss_without_catch_up_skips(self):
        cfg = Config(interval_seconds=3600, catch_up_missed=False)
        clock = Clock()
        sender = FakeSender()
        worker, log = make_worker(cfg, clock, sender)
        run_worker(worker)
        try:
            deadline = time.monotonic() + 3
            while len(sender.calls) < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            settle()
            clock.advance(7200)
            time.sleep(1.0)
            self.assertEqual(len(sender.calls), 1, "关闭补发后不应补发")
            self.assertTrue(log.has("跳过本次"), log.lines)
        finally:
            teardown_worker(worker)

    def test_trigger_during_send_does_not_double_send(self):
        '''发送中点"立即打卡"：只应当有一次发送，不能再排队发第二次。'''
        cfg = Config(interval_seconds=3600)
        clock = Clock()
        holder = {}

        def mid_send():
            worker = holder["worker"]
            worker.trigger_now()  # 模拟用户在发送过程中点按钮
            time.sleep(0.3)

        sender = FakeSender()
        sender.on_send = mid_send
        worker, log = make_worker(cfg, clock, sender)
        holder["worker"] = worker
        run_worker(worker)
        try:
            time.sleep(1.5)
            self.assertEqual(len(sender.calls), 1, "立即打卡在发送中被重复执行")
        finally:
            teardown_worker(worker)

    def test_waiting_trigger_sends_immediately(self):
        cfg = Config(interval_seconds=3600)
        clock = Clock()
        sender = FakeSender()
        worker, log = make_worker(cfg, clock, sender)
        run_worker(worker)
        try:
            deadline = time.monotonic() + 3
            while len(sender.calls) < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            settle(0.25)
            worker.trigger_now()
            deadline = time.monotonic() + 3
            while len(sender.calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(sender.calls), 2)
            self.assertTrue(log.has("提前发送"), log.lines)
        finally:
            teardown_worker(worker)

    def test_idle_releases_browser(self):
        cfg = Config(interval_seconds=3600, idle_close_seconds=1)
        clock = Clock()
        sender = FakeSender()
        worker, log = make_worker(cfg, clock, sender)
        run_worker(worker)
        try:
            deadline = time.monotonic() + 3
            while len(sender.calls) < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            settle(0.35)             # 让工作线程进入等待循环（idle_since 已记录）
            clock.advance(2)         # 假时钟推进 2 秒，越过 idle_close_seconds=1
            deadline = time.monotonic() + 5
            while sender.released < 1 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(sender.released, 1, "空闲后没有释放浏览器")
            self.assertTrue(log.has("关闭浏览器释放内存"), log.lines)
        finally:
            teardown_worker(worker)

    def test_human_span(self):
        self.assertEqual(human_span(45), "45 秒")
        self.assertEqual(human_span(600), "10 分钟")
        self.assertEqual(human_span(7500), "2 小时 5 分钟")
        self.assertEqual(human_span(7200), "2 小时")


class ResumeTests(unittest.TestCase):
    """v7.2：开机自启续上次节拍，不再重启就补一条「打卡」。"""

    def test_autostart_does_not_resend_immediately(self):
        cfg = Config(interval_seconds=3600)
        clock = Clock(datetime(2026, 11, 13, 9, 20, 0))
        last = datetime(2026, 11, 13, 9, 0, 0)
        sender = FakeSender()
        worker, log = make_worker(cfg, clock, sender, immediate=False)
        original = engine_mod.load_last_send
        engine_mod.load_last_send = lambda *a, **k: last
        try:
            run_worker(worker)
            time.sleep(0.6)
            self.assertEqual(sender.calls, [], "上次发送才 20 分钟，不该再发一条")
            self.assertTrue(log.has("续上次进度"), log.lines)
        finally:
            engine_mod.load_last_send = original
            teardown_worker(worker)

    def test_autostart_sends_when_overdue(self):
        cfg = Config(interval_seconds=3600)
        clock = Clock(datetime(2026, 11, 13, 11, 30, 0))
        last = datetime(2026, 11, 13, 9, 0, 0)
        sender = FakeSender()
        worker, log = make_worker(cfg, clock, sender, immediate=False)
        original = engine_mod.load_last_send
        engine_mod.load_last_send = lambda *a, **k: last
        try:
            run_worker(worker)
            deadline = time.monotonic() + 3
            while len(sender.calls) < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(sender.calls), 1, "已经超过一个间隔，自启应当补发")
            self.assertTrue(log.has("立即补发"), log.lines)
        finally:
            engine_mod.load_last_send = original
            teardown_worker(worker)

    def test_successful_send_is_recorded(self):
        cfg = Config(interval_seconds=3600)
        clock = Clock(datetime(2026, 11, 13, 9, 0, 0))
        sender = FakeSender()
        worker, log = make_worker(cfg, clock, sender)
        recorded: list[datetime] = []
        original_save = engine_mod.save_last_send
        engine_mod.save_last_send = lambda when, *a, **k: recorded.append(when)
        try:
            run_worker(worker)
            deadline = time.monotonic() + 3
            while not recorded and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(recorded, [clock.now])
        finally:
            engine_mod.save_last_send = original_save
            teardown_worker(worker)


class StateTests(unittest.TestCase):
    """v7.2：重启不重复打卡。"""

    def test_resume_target_before_due(self):
        now = datetime(2026, 11, 13, 9, 30, 0)
        last = datetime(2026, 11, 13, 9, 0, 0)
        target = state_mod.resume_target(last, now, 3600)
        self.assertEqual(target, datetime(2026, 11, 13, 10, 0, 0))

    def test_resume_target_after_due_sends_now(self):
        now = datetime(2026, 11, 13, 11, 30, 0)
        last = datetime(2026, 11, 13, 9, 0, 0)
        self.assertIsNone(state_mod.resume_target(last, now, 3600))

    def test_resume_without_record(self):
        self.assertIsNone(state_mod.resume_target(None, datetime(2026, 11, 13, 9, 0, 0), 3600))

    def test_recently_sent_guard(self):
        now = datetime(2026, 11, 13, 9, 0, 30)
        self.assertTrue(state_mod.recently_sent(datetime(2026, 11, 13, 9, 0, 0), now))
        self.assertFalse(state_mod.recently_sent(datetime(2026, 11, 13, 8, 50, 0), now))
        self.assertFalse(state_mod.recently_sent(None, now))

    def test_state_roundtrip_and_corruption(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            stamp = datetime(2026, 11, 13, 9, 0, 0)
            state_mod.save_last_send(stamp, path)
            self.assertEqual(state_mod.load_last_send(path), stamp)
            path.write_text("{ not json", encoding="utf-8")
            self.assertIsNone(state_mod.load_last_send(path))


class ConfigTests(unittest.TestCase):
    def test_missing_keys_are_written_back(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"message": "早安", "interval_seconds": 1800}), encoding="utf-8")
            cfg = load_config(path)
            self.assertEqual(cfg.message, "早安")
            self.assertEqual(cfg.interval_seconds, 1800)
            self.assertTrue(cfg.catch_up_missed)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("catch_up_missed", saved)
            self.assertIn("idle_close_seconds", saved)
            self.assertEqual(saved["message"], "早安")

    def test_idle_close_zero_is_kept(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"idle_close_seconds": 0}), encoding="utf-8")
            cfg = load_config(path)
            self.assertEqual(cfg.idle_close_seconds, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
