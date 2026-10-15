# -*- coding: utf-8 -*-
"""添加任务不得在主线程等待磁盘。"""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

from core.task_model import TaskStatus


@unittest.skipUnless(os.environ.get("DISPLAY") or sys.platform == "win32", "needs a display")
class AddTaskUiTests(unittest.TestCase):
    def test_add_shows_row_while_mkdir_is_slow(self) -> None:
        import customtkinter  # noqa: F401
        from ui.app import App

        def slow_fail(self, *args, **kwargs):
            time.sleep(0.4)
            raise OSError(2, "The system cannot find the path specified")

        app = App()
        result: dict = {}

        def start() -> None:
            app.var_path.set("F:/下载")
            app.var_res.set("1440")
            app.var_vfmt.set("mp4")
            app.var_separate.set(False)
            app.txt_urls.insert("1.0", "https://www.youtube.com/watch?v=BmKFJgVLE-Q")
            started = time.perf_counter()
            app._on_add()
            result["elapsed"] = time.perf_counter() - started
            result["rows"] = len(app._rows)
            result["cleared"] = app.txt_urls.get("1.0", "end").strip()
            app.after(50, poll)

        deadline = time.time() + 6

        def poll() -> None:
            tasks = app._queue.tasks()
            if tasks and tasks[0].status == TaskStatus.FAILED:
                result["error"] = tasks[0].error
                result["status_text"] = app._rows[tasks[0].id].lbl_status.cget("text")
                app.quit()
                return
            if time.time() > deadline:
                result["error"] = None
                app.quit()
                return
            app.after(50, poll)

        try:
            with mock.patch("core.downloader.Path.mkdir", slow_fail):
                app.after(0, start)
                app.mainloop()
        finally:
            app._on_close()

        self.assertLess(result.get("elapsed", 99), 0.5, "添加任务阻塞了主线程")
        self.assertEqual(result.get("rows"), 1)
        self.assertEqual(result.get("cleared"), "")
        self.assertIn("无法创建保存目录", result.get("error") or "")
        self.assertIn("F:/下载", result.get("error") or "")
        self.assertIn("失败", result.get("status_text") or "")


if __name__ == "__main__":
    unittest.main()
