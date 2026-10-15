# -*- coding: utf-8 -*-
"""默认下载必须合并音轨，不能再落到只有画面的 AV1 mp4。"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.downloader import (
    DownloadOptions,
    build_audio_retry_selector,
    build_format_selector,
    download,
    file_has_audio,
    format_sort_for,
    select_downloaded_file,
)
from core.errors import FFMPEG_REQUIRED, FORMAT_NEEDS_AUDIO, NO_AUDIO_TRACK, friendly_error
from core.ffmpeg_path import resolve_tool
from tests.test_youtube_clients import KNOWN, YT, _install_fake

try:
    from yt_dlp import YoutubeDL
except ImportError:  # pragma: no cover
    YoutubeDL = None


def _fmt(**kwargs: object) -> dict:
    item = {
        "protocol": "https",
        "url": "https://example.com/v",
        "format": kwargs["format_id"],
    }
    item.update(kwargs)
    return item


# 与 Takashi 那条类似：1080 AV1 只有画面，同时有 1080 H.264、aac 和一条 360p 合体流。
YOUTUBE_LIKE = [
    _fmt(format_id="399", ext="mp4", vcodec="av01.0.08M.08", acodec="none", height=1080, width=1920, tbr=2000),
    _fmt(format_id="137", ext="mp4", vcodec="avc1.640028", acodec="none", height=1080, width=1920, tbr=2500),
    _fmt(format_id="248", ext="webm", vcodec="vp9", acodec="none", height=1080, width=1920, tbr=1800),
    _fmt(format_id="140", ext="m4a", vcodec="none", acodec="mp4a.40.2", abr=128, tbr=128),
    _fmt(format_id="251", ext="webm", vcodec="none", acodec="opus", abr=160, tbr=160),
    _fmt(format_id="18", ext="mp4", vcodec="avc1.42001E", acodec="mp4a.40.2", height=360, width=640, tbr=500),
]


def _pick(opts: DownloadOptions, formats: list[dict], *, incomplete: bool = False) -> dict:
    if YoutubeDL is None:
        raise unittest.SkipTest("yt-dlp is not installed")
    ydl_opts: dict = {"quiet": True, "no_warnings": True}
    sort = format_sort_for(opts)
    if sort:
        ydl_opts["format_sort"] = sort
    container = opts.video_format if opts.video_format in ("mp4", "webm", "mkv") else "mp4"
    if not (opts.audio_only or opts.resolution == "audio"):
        ydl_opts["merge_output_format"] = container
    ydl = YoutubeDL(ydl_opts)
    raw = {"id": "SIKo1d8IakQ", "title": "t", "formats": [dict(item) for item in formats]}
    ydl.sort_formats(raw)
    selector = ydl.build_format_selector(build_format_selector(opts))
    ctx = {
        "formats": raw["formats"],
        "incomplete_formats": incomplete,
        "has_merged_format": any(
            item.get("vcodec") not in (None, "none") and item.get("acodec") not in (None, "none")
            for item in raw["formats"]
        ),
    }
    picked = list(selector(ctx))
    if not picked:
        return {}
    return picked[0]


def _ids(picked: dict) -> str:
    if not picked:
        return ""
    req = picked.get("requested_formats") or [picked]
    return "+".join(str(item.get("format_id")) for item in req)


class FormatSelectorTests(unittest.TestCase):
    def test_default_mp4_merges_h264_and_aac_not_silent_av1(self) -> None:
        opts = DownloadOptions(url=YT, output_dir=".")
        spec = build_format_selector(opts)
        # 旧写法把 bv*[ext=mp4] 放在 `+` 的斜线左边，yt-dlp 会先收下 399 这条无声 AV1。
        self.assertNotIn("bv*[height<=?1080][ext=mp4]/", spec)
        picked = _pick(opts, YOUTUBE_LIKE, incomplete=True)
        self.assertEqual(_ids(picked), "137+140")
        self.assertEqual(picked.get("acodec"), "mp4a.40.2")
        self.assertNotEqual(picked.get("acodec"), "none")
        self.assertTrue(str(picked.get("vcodec", "")).startswith("avc1"))

    def test_av1_plus_aac_when_h264_video_is_missing(self) -> None:
        formats = [item for item in YOUTUBE_LIKE if item["format_id"] != "137"]
        opts = DownloadOptions(url=YT, output_dir=".", resolution="1080", video_format="mp4")
        picked = _pick(opts, formats)
        self.assertEqual(_ids(picked), "399+140")
        self.assertNotEqual(picked.get("acodec"), "none")
        self.assertEqual(picked.get("height"), 1080)

    def test_video_only_formats_are_not_selected(self) -> None:
        formats = [item for item in YOUTUBE_LIKE if item["acodec"] == "none"]
        opts = DownloadOptions(url=YT, output_dir=".")
        picked = _pick(opts, formats)
        self.assertEqual(picked, {})

    def test_combined_file_is_used_when_it_is_the_only_audio(self) -> None:
        formats = [item for item in YOUTUBE_LIKE if item["format_id"] in ("399", "18")]
        opts = DownloadOptions(url=YT, output_dir=".")
        picked = _pick(opts, formats)
        self.assertEqual(_ids(picked), "18")
        self.assertNotEqual(picked.get("acodec"), "none")

    def test_opus_request_still_prefers_m4a_for_mp4(self) -> None:
        opts = DownloadOptions(url=YT, output_dir=".", video_format="mp4", audio_format="opus")
        spec = build_format_selector(opts)
        self.assertLess(spec.index("ba[ext=m4a]"), spec.index("ba[acodec^=opus]"))
        picked = _pick(opts, YOUTUBE_LIKE)
        self.assertEqual(_ids(picked), "137+140")

    def test_webm_merges_vp9_and_opus(self) -> None:
        opts = DownloadOptions(url=YT, output_dir=".", video_format="webm", audio_format="opus")
        picked = _pick(opts, YOUTUBE_LIKE)
        self.assertEqual(_ids(picked), "248+251")
        self.assertEqual(picked.get("acodec"), "opus")

    def test_audio_only_selector_stays_audio(self) -> None:
        opts = DownloadOptions(url=YT, output_dir=".", resolution="audio", audio_format="m4a")
        self.assertEqual(build_format_selector(opts), "ba[ext=m4a]/bestaudio/best")
        self.assertIsNone(format_sort_for(opts))

    def test_retry_selector_also_requires_audio(self) -> None:
        opts = DownloadOptions(url=YT, output_dir=".")
        spec = build_audio_retry_selector(opts)
        self.assertNotEqual(spec, build_format_selector(opts))
        for branch in spec.split("/"):
            if branch.startswith("bv"):
                self.assertIn("+", branch)
            else:
                self.assertIn("acodec!=none", branch)


class ProbeAndRetryTests(unittest.TestCase):
    def test_ffprobe_distinguishes_silent_video(self) -> None:
        ffmpeg = resolve_tool("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            audible = Path(tmp) / "with.mp4"
            silent = Path(tmp) / "silent.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=160x120:rate=10",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440",
                    "-t",
                    "0.3",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(audible),
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=160x120:rate=10",
                    "-t",
                    "0.3",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-an",
                    str(silent),
                ],
                check=True,
                capture_output=True,
            )
            self.assertTrue(file_has_audio(str(audible)))
            self.assertFalse(file_has_audio(str(silent)))
            self.assertIsNone(file_has_audio(str(Path(tmp) / "missing.mp4")))

    def test_prefers_merged_file_with_audio_over_silent_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fragment = root / "演示 [SIKo1d8IakQ].f399.mp4"
            merged = root / "演示 [SIKo1d8IakQ].mp4"
            fragment.write_bytes(b"video")
            merged.write_bytes(b"video+audio")

            def probe(path: str) -> bool:
                return "f399" not in path

            with mock.patch("core.downloader.file_has_audio", side_effect=probe):
                chosen, has_audio = select_downloaded_file(
                    root, str(fragment), "SIKo1d8IakQ", "mp4"
                )
                from core.downloader import drop_silent_siblings

                assert chosen is not None
                drop_silent_siblings(root, str(fragment), "SIKo1d8IakQ", chosen, lambda _m: None)
            self.assertTrue(has_audio)
            self.assertEqual(chosen, merged)
            self.assertFalse(fragment.exists())
            self.assertTrue(merged.exists())

    def test_download_keeps_file_when_audio_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "有声 [SIKo1d8IakQ].mp4"
            target.write_bytes(b"audio")
            script = [{"title": "有声", "id": "SIKo1d8IakQ", "_filename": str(target)}]
            with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
                with mock.patch("core.downloader.file_has_audio", return_value=True):
                    with _install_fake(script):
                        result = download(DownloadOptions(url=YT, output_dir=tmp))
            self.assertEqual(result.filepath, str(target))
            from tests.test_youtube_clients import _FakeYDL

            self.assertEqual(len(_FakeYDL.calls), 1)
            self.assertIn("+", _FakeYDL.calls[0]["format"])
            self.assertEqual(
                _FakeYDL.calls[0]["format_sort"],
                ["res", "fps", "vcodec:h264", "acodec:aac"],
            )
            self.assertTrue(target.is_file())

    def test_silent_download_retries_then_fails_in_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "无声 [SIKo1d8IakQ].mp4"
            target.write_bytes(b"silent")
            script = [
                {"title": "无声", "id": "SIKo1d8IakQ", "_filename": str(target)},
                {"title": "仍无声", "id": "SIKo1d8IakQ", "_filename": str(target)},
            ]
            logs: list[str] = []
            with mock.patch("core.downloader._supported_player_clients", return_value=set(KNOWN)):
                with mock.patch("core.downloader.file_has_audio", return_value=False):
                    with _install_fake(script):
                        with self.assertRaises(RuntimeError) as ctx:
                            download(
                                DownloadOptions(url=YT, output_dir=tmp),
                                log=logs.append,
                            )
            from tests.test_youtube_clients import _FakeYDL

            self.assertEqual(str(ctx.exception), NO_AUDIO_TRACK)
            self.assertEqual(friendly_error(ctx.exception), NO_AUDIO_TRACK)
            self.assertEqual(len(_FakeYDL.calls), 2)
            self.assertNotEqual(_FakeYDL.calls[0]["format"], _FakeYDL.calls[1]["format"])
            self.assertTrue(_FakeYDL.calls[1].get("overwrites"))
            self.assertTrue(any("没有音轨" in line for line in logs))
            self.assertFalse(target.exists())

    def test_messages(self) -> None:
        self.assertEqual(friendly_error(RuntimeError(FORMAT_NEEDS_AUDIO)), FORMAT_NEEDS_AUDIO)
        self.assertEqual(friendly_error(RuntimeError(NO_AUDIO_TRACK)), NO_AUDIO_TRACK)
        self.assertEqual(
            friendly_error(
                RuntimeError("You have requested merging of multiple formats but ffmpeg is not installed")
            ),
            FFMPEG_REQUIRED,
        )


if __name__ == "__main__":
    unittest.main()
