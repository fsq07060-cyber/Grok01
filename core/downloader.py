# -*- coding: utf-8 -*-
"""yt-dlp 封装：探测格式、按分辨率/容器下载，带进度钩子。"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


ProgressCallback = Callable[[str], None]
# percent 0–100, message, filepath_hint (optional during download)
RichProgressCallback = Callable[[float, str, Optional[str]], None]


@dataclass
class DownloadOptions:
    url: str
    output_dir: str
    resolution: str = "1080"  # "best" | "2160" | "1440" | "1080" | "720" | "480" | "audio"
    video_format: str = "mp4"  # mp4 / webm / mkv / best
    audio_format: str = "m4a"  # m4a / mp3 / opus / best
    audio_only: bool = False


@dataclass
class FormatInfo:
    format_id: str
    ext: str
    resolution: str
    note: str
    vcodec: str = ""
    acodec: str = ""
    filesize: Optional[int] = None


@dataclass
class ProbeResult:
    title: str
    formats: list[FormatInfo] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DownloadResult:
    title: str
    filepath: Optional[str]
    output_dir: str


def _import_yt_dlp():
    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError("未安装 yt-dlp。请执行：pip install yt-dlp") from e
    return yt_dlp


def _resolution_height(res: str) -> Optional[int]:
    if res in ("best", "audio"):
        return None
    m = re.match(r"^(\d+)", res)
    return int(m.group(1)) if m else None


def build_format_selector(opts: DownloadOptions) -> str:
    """根据 UI 选项构造 yt-dlp format 字符串。"""
    if opts.audio_only or opts.resolution == "audio":
        af = opts.audio_format
        if af == "best":
            return "bestaudio/best"
        return f"ba[ext={af}]/bestaudio/best"

    height = _resolution_height(opts.resolution)
    vf = opts.video_format
    af = opts.audio_format

    height_filter = f"[height<=?{height}]" if height else ""

    if vf == "best":
        video = f"bv*{height_filter}/b{height_filter}"
    else:
        video = f"bv*{height_filter}[ext={vf}]/bv*{height_filter}/b{height_filter}"

    if af == "best":
        audio = "ba"
    else:
        audio = f"ba[ext={af}]/ba"

    return f"{video}+{audio}/b{height_filter}/best"


def list_formats(url: str, log: Optional[ProgressCallback] = None) -> ProbeResult:
    """探测可用格式（不下载）。"""
    yt_dlp = _import_yt_dlp()
    log = log or (lambda _m: None)

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }

    log(f"正在探测格式：{url}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        raise RuntimeError("无法获取视频信息")

    if "entries" in info and info["entries"]:
        info = next((e for e in info["entries"] if e), info)

    title = info.get("title") or "未知标题"
    formats: list[FormatInfo] = []
    for f in info.get("formats") or []:
        height = f.get("height")
        width = f.get("width")
        res = f"{width}x{height}" if height else (f.get("resolution") or "audio")
        formats.append(
            FormatInfo(
                format_id=str(f.get("format_id", "")),
                ext=str(f.get("ext") or ""),
                resolution=str(res),
                note=str(f.get("format_note") or f.get("format") or ""),
                vcodec=str(f.get("vcodec") or ""),
                acodec=str(f.get("acodec") or ""),
                filesize=f.get("filesize") or f.get("filesize_approx"),
            )
        )

    log(f"探测完成：{title}（{len(formats)} 个格式）")
    return ProbeResult(title=title, formats=formats, raw=info)


def _parse_percent(d: dict[str, Any]) -> float:
    """从 yt-dlp hook dict 提取 0–100 进度。"""
    # 优先数值字段
    for key in ("downloaded_bytes",):
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes")
        if total and downloaded is not None and total > 0:
            return max(0.0, min(100.0, 100.0 * float(downloaded) / float(total)))
    pct_str = (d.get("_percent_str") or "").strip().rstrip("%")
    try:
        return max(0.0, min(100.0, float(pct_str)))
    except (TypeError, ValueError):
        return 0.0


class _ProgressHook:
    def __init__(
        self,
        log: ProgressCallback,
        on_progress: Optional[RichProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        self.log = log
        self.on_progress = on_progress
        self.cancel_event = cancel_event
        self.last_filepath: Optional[str] = None

    def __call__(self, d: dict[str, Any]) -> None:
        if self.cancel_event and self.cancel_event.is_set():
            raise RuntimeError("已取消")

        status = d.get("status")
        filename = d.get("filename") or ""
        if filename:
            self.last_filepath = filename

        if status == "downloading":
            pct = _parse_percent(d)
            speed = (d.get("_speed_str") or "").strip()
            eta = (d.get("_eta_str") or "").strip()
            msg = f"下载中 {pct:.1f}%  速度 {speed}  剩余 {eta}"
            self.log(msg)
            if self.on_progress:
                self.on_progress(pct, msg, filename or None)
        elif status == "finished":
            self.last_filepath = filename or self.last_filepath
            msg = f"下载完成，正在后处理… {filename}"
            self.log(msg)
            if self.on_progress:
                self.on_progress(100.0, msg, self.last_filepath)
        elif status == "error":
            self.log("下载出错")
            if self.on_progress:
                self.on_progress(0.0, "下载出错", None)


def download(
    opts: DownloadOptions,
    log: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    on_progress: Optional[RichProgressCallback] = None,
) -> DownloadResult:
    """
    执行下载。返回 DownloadResult（含最终文件路径，若可解析）。
    """
    yt_dlp = _import_yt_dlp()
    log = log or (lambda _m: None)
    fmt = build_format_selector(opts)

    out_dir = Path(opts.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(out_dir / "%(title)s [%(id)s].%(ext)s")

    merge_format = opts.video_format if opts.video_format in ("mp4", "webm", "mkv") else "mp4"
    hook = _ProgressHook(log, on_progress=on_progress, cancel_event=cancel_event)

    if opts.audio_only or opts.resolution == "audio":
        postprocessors = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": opts.audio_format if opts.audio_format != "best" else "m4a",
                "preferredquality": "192",
            }
        ]
        ydl_opts: dict[str, Any] = {
            "format": fmt,
            "outtmpl": outtmpl,
            "noplaylist": True,
            "progress_hooks": [hook],
            "postprocessors": postprocessors,
            "quiet": True,
            "no_warnings": False,
        }
    else:
        ydl_opts = {
            "format": fmt,
            "outtmpl": outtmpl,
            "noplaylist": True,
            "merge_output_format": merge_format,
            "progress_hooks": [hook],
            "quiet": True,
            "no_warnings": False,
        }

    log(f"格式选择器：{fmt}")
    log(f"保存目录：{opts.output_dir}")

    if cancel_event and cancel_event.is_set():
        raise RuntimeError("已取消")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(opts.url, download=True)
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("已取消")

        filepath: Optional[str] = None
        if info:
            # playlist 取第一条
            if "entries" in info and info["entries"]:
                info = next((e for e in info["entries"] if e), info)
            try:
                filepath = ydl.prepare_filename(info)
                # 音频提取后扩展名可能变化
                if opts.audio_only or opts.resolution == "audio":
                    pref = opts.audio_format if opts.audio_format != "best" else "m4a"
                    filepath = str(Path(filepath).with_suffix(f".{pref}"))
                # merge_output_format 也可能改扩展名
                elif opts.video_format in ("mp4", "webm", "mkv"):
                    p = Path(filepath)
                    if p.suffix.lower().lstrip(".") != opts.video_format:
                        # 合并后常见为 merge_format
                        candidate = p.with_suffix(f".{merge_format}")
                        if candidate.is_file():
                            filepath = str(candidate)
            except Exception:
                filepath = hook.last_filepath

            # 若 prepare 的路径不存在，尝试 hook 记录或请求文件列表
            if filepath and not Path(filepath).is_file():
                if hook.last_filepath and Path(hook.last_filepath).is_file():
                    filepath = hook.last_filepath
                else:
                    req = info.get("requested_downloads") or []
                    for rd in req:
                        fp = rd.get("filepath")
                        if fp and Path(fp).is_file():
                            filepath = fp
                            break

        title = (info or {}).get("title", "") or ""
        log(f"全部完成：{title}")
        if on_progress:
            on_progress(100.0, f"全部完成：{title}", filepath)
        return DownloadResult(title=title, filepath=filepath, output_dir=opts.output_dir)
