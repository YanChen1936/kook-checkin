"""真实发送自检：用现有 kook_profile 跑一次 BrowserSender.send()。

会真的在频道里发一条「打卡」（与程序平时行为一致）。不启动 GUI、不占单实例锁。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kookcheckin.config import load_config  # noqa: E402
from kookcheckin.logbus import LogBus  # noqa: E402
from kookcheckin.paths import DEBUG_LOG_PATH, LOG_PATH, ensure_dirs  # noqa: E402
from kookcheckin.sender import BrowserSender, check_runtime  # noqa: E402


def main() -> int:
    ensure_dirs()
    cfg = load_config()
    log = LogBus(LOG_PATH, DEBUG_LOG_PATH, cfg.log_max_bytes)
    log.start()
    issue = check_runtime()
    print("runtime:", issue or "ok")
    if issue:
        return 2

    sender = BrowserSender(log)
    t0 = time.time()
    try:
        result = sender.send(cfg)
        print(f"result ok={result.ok} fatal={result.fatal} need_login={result.need_login} "
              f"elapsed={time.time() - t0:.1f}s detail={result.detail}")
    finally:
        sender.stop()
        log.stop()
    return 0 if "result" in dir() else 1


if __name__ == "__main__":
    raise SystemExit(main())
