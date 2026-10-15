# -*- coding: utf-8 -*-
"""启动时把随包 / 仓库内 ffmpeg/bin 加入 PATH（若存在）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def ensure_bundled_ffmpeg() -> None:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass))
        roots.append(Path(sys.executable).resolve().parent)
    # 源码运行：项目根
    roots.append(Path(__file__).resolve().parents[1])

    for root in roots:
        bin_dir = root / "ffmpeg" / "bin"
        exe = bin_dir / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        if exe.is_file() or (bin_dir / "ffmpeg").is_file() or (bin_dir / "ffmpeg.exe").is_file():
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            return
