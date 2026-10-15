# -*- coding: utf-8 -*-
"""yt-dlp 封装：探测格式、按分辨率/容器下载，带进度钩子。"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from core.errors import (
    YOUTUBE_COOKIES_NEEDED,
    friendly_error,
    is_youtube_client_retryable,
    youtube_retry_reason,
)


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
    cookiefile: str = ""  # Netscape cookies.txt；空则不用
    cookiesfrombrowser: str = ""  # "" | chrome | edge | firefox


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


_BROWSER_ALIASES = {
    "": "",
    "none": "",
    "off": "",
    "不使用": "",
    "无": "",
    "chrome": "chrome",
    "edge": "edge",
    "msedge": "edge",
    "firefox": "firefox",
}


def normalize_browser(name: str) -> str:
    """空 / 不使用 → ''；chrome、edge、firefox 原样。其它值报错。"""
    key = (name or "").strip().lower()
    if key in _BROWSER_ALIASES:
        return _BROWSER_ALIASES[key]
    raise RuntimeError(
        f"不支持的浏览器 Cookie：{name}（可选 chrome / edge / firefox，或留空）"
    )


def is_youtube_url(url: str) -> bool:
    """youtube.com（含子域）与 youtu.be。其它站点不改 player client。"""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if host in ("youtu.be", "youtube.com", "youtube-nocookie.com"):
        return True
    return host.endswith(".youtube.com") or host.endswith(".youtube-nocookie.com")


# yt-dlp 2026.10 登录前默认客户端（不要求 PO Token）。
# tv_embedded 已在 2026.01 删除；android / ios / mweb 的 https 格式没有 PO Token 会 403。
# 顺序与 yt-dlp _DEFAULT_CLIENTS 一致：tv_samsung → visionos → web。
# visionos 不需要 JS 运行时；Windows 包里没有 Deno 时，它是最稳的无 Cookie 客户端。
_YOUTUBE_RECOMMENDED_CLIENTS = ("tv_samsung", "visionos", "web")
_YOUTUBE_FALLBACK_CLIENTS = (
    ("visionos",),
    ("tv_samsung",),
    ("web_embedded",),
    ("tv_downgraded",),
)

_supported_clients_cache: Optional[set[str]] = None


def _supported_player_clients() -> set[str]:
    """当前安装的 yt-dlp 实际认识的 player client。读不到时返回空集，调用方改用默认。"""
    global _supported_clients_cache
    if _supported_clients_cache is not None:
        return _supported_clients_cache
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS

        _supported_clients_cache = {
            str(name) for name in INNERTUBE_CLIENTS if not str(name).startswith("_")
        }
    except Exception:
        _supported_clients_cache = set()
    return _supported_clients_cache


@dataclass(frozen=True)
class _ExtractAttempt:
    cookiefile: str
    cookiesfrombrowser: str
    player_clients: tuple[str, ...]
    label: str


def build_extract_attempts(
    url: str,
    cookiefile: str = "",
    cookiesfrombrowser: str = "",
) -> list[_ExtractAttempt]:
    """
    非 YouTube：一次，沿用用户填写的 Cookie（可为空）。
    YouTube：若用户填了 Cookie，先用 yt-dlp 的登录客户端；
    然后在不带 Cookie 的情况下按推荐客户端重试。player_clients 为空表示不覆盖默认值。
    """
    if not is_youtube_url(url):
        return [_ExtractAttempt(cookiefile or "", cookiesfrombrowser or "", (), "")]

    path = (cookiefile or "").strip()
    browser = cookiesfrombrowser or ""
    has_cookies = bool(path)
    if browser.strip():
        try:
            # 「不使用」会归一成空，不算用户提供了 Cookie。
            has_cookies = bool(path or normalize_browser(browser))
        except RuntimeError:
            # 非法浏览器名留给这次 Cookie 尝试报错，不要当成没填。
            has_cookies = True

    attempts: list[_ExtractAttempt] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()

    def add(cookie: str, browser_name: str, clients: tuple[str, ...], label: str) -> None:
        key = (cookie, browser_name, clients)
        if key in seen:
            return
        seen.add(key)
        attempts.append(_ExtractAttempt(cookie, browser_name, clients, label))

    if has_cookies:
        add(path, browser, (), "已填写的 Cookie")

    known = _supported_player_clients()
    groups = (_YOUTUBE_RECOMMENDED_CLIENTS, *_YOUTUBE_FALLBACK_CLIENTS)
    for group in groups:
        clients = tuple(c for c in group if c in known) if known else group
        if not clients:
            continue
        add("", "", clients, "+".join(clients))

    if not any(item.player_clients for item in attempts):
        add("", "", (), "默认")
    return attempts


def _attempt_log(attempt: _ExtractAttempt, index: int, total: int) -> str:
    if attempt.player_clients:
        client = "客户端 " + ",".join(attempt.player_clients)
    else:
        client = "yt-dlp 默认客户端"
    cookies = "使用 Cookie" if (attempt.cookiefile or attempt.cookiesfrombrowser) else "不使用 Cookie"
    return f"YouTube 尝试 {index}/{total}：{client}，{cookies}"


def apply_cookie_options(
    ydl_opts: dict[str, Any],
    cookiefile: str = "",
    cookiesfrombrowser: str = "",
) -> dict[str, Any]:
    """
    把可选 Cookie 写进 yt-dlp 参数。
    未设置时不添加 cookiefile / cookiesfrombrowser，行为与原来一致。
    """
    browser = normalize_browser(cookiesfrombrowser)
    path = (cookiefile or "").strip().strip('"').strip("'")
    if path:
        if not Path(path).is_file():
            raise RuntimeError(f"Cookie 文件不存在：{path}")
        ydl_opts["cookiefile"] = path
    if browser:
        # (browser, profile, keyring, container)，与 yt-dlp parse_options 一致
        ydl_opts["cookiesfrombrowser"] = (browser, None, None, None)
    return ydl_opts


def _reraise_extract_error(exc: BaseException) -> None:
    text = str(exc)
    if isinstance(exc, RuntimeError) and (
        text.startswith("Cookie 文件不存在")
        or text.startswith("不支持的浏览器")
        or YOUTUBE_COOKIES_NEEDED in text
    ):
        raise exc
    if "已取消" in text:
        raise exc
    raise RuntimeError(friendly_error(exc)) from exc


def _opts_for_attempt(base: dict[str, Any], attempt: _ExtractAttempt) -> dict[str, Any]:
    opts = dict(base)
    opts.pop("cookiefile", None)
    opts.pop("cookiesfrombrowser", None)
    opts.pop("extractor_args", None)
    apply_cookie_options(opts, attempt.cookiefile, attempt.cookiesfrombrowser)
    if attempt.player_clients:
        opts["extractor_args"] = {
            "youtube": {"player_client": list(attempt.player_clients)}
        }
    return opts


def _extract_with_retries(
    url: str,
    base_opts: dict[str, Any],
    attempts: list[_ExtractAttempt],
    log: ProgressCallback,
    *,
    download: bool,
    cancel_event: Optional[threading.Event] = None,
    on_success: Callable[[Any, dict[str, Any]], Any],
) -> Any:
    """按尝试列表调用 yt-dlp。YouTube 上可重试的失败换下一个客户端。"""
    yt_dlp = _import_yt_dlp()
    youtube = is_youtube_url(url)
    total = len(attempts)
    last_retryable: Optional[BaseException] = None

    for index, attempt in enumerate(attempts, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("已取消")
        if youtube and total > 1:
            log(_attempt_log(attempt, index, total))
        try:
            opts = _opts_for_attempt(base_opts, attempt)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
                if info is None:
                    raise RuntimeError("无法获取视频信息")
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("已取消")
                return on_success(ydl, info)
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise
            if "已取消" in str(exc):
                raise
            if youtube and is_youtube_client_retryable(exc) and index < total:
                last_retryable = exc
                log(f"本次未成功（{youtube_retry_reason(exc)}），正在改试下一个客户端…")
                continue
            if youtube and is_youtube_client_retryable(exc):
                raise RuntimeError(YOUTUBE_COOKIES_NEEDED) from exc
            _reraise_extract_error(exc)

    raise RuntimeError(YOUTUBE_COOKIES_NEEDED) from last_retryable


def _probe_from_info(info: dict[str, Any]) -> ProbeResult:
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
    return ProbeResult(title=title, formats=formats, raw=info)


def list_formats(
    url: str,
    log: Optional[ProgressCallback] = None,
    *,
    cookiefile: str = "",
    cookiesfrombrowser: str = "",
) -> ProbeResult:
    """探测可用格式（不下载）。YouTube 会在无 Cookie 客户端之间自动重试。"""
    log = log or (lambda _m: None)
    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        # deno 是 yt-dlp 默认；再打开 node，本机有 Node 时 web/tv_samsung 才能解签名。
        "js_runtimes": {"deno": {}, "node": {}},
    }
    log(f"正在探测格式：{url}")

    def on_success(_ydl: Any, info: dict[str, Any]) -> ProbeResult:
        result = _probe_from_info(info)
        log(f"探测完成：{result.title}（{len(result.formats)} 个格式）")
        return result

    return _extract_with_retries(
        url,
        ydl_opts,
        build_extract_attempts(url, cookiefile, cookiesfrombrowser),
        log,
        download=False,
        on_success=on_success,
    )


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
        self._last_ui = 0.0
        self._last_pct = -1.0

    def __call__(self, d: dict[str, Any]) -> None:
        if self.cancel_event and self.cancel_event.is_set():
            raise RuntimeError("已取消")

        status = d.get("status")
        filename = d.get("filename") or ""
        if filename:
            self.last_filepath = filename

        if status == "downloading":
            pct = _parse_percent(d)
            now = time.monotonic()
            # yt-dlp 进度可能一秒几十次。每条都 after() 到主线程会把窗口打成「未响应」。
            if (
                self._last_pct >= 0
                and now - self._last_ui < 0.4
                and abs(pct - self._last_pct) < 1.0
                and pct < 100.0
            ):
                return
            self._last_ui = now
            self._last_pct = pct
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


def ensure_output_dir(
    path: str,
    timeout: float = 20.0,
    cancel_event: Optional[threading.Event] = None,
) -> Path:
    """
    在调用线程之外创建保存目录。
    必须由下载工作线程调用，不能在 Tk 主线程调用：未连接的盘符（如 F:）
    上 mkdir 可能阻塞几十秒，主线程一阻塞窗口就会变成「未响应」。
    """
    path = (path or "").strip()
    if not path:
        raise RuntimeError("请选择保存路径。")

    error: list[BaseException] = []
    done = threading.Event()

    def work() -> None:
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            error.append(e)
        finally:
            done.set()

    threading.Thread(target=work, name="mkdir-out", daemon=True).start()
    deadline = time.monotonic() + max(0.1, timeout)
    while not done.wait(0.2):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("已取消")
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"保存目录无响应：{path}。请确认磁盘已连接（例如 F: 盘）。"
                "任务已失败，窗口不会因此卡住。"
            )
    if error:
        detail = getattr(error[0], "strerror", None) or error[0]
        raise RuntimeError(f"无法创建保存目录：{path}（{detail}）") from error[0]
    return Path(path)


def download(
    opts: DownloadOptions,
    log: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    on_progress: Optional[RichProgressCallback] = None,
) -> DownloadResult:
    """
    执行下载。返回 DownloadResult（含最终文件路径，若可解析）。
    保存目录在本函数（工作线程）里创建，失败只影响这一条任务。
    """
    log = log or (lambda _m: None)
    log(f"准备保存目录：{opts.output_dir}")
    out_dir = ensure_output_dir(opts.output_dir, cancel_event=cancel_event)

    fmt = build_format_selector(opts)

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
            "js_runtimes": {"deno": {}, "node": {}},
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
            "js_runtimes": {"deno": {}, "node": {}},
        }

    log(f"格式选择器：{fmt}")
    log(f"保存目录：{opts.output_dir}")
    cookie_bits: list[str] = []
    if opts.cookiesfrombrowser:
        try:
            browser_name = normalize_browser(opts.cookiesfrombrowser)
        except RuntimeError:
            browser_name = opts.cookiesfrombrowser
        if browser_name:
            cookie_bits.append(f"浏览器 {browser_name}")
    if opts.cookiefile:
        cookie_bits.append("Cookie 文件")
    if cookie_bits:
        note = "已启用 " + "、".join(cookie_bits)
        if is_youtube_url(opts.url):
            note += "（读取失败时会改为无 Cookie 重试）"
        log(note)

    if cancel_event and cancel_event.is_set():
        raise RuntimeError("已取消")

    def on_success(ydl: Any, info: dict[str, Any]) -> DownloadResult:
        filepath: Optional[str] = None
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
                    candidate = p.with_suffix(f".{merge_format}")
                    if candidate.is_file():
                        filepath = str(candidate)
        except Exception:
            filepath = hook.last_filepath

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

    return _extract_with_retries(
        opts.url,
        ydl_opts,
        build_extract_attempts(opts.url, opts.cookiefile, opts.cookiesfrombrowser),
        log,
        download=True,
        cancel_event=cancel_event,
        on_success=on_success,
    )
