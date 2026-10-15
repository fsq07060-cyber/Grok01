# -*- coding: utf-8 -*-
"""启动时把随包 / 仓库内 ffmpeg/bin 加入 PATH（若存在）。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional


def _search_roots() -> list[Path]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass))
        roots.append(Path(sys.executable).resolve().parent)
    # 源码运行：项目根
    roots.append(Path(__file__).resolve().parents[1])
    return roots


def bundled_bin_dirs() -> list[Path]:
    found: list[Path] = []
    for root in _search_roots():
        bin_dir = root / "ffmpeg" / "bin"
        if bin_dir.is_dir() and bin_dir not in found:
            found.append(bin_dir)
    return found


def resolve_tool(name: str) -> Optional[str]:
    """找 ffmpeg / ffprobe。优先随包目录，其次 PATH。"""
    for bin_dir in bundled_bin_dirs():
        for candidate in (bin_dir / f"{name}.exe", bin_dir / name):
            if candidate.is_file():
                return str(candidate)
    return shutil.which(name) or shutil.which(f"{name}.exe")


def ensure_bundled_ffmpeg() -> None:
    for bin_dir in bundled_bin_dirs():
        if (bin_dir / "ffmpeg").is_file() or (bin_dir / "ffmpeg.exe").is_file():
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            return
