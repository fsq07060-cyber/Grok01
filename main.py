#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流媒体下载器入口：启动 customtkinter GUI。"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from core.ffmpeg_path import ensure_bundled_ffmpeg

        ensure_bundled_ffmpeg()
    except Exception:
        pass

    try:
        import customtkinter  # noqa: F401
    except ImportError:
        print(
            "未安装 customtkinter。请先执行：\n"
            "  pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    from ui.app import App

    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
