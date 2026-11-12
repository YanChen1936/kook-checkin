"""发送器：Playwright + 系统 Edge（持久化登录态）。

v6 每次发送都 sync_playwright() + launch_persistent_context + goto，一次 10-20 秒，
且发送完全依赖 %TEMP% 里的解包目录。v7 的改动：

  * 进程内复用 browser context / page，空闲时保活，加载网页只在首轮发生；
  * 发送前自检 playwright 驱动是否存在（onedir 打包后不会再丢，但崩溃/手工
    删除仍要给出可读提示，而不是 [WinError 2]）；
  * 发送后校验输入框是否被清空，避免“按了回车但其实没发出去”；
  * 失败截图落盘，异常分类：致命错误（驱动缺失）不重试，其它最多重试 3 次。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .logbus import LogBus, summarize_exception
from .paths import PROFILE_DIR, SHOT_DIR

REMOVE_MODAL_JS = (
    "document.querySelectorAll('.chuanyu-modal-container, .kaihei-modal-animate')"
    ".forEach(n => n.remove());"
)


class FatalSendError(RuntimeError):
    """重试没有意义的环境级错误。"""


@dataclass
class SendResult:
    ok: bool
    detail: str = ""
    fatal: bool = False
    need_login: bool = False


def driver_executable() -> tuple[Path | None, Path | None]:
    """返回 (node.exe, cli.js)。任一缺失都算环境坏了。"""
    try:
        from playwright._impl._driver import compute_driver_executable
    except Exception:
        return None, None
    try:
        result = compute_driver_executable()
        if isinstance(result, (tuple, list)) and len(result) >= 2:
            return Path(result[0]), Path(result[1])
        return Path(result), None
    except Exception:
        return None, None


def check_runtime() -> str:
    """返回空串表示驱动就绪，否则返回给人看的原因。"""
    node, cli = driver_executable()
    if node is None:
        return "未安装 playwright（源码环境请 pip install playwright）"
    if not node.exists():
        return f"浏览器驱动缺失：{node}，请重新安装/解压程序"
    if cli is not None and not cli.exists():
        return f"浏览器驱动不完整：{cli}，请重新安装/解压程序"
    return ""


def open_context(playwright, cfg: Config, headless: bool):
    kwargs = dict(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1280, "height": 900},
    )
    edge = (cfg.edge_path or "").strip()
    if edge and Path(edge).exists():
        return playwright.chromium.launch_persistent_context(executable_path=edge, **kwargs)
    return playwright.chromium.launch_persistent_context(channel="msedge", **kwargs)


def find_input(page, selectors) -> object | None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


class BrowserSender:
    """带上下文复用的发送器。线程内使用：不要在多个线程同时调用 send。"""

    def __init__(self, log: LogBus, headless: bool = True):
        self.log = log
        self.headless = headless
        self._pw = None
        self._ctx = None
        self._page = None
        self._logged_in = False

    # ------------------------------------------------------------ 内部工具
    def _alive(self) -> bool:
        try:
            return bool(self._ctx and self._page and not self._page.is_closed())
        except Exception:
            return False

    def _shutdown(self) -> None:
        for attr in ("_ctx", "_pw"):
            obj = getattr(self, attr)
            setattr(self, attr, None)
            if obj is None:
                continue
            try:
                obj.close() if attr == "_ctx" else obj.stop()
            except Exception:
                pass
        self._page = None
        self._logged_in = False

    def _ensure_page(self, cfg: Config, headless: bool):
        if self._alive():
            return self._page
        self._shutdown()
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._ctx = open_context(self._pw, cfg, headless)
        pages = self._ctx.pages
        self._page = pages[0] if pages else self._ctx.new_page()
        self._page.set_default_timeout(15000)
        return self._page

    def _goto(self, page, cfg: Config) -> None:
        page.goto(cfg.channel_url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
        self._kill_modal(page)

    def _kill_modal(self, page) -> None:
        try:
            page.evaluate(REMOVE_MODAL_JS)
        except Exception:
            pass

    def _wait_input(self, page, cfg: Config, rounds: int = 34, interval_ms: int = 500):
        for _ in range(rounds):
            box = find_input(page, cfg.input_selectors)
            if box is not None:
                return box
            page.wait_for_timeout(interval_ms)
            self._kill_modal(page)
        return None

    def _screenshot(self, page, tag: str) -> str:
        try:
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = SHOT_DIR / f"{tag}-{datetime.now():%Y%m%d-%H%M%S}.png"
            page.screenshot(path=str(path), full_page=False)
            self._prune_shots()
            return str(path)
        except Exception:
            return ""

    @staticmethod
    def _prune_shots(keep: int = 8) -> None:
        try:
            shots = sorted(SHOT_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in shots[keep:]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------ 发送主流程
    def send(self, cfg: Config, should_stop=None) -> SendResult:
        runtime_issue = check_runtime()
        if runtime_issue:
            return SendResult(False, runtime_issue, fatal=True)

        stop = should_stop or (lambda: False)
        attempts = 1 + max(0, int(getattr(cfg, "max_retry", 3)))
        last = SendResult(False, "未知错误")
        for attempt in range(attempts):
            if stop():
                return SendResult(False, "已取消")
            try:
                result = self._send_once(cfg, stop)
                if result.ok or result.fatal or result.need_login:
                    return result
                last = result
            except FatalSendError as exc:
                self.log.write_exception(exc, "发送失败", "环境异常，重试无效")
                return SendResult(False, summarize_exception(exc), fatal=True)
            except Exception as exc:
                self.log.write_exception(exc, "发送失败")
                last = SendResult(False, summarize_exception(exc))
                self._shutdown()
            if attempt + 1 < attempts:
                wait_s = min(2 ** attempt, 8)
                self.log.write(f"{wait_s} 秒后重试（第 {attempt + 1}/{attempts - 1} 次）")
                for _ in range(int(wait_s * 10)):
                    if stop():
                        return SendResult(False, "已取消")
                    time.sleep(0.1)
        return last

    def _send_once(self, cfg: Config, stop) -> SendResult:
        page = self._ensure_page(cfg, self.headless)
        try:
            if not page.url or "kookapp.cn" not in page.url:
                self._goto(page, cfg)
            else:
                page.reload(wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
                self._kill_modal(page)
        except Exception as exc:
            self._shutdown()
            raise RuntimeError(f"打开频道失败: {summarize_exception(exc)}") from exc

        box = self._wait_input(page, cfg)
        if box is None:
            login = self._handle_login(cfg, stop)
            if not login.ok:
                return login
            page = self._page
            box = self._wait_input(page, cfg)
            if box is None:
                shot = self._screenshot(page, "no-input")
                return SendResult(False, f"没有找到输入框{('，截图 ' + shot) if shot else ''}")

        try:
            box.click()
            box.fill(cfg.message)
            box.press("Enter")
        except Exception as exc:
            shot = self._screenshot(page, "send")
            self._shutdown()
            return SendResult(False, f"输入框操作失败: {summarize_exception(exc)}")

        sent_ok = True
        if getattr(cfg, "send_verify", True):
            sent_ok = self._verify_sent(page, box, cfg)
        if not sent_ok:
            shot = self._screenshot(page, "unverified")
            return SendResult(False, f"发送后校验失败{('，截图 ' + shot) if shot else ''}")

        self._logged_in = True
        self.log.write(f"已发送「{cfg.message}」")
        if not getattr(cfg, "keep_browser_alive", True):
            self._shutdown()
        return SendResult(True, f"已发送「{cfg.message}」")

    def _verify_sent(self, page, box, cfg: Config) -> bool:
        """KOOK 发送成功后输入框会被清空；3 秒内没清空说明没发出去。

        元素在发送后被框架重建时会抛异常，此时重新查找输入框再判断：
        找到了且有残留文本 = 没发出去；找不到 = 输入框已卸载，视为已发送。
        """
        expect = cfg.message.strip()
        for _ in range(15):
            try:
                text = box.inner_text().strip()
            except Exception:
                fresh = find_input(page, cfg.input_selectors)
                if fresh is None:
                    return True
                try:
                    text = fresh.inner_text().strip()
                except Exception:
                    return True
                box = fresh
            if not text or text != expect:
                return True
            time.sleep(0.2)
        return False

    def _handle_login(self, cfg: Config, stop) -> SendResult:
        self.log.write("登录态失效，正在打开 Edge 窗口，请登录 KOOK")
        self.headless = False
        try:
            self._shutdown()
            page = self._ensure_page(cfg, headless=False)
            self._goto(page, cfg)
        except Exception as exc:
            self._shutdown()
            return SendResult(False, f"打开登录窗口失败: {summarize_exception(exc)}", fatal=True)

        deadline = time.time() + max(30, int(cfg.login_wait_seconds))
        prompted = False
        while time.time() < deadline:
            if stop():
                return SendResult(False, "已取消")
            if find_input(page, cfg.input_selectors) is not None:
                self.log.write("登录成功，已恢复后台发送")
                self.headless = True
                return SendResult(True, "已登录")
            if not prompted and time.time() > deadline - cfg.login_wait_seconds + 5:
                self.log.write(f"请在 Edge 窗口登录 KOOK（最多等待 {cfg.login_wait_seconds // 60} 分钟）")
                prompted = True
            page.wait_for_timeout(1000)
            self._kill_modal(page)
        self._shutdown()
        return SendResult(False, "等待登录超时，本次发送取消", need_login=True)

    # ------------------------------------------------------------ 生命周期
    @property
    def is_open(self) -> bool:
        """浏览器还开着吗（空闲释放后为 False）。"""
        return self._alive()

    def release(self) -> None:
        """空闲释放：关掉 Edge 与 playwright 驱动，下次发送时按需重建。"""
        if self._pw is None and self._ctx is None:
            return
        self._shutdown()

    def stop(self) -> None:
        self._shutdown()
