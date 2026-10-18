# -*- coding: utf-8 -*-
"""Cloudflare 403 先用浏览器指纹重试，默认不读浏览器 Cookie。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yt_dlp import YoutubeDL
from yt_dlp.networking.impersonate import ImpersonateTarget

from core.downloader import (
    DownloadOptions,
    _opts_for_attempt,
    build_extract_attempts,
    download,
    list_formats,
)
from core.errors import IMPERSONATE_DEPENDENCY_MISSING, IMPERSONATE_STILL_BLOCKED, YOUTUBE_COOKIES_NEEDED
from tests.test_youtube_clients import KNOWN, YT, _install_fake, _FakeYDL

HANIME = "https://hanime1.me/watch?v=22673"
CF = RuntimeError(
    "ERROR: [generic] Got HTTP Error 403 caused by Cloudflare anti-bot challenge; "
    "see https://github.com/yt-dlp/yt-dlp#impersonation for how to install the "
    'required impersonation dependency, and try again with --extractor-args "generic:impersonate"'
)


def _generic_impersonate(opts: dict):
    return ((opts.get("extractor_args") or {}).get("generic") or {}).get("impersonate")


class ImpersonateAttemptTests(unittest.TestCase):
    def test_default_hanime_attempt_has_no_cookies_then_chrome_fingerprint(self) -> None:
        attempts = build_extract_attempts(HANIME, "", "不使用")
        self.assertEqual(len(attempts), 2)
        self.assertFalse(attempts[0].impersonate)
        self.assertTrue(attempts[1].impersonate)
        first = _opts_for_attempt({"quiet": True}, attempts[0])
        second = _opts_for_attempt({"quiet": True}, attempts[1])
        self.assertNotIn("cookiefile", first)
        self.assertNotIn("cookiesfrombrowser", first)
        self.assertNotIn("impersonate", first)
        self.assertNotIn("cookiesfrombrowser", second)
        self.assertNotIn("cookiefile", second)
        self.assertIsInstance(second["impersonate"], ImpersonateTarget)
        self.assertEqual(second["impersonate"].client, "chrome")
        self.assertEqual(_generic_impersonate(second), ["chrome"])
        self.assertIsNone(((second.get("extractor_args") or {}).get("youtube") or {}).get("player_client"))

    def test_ytdlp_accepts_impersonate_target_without_network(self) -> None:
        attempts = build_extract_attempts(HANIME)
        opts = _opts_for_attempt({"quiet": True, "skip_download": True, "noplaylist": True}, attempts[1])
        with YoutubeDL(opts) as ydl:
            self.assertEqual(ydl.params["impersonate"].client, "chrome")
            self.assertTrue(ydl._impersonate_target_available(ydl.params["impersonate"]))

    def test_cloudflare_403_retries_impersonate_then_says_cookies_are_optional(self) -> None:
        logs: list[str] = []
        with _install_fake([CF, CF]):
            with self.assertRaises(RuntimeError) as ctx:
                list_formats(HANIME, log=logs.append, cookiesfrombrowser="不使用")
        self.assertEqual(str(ctx.exception), IMPERSONATE_STILL_BLOCKED)
        self.assertIn("不能保证每个网站都能下载", str(ctx.exception))
        self.assertIn("浏览器指纹", str(ctx.exception))
        self.assertNotIn("pip install", str(ctx.exception))
        self.assertEqual(len(_FakeYDL.calls), 2)
        self.assertNotIn("impersonate", _FakeYDL.calls[0])
        self.assertNotIn("cookiesfrombrowser", _FakeYDL.calls[0])
        self.assertNotIn("cookiesfrombrowser", _FakeYDL.calls[1])
        self.assertEqual(_FakeYDL.calls[1]["impersonate"].client, "chrome")
        self.assertEqual(_generic_impersonate(_FakeYDL.calls[1]), ["chrome"])
        self.assertTrue(any("浏览器指纹" in line and "不使用 Cookie" in line for line in logs))

    def test_impersonate_retry_can_succeed_without_cookies(self) -> None:
        ok = {"title": "指纹之后", "id": "22673", "formats": [{"format_id": "1", "ext": "mp4"}]}
        logs: list[str] = []
        with _install_fake([RuntimeError("[Hanime1] 22673: Unable to download webpage: HTTP Error 403: Forbidden"), ok]):
            result = list_formats(HANIME, log=logs.append)
        self.assertEqual(result.title, "指纹之后")
        self.assertEqual(len(_FakeYDL.calls), 2)
        self.assertNotIn("cookiesfrombrowser", _FakeYDL.calls[1])
        self.assertEqual(_FakeYDL.calls[1]["extractor_args"]["generic"]["impersonate"], ["chrome"])

    def test_404_does_not_impersonate(self) -> None:
        with _install_fake([RuntimeError("HTTP Error 404: Not Found")]):
            with self.assertRaises(RuntimeError) as ctx:
                list_formats(HANIME)
        self.assertEqual(len(_FakeYDL.calls), 1)
        self.assertNotIn("impersonate", _FakeYDL.calls[0])
        self.assertIn("HTTP 404", str(ctx.exception))

    def test_missing_impersonation_dependency_is_chinese(self) -> None:
        missing = RuntimeError(
            'Impersonate target "chrome" is not available. '
            "Use --list-impersonate-targets to see available targets. "
            "You may be missing dependencies required to support this target."
        )
        with _install_fake([CF, missing]):
            with self.assertRaises(RuntimeError) as ctx:
                list_formats("https://example.com/cloudflare")
        self.assertEqual(str(ctx.exception), IMPERSONATE_DEPENDENCY_MISSING)
        self.assertIn("不需要再单独执行 pip install", str(ctx.exception))
        self.assertEqual(len(_FakeYDL.calls), 2)

    def test_explicit_browser_cookie_is_kept_but_not_forced_when_empty(self) -> None:
        attempts = build_extract_attempts("https://example.com/v", "", "")
        opts = [_opts_for_attempt({}, item) for item in attempts]
        self.assertTrue(all("cookiesfrombrowser" not in item for item in opts))
        self.assertTrue(opts[1]["impersonate"].client == "chrome")

    def test_youtube_403_chain_tries_impersonate_before_asking_for_cookies(self) -> None:
        script = [RuntimeError("HTTP Error 403: Forbidden")] * 5
        script.append({"title": "指纹客户端", "id": "x", "formats": []})
        with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
            with _install_fake(script):
                result = list_formats(YT)
        self.assertEqual(result.title, "指纹客户端")
        self.assertEqual(len(_FakeYDL.calls), 6)
        self.assertNotIn("impersonate", _FakeYDL.calls[0])
        self.assertNotIn("cookiesfrombrowser", _FakeYDL.calls[-1])
        self.assertEqual(_FakeYDL.calls[-1]["impersonate"].client, "chrome")
        self.assertEqual(_generic_impersonate(_FakeYDL.calls[-1]), ["chrome"])

    def test_youtube_login_wall_does_not_need_impersonate_before_cookie_message(self) -> None:
        script = [RuntimeError("Sign in to confirm you're not a bot")] * 6
        with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
            with _install_fake(script):
                with self.assertRaises(RuntimeError) as ctx:
                    list_formats(YT)
        self.assertEqual(str(ctx.exception), YOUTUBE_COOKIES_NEEDED)
        self.assertEqual(len(_FakeYDL.calls), 5)
        self.assertTrue(all("impersonate" not in call for call in _FakeYDL.calls))

    def test_download_uses_impersonate_after_403(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = [
                RuntimeError("HTTP Error 403: Forbidden"),
                {"title": "下好了", "id": "22673", "_filename": str(Path(tmp) / "a.mp4")},
            ]
            with _install_fake(script):
                result = download(DownloadOptions(url=HANIME, output_dir=tmp, video_format="best"))
        self.assertEqual(result.title, "下好了")
        self.assertNotIn("cookiesfrombrowser", _FakeYDL.calls[0])
        self.assertNotIn("cookiesfrombrowser", _FakeYDL.calls[1])
        self.assertEqual(_FakeYDL.calls[1]["impersonate"].client, "chrome")
        self.assertEqual(len(_FakeYDL.calls), 2)


class BundleSpecTests(unittest.TestCase):
    def test_windows_spec_bundles_curl_cffi_libs(self) -> None:
        text = Path("build_windows.spec").read_text(encoding="utf-8")
        self.assertIn('collect_all("curl_cffi")', text)
        self.assertIn("curl_cffi.libs", text)
        self.assertIn("libcurl-impersonate", text)
        self.assertIn("pyi_rth_curl_cffi.py", text)
        requirements = Path("requirements.txt").read_text(encoding="utf-8")
        self.assertIn("yt-dlp[default,curl-cffi]", requirements)
        workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
        self.assertIn("libcurl-impersonate", workflow)


if __name__ == "__main__":
    unittest.main()
