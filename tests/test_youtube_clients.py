# -*- coding: utf-8 -*-
"""YouTube 无 Cookie 客户端与自动重试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.downloader import (
    DownloadOptions,
    build_extract_attempts,
    download,
    is_youtube_url,
    list_formats,
)
from core.errors import FORMAT_NEEDS_AUDIO, YOUTUBE_COOKIES_NEEDED

YT = "https://www.youtube.com/watch?v=BmKFJgVLE-Q"
KNOWN = {
    "tv_samsung",
    "visionos",
    "web",
    "web_embedded",
    "tv_downgraded",
    "android",
    "ios",
    "mweb",
    "tv_embedded",
}


class _FakeYDL:
    calls: list[dict] = []
    script: list = []

    def __init__(self, opts: dict) -> None:
        self.opts = opts
        type(self).calls.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def extract_info(self, url: str, download: bool = False):
        action = type(self).script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action

    def prepare_filename(self, info: dict) -> str:
        return info.get("_filename") or "video.mp4"


def _install_fake(script: list):
    _FakeYDL.calls = []
    _FakeYDL.script = list(script)

    class Module:
        YoutubeDL = _FakeYDL

    return mock.patch("core.downloader._import_yt_dlp", return_value=Module)


def _clients(opts: dict) -> list[str] | None:
    args = opts.get("extractor_args") or {}
    return (args.get("youtube") or {}).get("player_client")


class YoutubeAttemptTests(unittest.TestCase):
    def test_hosts(self) -> None:
        self.assertTrue(is_youtube_url(YT))
        self.assertTrue(is_youtube_url("https://youtu.be/BmKFJgVLE-Q"))
        self.assertTrue(is_youtube_url("https://m.youtube.com/watch?v=abc"))
        self.assertTrue(is_youtube_url("https://music.youtube.com/watch?v=abc"))
        self.assertTrue(is_youtube_url("https://www.youtube-nocookie.com/embed/abc"))
        self.assertFalse(is_youtube_url("https://example.com/watch?v=abc"))
        self.assertFalse(is_youtube_url("https://notyoutube.com/watch?v=abc"))

    def test_chain_prefers_2026_clients_and_skips_po_token_clients(self) -> None:
        with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
            attempts = build_extract_attempts(YT, "", "不使用")
        clients = [a.player_clients for a in attempts]
        self.assertEqual(
            clients,
            [
                ("tv_samsung", "visionos", "web"),
                ("visionos",),
                ("tv_samsung",),
                ("web_embedded",),
                ("tv_downgraded",),
            ],
        )
        flat = {name for group in clients for name in group}
        self.assertNotIn("android", flat)
        self.assertNotIn("ios", flat)
        self.assertNotIn("mweb", flat)
        self.assertNotIn("tv_embedded", flat)
        self.assertTrue(all(not a.cookiefile and not a.cookiesfrombrowser for a in attempts))

    def test_user_cookies_are_tried_before_nocookie_clients(self) -> None:
        with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
            attempts = build_extract_attempts(YT, "C:/cookies.txt", "chrome")
        self.assertEqual(attempts[0].cookiefile, "C:/cookies.txt")
        self.assertEqual(attempts[0].cookiesfrombrowser, "chrome")
        self.assertEqual(attempts[0].player_clients, ())
        self.assertEqual(attempts[1].player_clients, ("tv_samsung", "visionos", "web"))
        self.assertEqual(attempts[1].cookiefile, "")
        self.assertEqual(attempts[1].cookiesfrombrowser, "")

    def test_unknown_client_is_dropped(self) -> None:
        with mock.patch(
            "core.downloader._supported_player_clients",
            return_value={"visionos", "web", "web_embedded"},
        ):
            attempts = build_extract_attempts("https://youtu.be/abc")
        self.assertEqual(attempts[0].player_clients, ("visionos", "web"))
        self.assertEqual(attempts[1].player_clients, ("visionos",))
        self.assertEqual(attempts[2].player_clients, ("web_embedded",))

    def test_non_youtube_is_single_attempt_without_player_client(self) -> None:
        attempts = build_extract_attempts("https://example.com/v", "", "chrome")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].player_clients, ())
        self.assertEqual(attempts[0].cookiesfrombrowser, "chrome")


class YoutubeRetryTests(unittest.TestCase):
    def test_probe_sets_recommended_client_and_stops_on_success(self) -> None:
        info = {"title": "演示", "id": "BmKFJgVLE-Q", "formats": [{"format_id": "18", "ext": "mp4"}]}
        with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
            with _install_fake([info]):
                result = list_formats(YT)
        self.assertEqual(result.title, "演示")
        self.assertEqual(len(_FakeYDL.calls), 1)
        self.assertEqual(_clients(_FakeYDL.calls[0]), ["tv_samsung", "visionos", "web"])
        self.assertNotIn("cookiefile", _FakeYDL.calls[0])
        self.assertNotIn("cookiesfrombrowser", _FakeYDL.calls[0])
        self.assertEqual(_FakeYDL.calls[0]["js_runtimes"], {"deno": {}, "node": {}})

    def test_missing_cookie_file_falls_through(self) -> None:
        ok = {"title": "文件缺失后仍探测", "id": "x", "formats": []}
        with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
            with _install_fake([ok]):
                result = list_formats(YT, cookiefile=r"C:\no-such-cookies.txt")
        self.assertEqual(result.title, "文件缺失后仍探测")
        self.assertEqual(len(_FakeYDL.calls), 1)
        self.assertNotIn("cookiefile", _FakeYDL.calls[0])
        self.assertEqual(_clients(_FakeYDL.calls[0]), ["tv_samsung", "visionos", "web"])

    def test_unknown_browser_is_not_retried(self) -> None:
        with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
            with _install_fake([]):
                with self.assertRaises(RuntimeError) as ctx:
                    list_formats(YT, cookiesfrombrowser="safari")
        self.assertIn("不支持的浏览器", str(ctx.exception))
        self.assertEqual(_FakeYDL.calls, [])

    def test_403_retries_next_client_then_succeeds(self) -> None:
        logs: list[str] = []
        ok = {"title": "第二次", "id": "x", "formats": []}
        script = [
            RuntimeError("ERROR: unable to download video data: HTTP Error 403: Forbidden"),
            ok,
        ]
        with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
            with _install_fake(script):
                result = list_formats(YT, log=logs.append)
        self.assertEqual(result.title, "第二次")
        self.assertEqual(_clients(_FakeYDL.calls[0]), ["tv_samsung", "visionos", "web"])
        self.assertEqual(_clients(_FakeYDL.calls[1]), ["visionos"])
        self.assertTrue(any("HTTP 403" in line for line in logs))
        self.assertTrue(any("visionos" in line for line in logs))

    def test_cookie_read_failure_falls_through_without_cookies(self) -> None:
        ok = {"title": "无 Cookie 成功", "id": "x", "formats": []}
        script = [RuntimeError("ERROR: failed to load cookies"), ok]
        with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
            with _install_fake(script):
                result = list_formats(YT, cookiesfrombrowser="chrome")
        self.assertEqual(result.title, "无 Cookie 成功")
        self.assertEqual(_FakeYDL.calls[0]["cookiesfrombrowser"], ("chrome", None, None, None))
        self.assertNotIn("cookiesfrombrowser", _FakeYDL.calls[1])
        self.assertEqual(_clients(_FakeYDL.calls[1]), ["tv_samsung", "visionos", "web"])

    def test_410_does_not_retry(self) -> None:
        with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
            with _install_fake([RuntimeError("HTTP Error 410: Gone")]):
                with self.assertRaises(RuntimeError) as ctx:
                    list_formats(YT)
        self.assertIn("HTTP 410", str(ctx.exception))
        self.assertEqual(len(_FakeYDL.calls), 1)
        self.assertNotIn("不用 Cookie 仍无法下载", str(ctx.exception))

    def test_exhausted_chain_asks_for_cookies_in_chinese(self) -> None:
        script = [RuntimeError("Sign in to confirm you're not a bot")] * 5
        with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
            with _install_fake(script):
                with self.assertRaises(RuntimeError) as ctx:
                    list_formats("https://youtu.be/BmKFJgVLE-Q")
        self.assertEqual(str(ctx.exception), YOUTUBE_COOKIES_NEEDED)
        self.assertIn("年龄限制", str(ctx.exception))
        self.assertEqual(len(_FakeYDL.calls), 5)
        self.assertEqual(_clients(_FakeYDL.calls[-1]), ["tv_downgraded"])

    def test_format_unavailable_retries_clients_then_chinese(self) -> None:
        script = [RuntimeError("ERROR: Requested format is not available. Use --list-formats")] * 5
        with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
            with _install_fake(script):
                with self.assertRaises(RuntimeError) as ctx:
                    list_formats(YT)
        self.assertEqual(str(ctx.exception), FORMAT_NEEDS_AUDIO)
        self.assertNotIn("不用 Cookie", str(ctx.exception))
        self.assertEqual(len(_FakeYDL.calls), 5)
        self.assertEqual(_clients(_FakeYDL.calls[-1]), ["tv_downgraded"])

    def test_non_youtube_format_unavailable_does_not_retry(self) -> None:
        with _install_fake([RuntimeError("Requested format is not available")]):
            with self.assertRaises(RuntimeError) as ctx:
                list_formats("https://example.com/v")
        self.assertEqual(len(_FakeYDL.calls), 1)
        self.assertEqual(str(ctx.exception), FORMAT_NEEDS_AUDIO)
        self.assertIsNone(_clients(_FakeYDL.calls[0]))

    def test_non_youtube_403_does_not_retry_or_set_player_client(self) -> None:
        with _install_fake([RuntimeError("HTTP Error 403: Forbidden")]):
            with self.assertRaises(RuntimeError) as ctx:
                list_formats("https://example.com/v")
        self.assertEqual(len(_FakeYDL.calls), 1)
        self.assertIsNone(_clients(_FakeYDL.calls[0]))
        self.assertNotIn(YOUTUBE_COOKIES_NEEDED, str(ctx.exception))

    def test_download_retries_same_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = [
                RuntimeError("HTTP Error 403: Forbidden"),
                {"title": "下好了", "id": "x", "_filename": str(Path(tmp) / "a.mp4")},
            ]
            with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
                with _install_fake(script):
                    result = download(
                        DownloadOptions(url=YT, output_dir=tmp, video_format="best")
                    )
        self.assertEqual(result.title, "下好了")
        self.assertEqual(_clients(_FakeYDL.calls[0]), ["tv_samsung", "visionos", "web"])
        self.assertEqual(_clients(_FakeYDL.calls[1]), ["visionos"])
        self.assertEqual(len(_FakeYDL.calls), 2)


if __name__ == "__main__":
    unittest.main()
