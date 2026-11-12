"""运行期路径与常量。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "KOOK 定时打卡"
APP_ID = "KOOKCheckin"
MUTEX_NAME = "KOOKCheckinMutex"

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
BASE_DIR = LOCALAPPDATA / APP_ID
PROFILE_DIR = BASE_DIR / "kook_profile"
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "kook_checkin.log"
DEBUG_LOG_PATH = BASE_DIR / "kook_checkin.debug.log"
STATE_PATH = BASE_DIR / "state.json"
QUIT_FLAG = BASE_DIR / "quit.flag"
SHOT_DIR = BASE_DIR / "shots"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def ensure_dirs() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """打包后指向 onedir 的 _internal 目录，源码运行时指向项目根。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def asset_path(name: str) -> Path:
    return bundle_dir() / "assets" / name


def exe_path() -> str:
    """当前可执行文件路径：打包后是 exe，源码运行时是 python.exe（自启不写入）。"""
    return sys.executable if is_frozen() else ""
