# -*- coding: utf-8 -*-
"""PyInstaller runtime hook：把随包 ffmpeg/bin 加入 PATH。"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _ensure_ffmpeg_on_path() -> None:
    # onedir: 可执行文件旁；onefile: _MEIPASS
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "ffmpeg" / "bin")
        candidates.append(Path(sys.executable).resolve().parent / "ffmpeg" / "bin")
    else:
        candidates.append(Path(__file__).resolve().parents[1] / "ffmpeg" / "bin")

    for bin_dir in candidates:
        if (bin_dir / "ffmpeg.exe").is_file() or (bin_dir / "ffmpeg").is_file():
            path = str(bin_dir)
            os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
            return


_ensure_ffmpeg_on_path()
