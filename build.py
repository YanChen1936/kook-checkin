"""打包：PyInstaller onedir（不再 onefile 解包到 %TEMP%）。

    python build.py          # 生成 dist/KOOK打卡/
    python build.py --debug  # 带控制台窗口，便于看错误

onedir 的意义：旧版 onefile 每次运行把 playwright 驱动解包到 %TEMP%/_MEIxxxx，
Windows 清理临时文件后 node.exe 被删，于是每小时报 [WinError 2] 找不到文件。
onedir 目录常驻磁盘，进程运行期间不依赖任何临时目录。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "KOOK打卡"

# 这些 Qt 模块本项目完全不用，排除后能少几十 MB
EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQml",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSql",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtTest",
    "tkinter",
    "unittest",
    "pydoc_data",
    "lib2to3",
]


# PyInstaller 的 PySide6 hook 会无视 --exclude-module 塞进这些 Qt 运行库；
# 它们的 Python 绑定已被排除，留着只是体积。删之前都确认过本项目不 import。
PRUNE_FILES = [
    "opengl32sw.dll",       # OpenGL 软件渲染兜底，约 20MB
    "Qt6Pdf.dll",
    "Qt6Quick.dll",
    "Qt6Qml.dll",
    "Qt6QmlModels.dll",
    "Qt6QmlMeta.dll",
    "Qt6QmlWorkerScript.dll",
    "Qt6QuickControls2.dll",
    "Qt6QuickTemplates2.dll",
    "Qt6VirtualKeyboard.dll",
    "Qt6VirtualKeyboardQml.dll",
    "Qt6VirtualKeyboardSettings.dll",
    "d3dcompiler_47.dll",
]
PRUNE_GLOBS = ["Qt6Quick*", "Qt6Qml*", "Qt6VirtualKeyboard*", "Qt6Pdf*"]
PRUNE_DIRS = ["translations"]


def prune(app_dir: Path) -> None:
    """删掉用不到的 Qt 运行库，减体积；失败不影响构建。"""
    internal = app_dir / "_internal"
    removed = 0
    for name in PRUNE_FILES:
        for p in internal.rglob(name):
            try:
                removed += p.stat().st_size
                p.unlink()
            except OSError:
                pass
    for pattern in PRUNE_GLOBS:
        for p in internal.rglob(pattern):
            if p.is_file():
                try:
                    removed += p.stat().st_size
                    p.unlink()
                except OSError:
                    pass
    for name in PRUNE_DIRS:
        for p in internal.rglob(name):
            if p.is_dir():
                removed += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                shutil.rmtree(p, ignore_errors=True)
    if removed:
        print(f"裁剪掉 {removed / 1024 / 1024:.1f} MB 未使用的 Qt 组件")


def main() -> int:
    debug = "--debug" in sys.argv
    dist = ROOT / "dist"
    build = ROOT / "build"
    for path in (dist / NAME, build):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    args = [
        str(ROOT / "run.py"),
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        NAME,
        "--icon",
        str(ROOT / "assets" / "kook_icon.ico"),
        "--add-data",
        f"{ROOT / 'assets'}{os.pathsep}assets",
        "--version-file",
        str(ROOT / "version_info.txt"),
        "--collect-all",
        "playwright",
        "--distpath",
        str(dist),
        "--workpath",
        str(build),
        "--specpath",
        str(build),
    ]
    if not debug:
        args.append("--windowed")
    for module in EXCLUDES:
        args.extend(["--exclude-module", module])

    import PyInstaller.__main__

    PyInstaller.__main__.run(args)
    prune(dist / NAME)
    exe = dist / NAME / f"{NAME}.exe"
    print(f"\n构建完成: {exe}  存在={exe.exists()}")
    if exe.exists():
        total = sum(p.stat().st_size for p in (dist / NAME).rglob("*") if p.is_file())
        print(f"目录体积: {total / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
