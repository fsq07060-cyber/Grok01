# -*- coding: utf-8 -*-
"""失效视频文案、探测逐条继续、Cookie 参数。"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core.downloader import DownloadOptions, DownloadResult, apply_cookie_options, build_format_selector
from core.errors import (
    IMPERSONATE_DEPENDENCY_MISSING,
    IMPERSONATE_STILL_BLOCKED,
    SITE_LIMIT_TIP,
    YOUTUBE_COOKIES_NEEDED,
    ProbeBatchResult,
    format_probe_failure_dialog,
    friendly_error,
    is_impersonate_retryable,
    is_youtube_client_retryable,
    run_probe_batch,
)
from core.task_model import DownloadTask, TaskStatus
from core import task_queue as tq


class FriendlyErrorTests(unittest.TestCase):
    def test_http_410_gone_message(self) -> None:
        exc = RuntimeError("ERROR: Unable to download webpage: HTTP Error 410: Gone")
        self.assertEqual(friendly_error(exc), "该视频已失效或已删除 (HTTP 410)")

    def test_410_gone_without_error_word(self) -> None:
        exc = RuntimeError("Unable to download webpage HTTP 410 Gone")
        self.assertEqual(friendly_error(exc), "该视频已失效或已删除 (HTTP 410)")

    def test_http_404(self) -> None:
        exc = RuntimeError("HTTP Error 404: Not Found")
        self.assertEqual(friendly_error(exc), "该视频已失效或已删除 (HTTP 404)")

    def test_status_code_on_cause(self) -> None:
        class HTTPGone(Exception):
            def __init__(self) -> None:
                super().__init__("Gone")
                self.code = 410

        outer = RuntimeError("Unable to download webpage")
        outer.__cause__ = HTTPGone()
        self.assertEqual(friendly_error(outer), "该视频已失效或已删除 (HTTP 410)")

    def test_bare_404_token(self) -> None:
        self.assertEqual(friendly_error(RuntimeError("status 404")), "该视频已失效或已删除 (HTTP 404)")

    def test_unrelated_error_stays_short(self) -> None:
        text = friendly_error(RuntimeError("ERROR: Unsupported URL: ftp://example"))
        self.assertEqual(text, "Unsupported URL: ftp://example")

    def test_bot_check_hints_cookies(self) -> None:
        exc = RuntimeError(
            "ERROR: [youtube] abc: Sign in to confirm you're not a bot. "
            "Use --cookies-from-browser or --cookies for the authentication."
        )
        text = friendly_error(exc)
        self.assertIn("Cookie", text)
        self.assertNotIn("http", text.lower())

    def test_cookie_load_failure(self) -> None:
        exc = RuntimeError("ERROR: failed to load cookies")
        self.assertIn("无法读取 Cookie", friendly_error(exc))

    def test_youtube_cookies_needed_wins_over_bot_cause(self) -> None:
        err = RuntimeError(YOUTUBE_COOKIES_NEEDED)
        err.__cause__ = RuntimeError("Sign in to confirm you're not a bot")
        self.assertEqual(friendly_error(err), YOUTUBE_COOKIES_NEEDED)

    def test_retryable_403_bot_and_cookie_not_410(self) -> None:
        self.assertTrue(
            is_youtube_client_retryable(RuntimeError("unable to download video data: HTTP Error 403: Forbidden"))
        )
        self.assertTrue(is_youtube_client_retryable(RuntimeError("Sign in to confirm you're not a bot")))
        self.assertTrue(is_youtube_client_retryable(RuntimeError("failed to load cookies")))
        self.assertTrue(
            is_youtube_client_retryable(
                RuntimeError("Playback on other websites has been disabled by the video owner")
            )
        )
        self.assertFalse(is_youtube_client_retryable(RuntimeError("HTTP Error 410: Gone")))
        self.assertFalse(is_youtube_client_retryable(RuntimeError("HTTP Error 404: Not Found")))
        self.assertFalse(is_youtube_client_retryable(RuntimeError("已取消")))
        self.assertFalse(is_youtube_client_retryable(RuntimeError("不支持的浏览器 Cookie：safari")))

    def test_cloudflare_403_is_impersonate_retryable_but_404_is_not(self) -> None:
        cf = RuntimeError(
            "ERROR: [generic] Got HTTP Error 403 caused by Cloudflare anti-bot challenge; "
            "see https://github.com/yt-dlp/yt-dlp#impersonation for how to install the "
            'required impersonation dependency, and try again with --extractor-args "generic:impersonate"'
        )
        self.assertTrue(is_impersonate_retryable(cf))
        self.assertTrue(is_impersonate_retryable(RuntimeError("HTTP Error 403: Forbidden")))
        self.assertFalse(is_impersonate_retryable(RuntimeError("HTTP Error 404: Not Found")))
        self.assertFalse(is_impersonate_retryable(RuntimeError("HTTP Error 410: Gone")))
        self.assertEqual(friendly_error(RuntimeError(IMPERSONATE_STILL_BLOCKED)), IMPERSONATE_STILL_BLOCKED)
        missing = RuntimeError(
            'Impersonate target "chrome" is not available. '
            "You may be missing dependencies required to support this target."
        )
        self.assertEqual(friendly_error(missing), IMPERSONATE_DEPENDENCY_MISSING)

    def test_site_limit_tip_is_honest(self) -> None:
        self.assertIn("不能保证每个网站都能下载", SITE_LIMIT_TIP)
        self.assertIn("年龄限制", SITE_LIMIT_TIP)
        self.assertIn("DRM", SITE_LIMIT_TIP)
        self.assertIn("Cloudflare", SITE_LIMIT_TIP)
        self.assertIn("浏览器 Cookie", SITE_LIMIT_TIP)
        self.assertNotIn("所有网站", SITE_LIMIT_TIP)


class ProbeBatchTests(unittest.TestCase):
    def test_first_410_does_not_skip_later_url(self) -> None:
        logs: list[str] = []

        def probe_one(url: str) -> str:
            if "gone" in url:
                raise RuntimeError("HTTP Error 410: Gone")
            return "可下标题"

        batch = run_probe_batch(
            ["https://example.test/gone", "https://example.test/ok"],
            probe_one,
            logs.append,
        )
        self.assertEqual(len(batch.failures), 1)
        self.assertEqual(batch.failures[0][0], "https://example.test/gone")
        self.assertIn("HTTP 410", batch.failures[0][1])
        self.assertEqual(batch.successes, [("https://example.test/ok", "可下标题")])
        self.assertEqual(batch.untested, [])
        dialog = format_probe_failure_dialog(batch)
        self.assertIn("https://example.test/gone", dialog)
        self.assertIn("HTTP 410", dialog)
        self.assertIn("其余 1 个链接已逐条探测", dialog)
        self.assertTrue(any("https://example.test/gone" in line for line in logs))
        self.assertTrue(any("探测成功" in line and "https://example.test/ok" in line for line in logs))

    def test_cookie_failure_leaves_rest_untested(self) -> None:
        def probe_one(url: str) -> str:
            raise RuntimeError("failed to load cookies")

        batch = run_probe_batch(
            ["https://example.test/a", "https://example.test/b"],
            probe_one,
            lambda _m: None,
        )
        self.assertEqual(len(batch.failures), 1)
        self.assertEqual(batch.untested, ["https://example.test/b"])
        dialog = format_probe_failure_dialog(batch)
        self.assertIn("https://example.test/a", dialog)
        self.assertIn("https://example.test/b", dialog)
        self.assertIn("未探测", dialog)

    def test_single_failure_does_not_mention_other_urls(self) -> None:
        batch = ProbeBatchResult(
            failures=[("https://example.test/only", "该视频已失效或已删除 (HTTP 410)")]
        )
        dialog = format_probe_failure_dialog(batch)
        self.assertIn("https://example.test/only", dialog)
        self.assertNotIn("其余", dialog)
        self.assertNotIn("未探测", dialog)


class CookieOptionTests(unittest.TestCase):
    def test_defaults_add_no_cookie_keys(self) -> None:
        opts = apply_cookie_options({"quiet": True}, "", "不使用")
        self.assertEqual(opts, {"quiet": True})
        self.assertNotIn("cookiefile", opts)
        self.assertNotIn("cookiesfrombrowser", opts)

    def test_browser_and_file(self) -> None:
        with tempfile.NamedTemporaryFile() as fh:
            opts = apply_cookie_options({}, fh.name, "Chrome")
        self.assertEqual(opts["cookiefile"], fh.name)
        self.assertEqual(opts["cookiesfrombrowser"], ("chrome", None, None, None))

    def test_edge_alias(self) -> None:
        opts = apply_cookie_options({}, "", "msedge")
        self.assertEqual(opts["cookiesfrombrowser"], ("edge", None, None, None))

    def test_missing_cookie_file(self) -> None:
        missing = str(Path(tempfile.gettempdir()) / "stream-downloader-missing-cookies.txt")
        with self.assertRaises(RuntimeError) as ctx:
            apply_cookie_options({}, missing, "")
        self.assertIn("Cookie 文件不存在", str(ctx.exception))

    def test_unknown_browser(self) -> None:
        with self.assertRaises(RuntimeError):
            apply_cookie_options({}, "", "safari")

    def test_format_selector_unchanged_for_default_options(self) -> None:
        opts = DownloadOptions(url="https://example.test/v", output_dir=".")
        self.assertEqual(opts.cookiefile, "")
        self.assertEqual(opts.cookiesfrombrowser, "")
        spec = build_format_selector(opts)
        self.assertIn("height<=?1080", spec)
        self.assertIn("ba[ext=m4a]", spec)
        for branch in spec.split("/"):
            if branch.startswith("bv"):
                self.assertIn("+", branch, branch)
            else:
                self.assertIn("acodec!=none", branch)
                self.assertIn("vcodec!=none", branch)


class QueueFailureLogTests(unittest.TestCase):
    def test_failure_log_has_url_and_short_reason_and_continues(self) -> None:
        logs: list[str] = []
        calls: list[str] = []

        def fake_download(opts, log=None, cancel_event=None, on_progress=None):
            calls.append(opts.url)
            if "gone" in opts.url:
                raise RuntimeError("ERROR: Unable to download webpage: HTTP Error 410: Gone")
            return DownloadResult(title="第二条", filepath=None, output_dir=opts.output_dir)

        url_gone = "https://example.test/gone"
        url_ok = "https://example.test/ok"
        with mock.patch.object(tq, "download", fake_download):
            queue = tq.DownloadQueue(max_workers=1, on_log=logs.append)
            queue.start()
            try:
                first = queue.add(DownloadTask(url=url_gone, output_dir="."))
                second = queue.add(DownloadTask(url=url_ok, output_dir="."))
                deadline = time.time() + 5
                while time.time() < deadline:
                    a = queue.get(first.id)
                    b = queue.get(second.id)
                    if (
                        a
                        and b
                        and a.status in (TaskStatus.FAILED, TaskStatus.COMPLETED)
                        and b.status in (TaskStatus.FAILED, TaskStatus.COMPLETED)
                    ):
                        break
                    time.sleep(0.05)
                a = queue.get(first.id)
                b = queue.get(second.id)
            finally:
                queue.stop()

        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        assert a is not None and b is not None
        self.assertEqual(a.status, TaskStatus.FAILED)
        self.assertEqual(a.error, "该视频已失效或已删除 (HTTP 410)")
        self.assertEqual(b.status, TaskStatus.COMPLETED)
        self.assertEqual(calls, [url_gone, url_ok])
        joined = "\n".join(logs)
        self.assertIn(url_gone, joined)
        self.assertIn("该视频已失效或已删除 (HTTP 410)", joined)


class OutputDirTests(unittest.TestCase):
    def test_bad_parent_fails_with_chinese_message(self) -> None:
        from core.downloader import ensure_output_dir

        with tempfile.NamedTemporaryFile() as fh:
            bad = str(Path(fh.name) / "nested")
            with self.assertRaises(RuntimeError) as ctx:
                ensure_output_dir(bad, timeout=3)
        self.assertIn("无法创建保存目录", str(ctx.exception))
        self.assertIn(bad, str(ctx.exception))

    def test_slow_mkdir_times_out_off_caller(self) -> None:
        from core.downloader import ensure_output_dir

        def hang(self, *args, **kwargs):
            time.sleep(2)

        started = time.perf_counter()
        with mock.patch("core.downloader.Path.mkdir", hang):
            with self.assertRaises(RuntimeError) as ctx:
                ensure_output_dir("F:/下载", timeout=0.4)
        self.assertIn("保存目录无响应", str(ctx.exception))
        self.assertIn("F:/下载", str(ctx.exception))
        self.assertLess(time.perf_counter() - started, 1.5)

    def test_download_does_not_open_ytdlp_when_dir_fails(self) -> None:
        from core.downloader import download

        with tempfile.NamedTemporaryFile() as fh:
            bad = str(Path(fh.name) / "nested")
            with mock.patch(
                "core.downloader._import_yt_dlp",
                side_effect=AssertionError("yt-dlp should not load"),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    download(DownloadOptions(url="https://example.test/v", output_dir=bad))
        self.assertIn("无法创建保存目录", str(ctx.exception))

    def test_add_returns_while_worker_is_blocked(self) -> None:
        release = threading.Event()

        def blocked(opts, log=None, cancel_event=None, on_progress=None):
            if log:
                log("worker-entered")
            release.wait(3)
            return DownloadResult(title="x", filepath=None, output_dir=opts.output_dir)

        with mock.patch.object(tq, "download", blocked):
            queue = tq.DownloadQueue(max_workers=1)
            queue.start()
            try:
                started = time.perf_counter()
                task = queue.add(
                    DownloadTask(url="https://www.youtube.com/watch?v=BmKFJgVLE-Q", output_dir="F:/下载")
                )
                self.assertLess(time.perf_counter() - started, 0.4)
                self.assertEqual(len(queue.tasks()), 1)
                self.assertIn(queue.get(task.id).status, (TaskStatus.QUEUED, TaskStatus.DOWNLOADING))
            finally:
                release.set()
                queue.stop()

    def test_status_callback_can_read_tasks_without_deadlock(self) -> None:
        holder: dict = {}
        saw_downloading = threading.Event()

        def on_update(task: DownloadTask) -> None:
            holder["q"].tasks()
            if task.status == TaskStatus.DOWNLOADING:
                saw_downloading.set()

        def fake(opts, log=None, cancel_event=None, on_progress=None):
            return DownloadResult(title="ok", filepath=None, output_dir=opts.output_dir)

        with mock.patch.object(tq, "download", fake):
            queue = tq.DownloadQueue(max_workers=1, on_update=on_update)
            holder["q"] = queue
            queue.start()
            try:
                queue.add(DownloadTask(url="https://example.test/a", output_dir="."))
                self.assertTrue(
                    saw_downloading.wait(2),
                    "status callback deadlocked while reading the task list",
                )
            finally:
                queue.stop()


class ProgressThrottleTests(unittest.TestCase):
    def test_hook_drops_burst(self) -> None:
        from core.downloader import _ProgressHook

        logs: list[str] = []
        hook = _ProgressHook(logs.append)
        for i in range(40):
            hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": i,
                    "total_bytes": 1000,
                    "_speed_str": "1MiB/s",
                    "_eta_str": "10s",
                }
            )
        hook({"status": "finished", "filename": "a.mp4"})
        self.assertLess(len(logs), 15)
        self.assertGreaterEqual(len(logs), 2)
        self.assertTrue(any("后处理" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
