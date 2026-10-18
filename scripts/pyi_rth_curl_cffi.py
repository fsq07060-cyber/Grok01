# -*- coding: utf-8 -*-
"""PyInstaller runtime hook：让 curl_cffi 找到旁边的 libcurl-impersonate。

Windows 轮子用 delvewheel 把 DLL 放在 curl_cffi.libs，并在 import 时按
``dirname(__file__)/../curl_cffi.libs`` 调用 ``os.add_dll_directory``。
冻结后的 ``__file__`` 有时对不上这个相对路径，所以启动时再登记一次。
"""

from __future__ import annotations

import os
import sys


def _add(path: str) -> None:
    if not path or not os.path.isdir(path):
        return
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(path)
        except OSError:
            pass
    os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")


if getattr(sys, "frozen", False):
    base = getattr(sys, "_MEIPASS", "")
    _add(os.path.join(base, "curl_cffi.libs"))
    exe_dir = os.path.dirname(getattr(sys, "executable", "") or "")
    _add(os.path.join(exe_dir, "_internal", "curl_cffi.libs"))
    _add(os.path.join(exe_dir, "curl_cffi.libs"))
