# -*- coding: utf-8 -*-
"""跨平台打开文件 / 目录封装。Windows 打包后走 os.startfile；Linux/macOS 用对应命令。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def open_file(path: str | Path) -> None:
    """用系统默认程序打开文件。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在：{p}")
    _open(p)


def open_directory(path: str | Path) -> None:
    """在资源管理器 / 文件管理器中打开目录。"""
    p = Path(path)
    if p.is_file():
        p = p.parent
    if not p.is_dir():
        raise FileNotFoundError(f"目录不存在：{p}")
    _open(p)


def reveal_in_folder(path: str | Path) -> None:
    """
    尽量在文件管理器中选中该文件；若平台不支持则退化为打开所在目录。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"路径不存在：{p}")
    if sys.platform == "win32":
        # explorer /select,<path>
        subprocess.run(["explorer", f"/select,{p}"], check=False)
        return
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(p)], check=False)
        return
    # Linux：打开父目录
    open_directory(p if p.is_dir() else p.parent)


def _open(path: Path) -> None:
    target = str(path.resolve())
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", target], check=False)
    else:
        subprocess.run(["xdg-open", target], check=False)
