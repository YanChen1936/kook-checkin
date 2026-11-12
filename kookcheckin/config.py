"""配置：dataclass 默认值 + config.json 覆盖 + 开机自启。

v6 行为保持：config.json 里出现的键覆盖默认值，任何解析失败都回退默认，
不允许因为配置写坏而挡住程序启动。v7 新增：文件不存在时主动写出一份默认
配置，用户照着改即可；写入前会做类型校验。
"""

from __future__ import annotations

import json
import winreg
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from .paths import CONFIG_PATH, RUN_KEY, exe_path, is_frozen

EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

DEFAULT_SELECTORS: tuple[str, ...] = (
    "div[contenteditable='true']",
    "textarea",
    "input[type='text']",
    "[data-testid='message-input']",
)


@dataclass
class Config:
    # 首次运行后请把下面两行改成你自己的频道：打开 KOOK 网页版进入目标频道，
    # 地址栏就是 https://www.kookapp.cn/app/channels/<服务器ID>/<频道ID>，原样粘进来即可。
    channel_url: str = ""
    channel_display: str = "我的频道"
    message: str = "打卡"
    interval_seconds: int = 3600
    input_selectors: tuple[str, ...] = field(default=DEFAULT_SELECTORS)
    nav_timeout_ms: int = 60000
    login_wait_seconds: int = 300
    edge_path: str = EDGE_PATH
    log_max_bytes: int = 524288

    # ---- v7 新增（可选，不必写进 config.json） ----
    send_verify: bool = True
    max_retry: int = 3
    keep_browser_alive: bool = True

    # ---- v7.1 新增 ----
    catch_up_missed: bool = True   # 休眠/卡死错过发送点时立即补发一次
    idle_close_seconds: int = 900  # 空闲多久关掉浏览器释放内存，0 = 一直保活


def _coerce(name: str, value: Any, current: Any) -> Any:
    if name == "input_selectors":
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple)):
            items = tuple(str(v) for v in value if str(v).strip())
            return items or current
        return current
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int) and name == "idle_close_seconds":
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return current
    if isinstance(current, int):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return current
    if isinstance(current, str):
        return str(value) if value is not None else current
    return value


def load_config(path=CONFIG_PATH) -> Config:
    cfg = Config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        try:
            save_config(cfg, path)
        except OSError:
            pass
        return cfg
    except Exception:
        return cfg
    if not isinstance(raw, dict):
        return cfg
    for f in fields(cfg):
        if f.name in raw:
            setattr(cfg, f.name, _coerce(f.name, raw[f.name], getattr(cfg, f.name)))
    if not cfg.edge_path or not cfg.edge_path.lower().endswith(".exe"):
        cfg.edge_path = EDGE_PATH
    missing = [f.name for f in fields(cfg) if f.name not in raw]
    if missing:
        # 老版本写下的 config.json 缺新键时补写一份，让用户看得见、改得动
        try:
            save_config(cfg, path)
        except OSError:
            pass
    return cfg


def save_config(cfg: Config, path=CONFIG_PATH) -> None:
    data = asdict(cfg)
    data["input_selectors"] = list(cfg.input_selectors)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 开机自启

def get_autostart_cmd() -> str:
    exe = exe_path()
    if not exe:
        return ""
    return f'"{exe}" --auto'


def set_autostart(enabled: bool) -> bool:
    """写/删 HKCU Run；返回是否成功。源码运行时不写（避免把 python.exe 写进自启）。"""
    cmd = get_autostart_cmd()
    if not cmd:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, "KOOKCheckin", 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, "KOOKCheckin")
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, "KOOKCheckin")
            return bool(value)
    except OSError:
        return False
