"""上次发送记录（state.json）：重启、重开开关不再重复打卡。

日志证据（2026-11-12 23:11/23:13/23:17/23:19/23:21）：v7.0 每次启动都"后台启动并立即打卡"，
五次重启等于往频道里多刷了五条「打卡」。所以把最后一次成功发送的时刻落盘，启动时
若还没到期就只排下一次，不补发第二遍。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from .paths import STATE_PATH


def load_last_send(path: Path | None = None) -> datetime | None:
    """读取上次成功发送时刻；文件缺失或损坏时返回 None（不影响正常打卡）。"""
    target = path or STATE_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return None
    import json

    try:
        raw = json.loads(text)
        stamp = raw.get("last_send_at")
        if not stamp:
            return None
        return datetime.fromisoformat(str(stamp))
    except (ValueError, TypeError, AttributeError):
        return None


def save_last_send(when: datetime, path: Path | None = None) -> None:
    """原子写：先写临时文件再替换，避免断电留下半个 JSON。"""
    target = path or STATE_PATH
    import json

    payload = json.dumps(
        {"last_send_at": when.isoformat(timespec="seconds")}, ensure_ascii=False
    )
    tmp = target.with_suffix(".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def resume_target(last: datetime | None, now: datetime, interval_s: int) -> datetime | None:
    """续上次节拍：还没到期就返回下一次时刻，已到期或没有记录则返回 None（立即发送）。"""
    if last is None:
        return None
    nxt = last + timedelta(seconds=max(30, int(interval_s)))
    return nxt if nxt > now else None


def recently_sent(last: datetime | None, now: datetime, min_gap: float = 60.0) -> bool:
    """刚刚发过（默认 60 秒内）：用于抑制反复开关或重启带来的连发。"""
    if last is None:
        return False
    return (now - last).total_seconds() < min_gap
