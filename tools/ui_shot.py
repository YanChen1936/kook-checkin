"""离屏渲染主窗口，用于验收界面（不弹真窗口、不启动引擎）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 用系统字体渲染（offscreen 平台缺 CJK 字体回退，会渲染成方块）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from kookcheckin.config import load_config  # noqa: E402
from kookcheckin.logbus import LogBus  # noqa: E402
from kookcheckin.paths import ensure_dirs  # noqa: E402
from kookcheckin.window import MainWindow  # noqa: E402


def main() -> int:
    ensure_dirs()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    cfg = load_config()
    log = LogBus()
    win = MainWindow(cfg, log)
    win.show()
    app.processEvents()

    out = Path(__file__).resolve().parent.parent / "build_ui.png"
    # 三种状态各截一张
    win.grab().save(str(out))
    win.ring.set_state("running")
    win.ring.set_caption("距下次打卡")
    win.ring.set_progress(0.42)
    win.ring.set_time_text("34:56")
    win.row_last.set_value("21:48 · 成功", "#30D158")
    win._set_status("已开启：立即发送一次，之后每 1 小时", "#62E08C")
    win.sw_auto.set_on(True, animate=False)
    app.processEvents()
    win.grab().save(str(out.with_name("build_ui_running.png")))
    print("saved", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
