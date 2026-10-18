# -*- coding: utf-8 -*-
"""yt-dlp 封装：探测格式、按分辨率/容器下载，带进度钩子。"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from core.errors import (
    FORMAT_NEEDS_AUDIO,
    IMPERSONATE_DEPENDENCY_MISSING,
    IMPERSONATE_STILL_BLOCKED,
    NO_AUDIO_TRACK,
    YOUTUBE_COOKIES_NEEDED,
    friendly_error,
    is_format_unavailable,
    is_impersonate_dependency_missing,
    is_impersonate_retryable,
    is_youtube_client_retryable,
    youtube_retry_reason,
)
from core.ffmpeg_path import ensure_bundled_ffmpeg, resolve_tool


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
    video_id: str = ""


def _import_yt_dlp():
    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError("未安装 yt-dlp。请执行：pip install yt-dlp") from e
    return yt_dlp


# 模块级导入让 PyInstaller 分析能跟上 curl_cffi._wrapper。
# Windows 轮子里的 libcurl-impersonate 在 curl_cffi.libs，由 build_windows.spec 单独打进包。
try:
    import curl_cffi as _curl_cffi  # noqa: F401
except Exception:
    _curl_cffi = None


def _impersonate_target():
    """
    --impersonate chrome。
    版本留空时，yt-dlp 会选当前 curl_cffi 里优先的桌面 Chrome。
    见 https://github.com/yt-dlp/yt-dlp#impersonation
    """
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except Exception as exc:
        raise RuntimeError(IMPERSONATE_DEPENDENCY_MISSING) from exc
    return ImpersonateTarget(client="chrome")


def _resolution_height(res: str) -> Optional[int]:
    if res in ("best", "audio"):
        return None
    m = re.match(r"^(\d+)", res)
    return int(m.group(1)) if m else None


_DIM_RE = re.compile(r"(?P<w>\d{3,4})x(?P<h>\d{3,4})", re.IGNORECASE)
_HEIGHT_P_RE = re.compile(r"(?<!\d)(?P<h>\d{3,4})p(?!\d)", re.IGNORECASE)


def _plausible_height(value: int) -> bool:
    return 144 <= value <= 4320


def _height_hint(fmt: dict[str, Any]) -> Optional[int]:
    """从路径或格式说明里读 1080p / 1920x1080。不看 query，避免把签名里的数字当成清晰度。"""
    url = str(fmt.get("url") or "")
    path = urlparse(url).path if url else ""
    texts = [
        path,
        str(fmt.get("format_id") or ""),
        str(fmt.get("format") or ""),
        str(fmt.get("format_note") or ""),
        str(fmt.get("resolution") or ""),
    ]
    found: Optional[int] = None
    for text in texts:
        for match in _DIM_RE.finditer(text):
            height = int(match.group("h"))
            if _plausible_height(height):
                found = height
        for match in _HEIGHT_P_RE.finditer(text):
            height = int(match.group("h"))
            if _plausible_height(height):
                found = height
    return found


def fill_missing_stream_hints(info: dict[str, Any]) -> None:
    """
    通用提取经常只给出 mp4 地址，不写 height / 编码。
    补上路径里的清晰度，后面的 height 过滤和排序才能选对 1080p 而不是另一条。
    不臆造 acodec：没写编码的合体流交给格式串的 `?`，纯画面仍是字面 none。
    """
    if not isinstance(info, dict):
        return
    formats = info.get("formats")
    targets: list[dict[str, Any]] = []
    if isinstance(formats, list):
        targets.extend(item for item in formats if isinstance(item, dict))
    elif info.get("url"):
        targets.append(info)
    for fmt in targets:
        if fmt.get("height"):
            continue
        height = _height_hint(fmt)
        if height:
            fmt["height"] = height


def _merge_container(video_format: str) -> str:
    if video_format in ("mp4", "webm", "mkv"):
        return video_format
    return "mp4"


def _is_audio_only(opts: DownloadOptions) -> bool:
    return bool(opts.audio_only or opts.resolution == "audio")


def _height_filter(resolution: str) -> str:
    height = _resolution_height(resolution)
    return f"[height<=?{height}]" if height else ""


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _audio_terms(container: str, audio_format: str) -> list[str]:
    """音轨选择，从左到右。mp4 先放 aac/m4a，方便直接封装而不是只下画面。"""
    requested: list[str] = []
    if audio_format == "m4a":
        requested += ["ba[ext=m4a]", "ba[acodec^=mp4a]"]
    elif audio_format == "mp3":
        requested += ["ba[ext=mp3]", "ba[acodec^=mp3]"]
    elif audio_format == "opus":
        requested += ["ba[acodec^=opus]", "ba[ext=webm]"]
    elif audio_format not in ("best", ""):
        requested.append(f"ba[ext={audio_format}]")

    compatible: list[str] = []
    if container == "mp4":
        compatible += ["ba[ext=m4a]", "ba[acodec^=mp4a]"]
    elif container == "webm":
        compatible += ["ba[acodec^=opus]", "ba[ext=webm]"]

    # mp4 必须能 remux：aac/m4a 排在用户指定的其它编码前面。
    if container == "mp4":
        terms = compatible + requested + ["ba"]
    else:
        terms = requested + compatible + ["ba"]
    return _dedupe(terms)


def _video_terms(container: str, video_format: str, height_filter: str) -> list[str]:
    terms: list[str] = []
    ext = video_format if video_format in ("mp4", "webm", "mkv") else ""
    if ext:
        terms.append(f"bv*{height_filter}[ext={ext}]")
    elif container == "mp4":
        terms.append(f"bv*{height_filter}[ext=mp4]")
    elif container == "webm":
        terms.append(f"bv*{height_filter}[ext=webm]")
    terms.append(f"bv*{height_filter}")
    if height_filter:
        terms.append("bv*")
    return _dedupe(terms)


def _muxed_fallbacks(height_filter: str) -> list[str]:
    """
    没有独立音轨可合并时，接受一条已经含画面的单文件。
    `?` 让「提取器没写 vcodec/acodec」的合体流通过（通用提取常见）。
    字面量 acodec=none / vcodec=none 的纯画面或纯声音仍然排除，避免无声 AV1 抢先。
    """
    terms: list[str] = []
    if height_filter:
        terms.append(f"b{height_filter}[vcodec!=?none][acodec!=?none]")
    terms.append("b[vcodec!=?none][acodec!=?none]")
    return terms


def _with_audio_fallbacks(videos: list[str], audios: list[str], height_filter: str) -> str:
    """每个 `/` 分支都是「画面+声音」或自带音轨的单文件。`+` 比 `/` 更紧，不能把 bv* 单独放在斜线左边。"""
    parts = [f"{video}+{audio}" for video in videos for audio in audios]
    parts.extend(_muxed_fallbacks(height_filter))
    return "/".join(parts)


def build_format_selector(opts: DownloadOptions) -> str:
    """根据 UI 选项构造 yt-dlp format 字符串。普通下载永远合并音视频。"""
    if _is_audio_only(opts):
        af = opts.audio_format
        if af == "best":
            return "bestaudio/best"
        return f"ba[ext={af}]/bestaudio/best"

    container = _merge_container(opts.video_format)
    height_filter = _height_filter(opts.resolution)
    return _with_audio_fallbacks(
        _video_terms(container, opts.video_format, height_filter),
        _audio_terms(container, opts.audio_format),
        height_filter,
    )


def build_audio_retry_selector(opts: DownloadOptions) -> str:
    """第一次下到无声文件时用的更宽格式：不限容器。纯画面仍不选，未声明编码的合体流可以。"""
    container = _merge_container(opts.video_format)
    height_filter = _height_filter(opts.resolution)
    videos = [f"bv*{height_filter}"] if height_filter else []
    videos.append("bv*")
    return _with_audio_fallbacks(
        _dedupe(videos),
        _audio_terms(container, opts.audio_format),
        height_filter,
    )


def format_sort_for(opts: DownloadOptions) -> Optional[list[str]]:
    """同一清晰度下，mp4 优先 h264+aac，这样 ffmpeg 可以复制流而不是丢掉声音。"""
    if _is_audio_only(opts):
        return None
    container = _merge_container(opts.video_format)
    if container == "mp4":
        return ["res", "fps", "vcodec:h264", "acodec:aac"]
    if container == "webm":
        return ["res", "fps", "vcodec:vp9", "acodec:opus"]
    return None


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
    impersonate: bool = False


def build_extract_attempts(
    url: str,
    cookiefile: str = "",
    cookiesfrombrowser: str = "",
) -> list[_ExtractAttempt]:
    """
    非 YouTube：先按用户填写的 Cookie（默认空，不读浏览器）请求一次。
    若遇到 403 / Cloudflare，同一套 Cookie 再开 --impersonate 与 generic:impersonate。
    不会自动改成 chrome 的 cookiesfrombrowser。

    YouTube：若用户填了 Cookie，先用 yt-dlp 的登录客户端；
    然后在不带 Cookie 的情况下按推荐客户端重试。
    客户端都因 403 / Cloudflare 失败时，最后再试一次浏览器指纹，仍然不自动读 Cookie。
    player_clients 为空表示不覆盖默认值。
    """
    path = (cookiefile or "").strip()
    browser = cookiesfrombrowser or ""
    if not is_youtube_url(url):
        plain = _ExtractAttempt(path, browser, (), "", False)
        fingerprinted = _ExtractAttempt(path, browser, (), "浏览器指纹", True)
        return [plain, fingerprinted]

    has_cookies = bool(path)
    if browser.strip():
        try:
            # 「不使用」会归一成空，不算用户提供了 Cookie。
            has_cookies = bool(path or normalize_browser(browser))
        except RuntimeError:
            # 非法浏览器名留给这次 Cookie 尝试报错，不要当成没填。
            has_cookies = True

    attempts: list[_ExtractAttempt] = []
    seen: set[tuple[str, str, tuple[str, ...], bool]] = set()

    def add(
        cookie: str,
        browser_name: str,
        clients: tuple[str, ...],
        label: str,
        *,
        impersonate: bool = False,
    ) -> None:
        key = (cookie, browser_name, clients, impersonate)
        if key in seen:
            return
        seen.add(key)
        attempts.append(_ExtractAttempt(cookie, browser_name, clients, label, impersonate))

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
    # 403 / Cloudflare 才走到这一次。默认仍不带 Cookie。
    add("", "", (), "浏览器指纹", impersonate=True)
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
        or IMPERSONATE_STILL_BLOCKED in text
        or IMPERSONATE_DEPENDENCY_MISSING in text
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
    opts.pop("impersonate", None)
    apply_cookie_options(opts, attempt.cookiefile, attempt.cookiesfrombrowser)
    extractor_args: dict[str, Any] = {}
    if attempt.player_clients:
        extractor_args["youtube"] = {"player_client": list(attempt.player_clients)}
    if attempt.impersonate:
        # 全局 --impersonate 覆盖后续媒体请求；generic:impersonate 覆盖网页这一下。
        # Cloudflare 经常网页过了、视频地址仍 403，所以两个都要。
        opts["impersonate"] = _impersonate_target()
        extractor_args["generic"] = {"impersonate": ["chrome"]}
    if extractor_args:
        opts["extractor_args"] = extractor_args
    return opts


def _attempt_uses_cookies(attempt: _ExtractAttempt) -> bool:
    if (attempt.cookiefile or "").strip():
        return True
    try:
        return bool(normalize_browser(attempt.cookiesfrombrowser))
    except RuntimeError:
        return bool((attempt.cookiesfrombrowser or "").strip())


def _impersonate_log(attempt: _ExtractAttempt) -> str:
    cookies = "使用已填写的 Cookie" if _attempt_uses_cookies(attempt) else "不使用 Cookie"
    return f"遇到 HTTP 403 / Cloudflare，正在用浏览器指纹重试（{cookies}）…"


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
    client_attempts = [item for item in attempts if not item.impersonate]
    client_total = len(client_attempts)
    last_retryable: Optional[BaseException] = None

    for index, attempt in enumerate(attempts, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("已取消")
        if attempt.impersonate:
            log(_impersonate_log(attempt))
        elif youtube and client_total > 1:
            log(_attempt_log(attempt, index, client_total))
        try:
            opts = _opts_for_attempt(base_opts, attempt)

            class _HintYDL(yt_dlp.YoutubeDL):
                def process_video_result(self, info_dict, download=True):  # type: ignore[override]
                    fill_missing_stream_hints(info_dict)
                    return super().process_video_result(info_dict, download=download)

            with _HintYDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
                if info is None:
                    raise RuntimeError("无法获取视频信息")
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("已取消")
                return on_success(ydl, info)
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise
            text = str(exc)
            if "已取消" in text:
                raise
            if (
                IMPERSONATE_DEPENDENCY_MISSING in text
                or IMPERSONATE_STILL_BLOCKED in text
                or YOUTUBE_COOKIES_NEEDED in text
            ):
                raise
            nxt = attempts[index] if index < len(attempts) else None
            if is_format_unavailable(exc):
                if youtube and nxt is not None and not nxt.impersonate:
                    log("这个客户端没有可合并的音视频格式，正在改试下一个客户端…")
                    continue
                raise RuntimeError(FORMAT_NEEDS_AUDIO) from exc
            if youtube and is_youtube_client_retryable(exc) and nxt is not None:
                if nxt.impersonate and not is_impersonate_retryable(exc):
                    raise RuntimeError(YOUTUBE_COOKIES_NEEDED) from exc
                last_retryable = exc
                if not nxt.impersonate:
                    log(f"本次未成功（{youtube_retry_reason(exc)}），正在改试下一个客户端…")
                continue
            if youtube and is_youtube_client_retryable(exc):
                raise RuntimeError(YOUTUBE_COOKIES_NEEDED) from exc
            if nxt is not None and nxt.impersonate and is_impersonate_retryable(exc):
                continue
            if attempt.impersonate and is_impersonate_dependency_missing(exc):
                raise RuntimeError(IMPERSONATE_DEPENDENCY_MISSING) from exc
            if attempt.impersonate and is_impersonate_retryable(exc):
                raise RuntimeError(IMPERSONATE_STILL_BLOCKED) from exc
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


_MEDIA_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".webm",
    ".m4a",
    ".mp3",
    ".opus",
    ".ogg",
    ".aac",
    ".flv",
    ".mov",
    ".ts",
    ".m4v",
}
_INTERMEDIATE_RE = re.compile(r"\.f\d+\.")
# codec_type 不是 audio、但编码名或标签能认出来的声音。
_AUDIO_NAME_RE = re.compile(
    r"aac|mp3|mp2|mp4a|opus|vorbis|flac|alac|ac-?3|eac3|dts|truehd|amr|speex|"
    r"wmav|wmapro|cook|sipr|qcelp|gsm|nelly|tta|wavpack|\bape\b|mlp|atrac|"
    r"adpcm|pcm_|pcm\b",
    re.IGNORECASE,
)
_CLEAR_NON_AUDIO = {"video", "subtitle", "attachment"}


def probe_streams_have_audio(streams: list[dict[str, Any]]) -> Optional[bool]:
    """
    True：有音轨（含编码名不标准、但能认出是声音的流）。
    False：只有画面等明确的非音轨，是真·纯画面。
    None：没探到流，或还有 data/unknown，不能据此删文件。
    """
    if not streams:
        return None
    saw_video = False
    ambiguous = False
    for stream in streams:
        if _stream_is_audio(stream):
            return True
        kind = str(stream.get("codec_type") or "").strip().lower()
        if kind == "video":
            saw_video = True
        elif kind not in _CLEAR_NON_AUDIO:
            ambiguous = True
    if ambiguous or not saw_video:
        return None
    return False


def _stream_is_audio(stream: dict[str, Any]) -> bool:
    kind = str(stream.get("codec_type") or "").strip().lower()
    if kind == "audio":
        return True
    label = " ".join(
        str(stream.get(key) or "")
        for key in ("codec_name", "codec_tag_string", "codec_long_name")
    )
    if _AUDIO_NAME_RE.search(label):
        return True
    if kind in _CLEAR_NON_AUDIO:
        return False
    if stream.get("channels") or stream.get("channel_layout") or stream.get("sample_rate"):
        return True
    return False


def file_has_audio(path: Optional[str]) -> Optional[bool]:
    """
    ffprobe 是否看到音轨。
    True/False 表示已经确认；None 表示文件不存在、ffprobe 没跑成，或流类型含糊，不能据此判失败。
    只有确认是纯画面才返回 False。
    """
    if not path:
        return None
    file_path = Path(path)
    if not file_path.is_file():
        return None
    probe = resolve_tool("ffprobe")
    if not probe:
        return None
    try:
        proc = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-probesize",
                "32000000",
                "-analyzeduration",
                "10000000",
                "-show_entries",
                "stream=codec_type,codec_name,codec_tag_string,codec_long_name,channels,channel_layout,sample_rate",
                "-of",
                "json",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    payload = _parse_probe_json(proc.stdout)
    if payload is None:
        return None
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return None
    return probe_streams_have_audio([item for item in streams if isinstance(item, dict)])


def _parse_probe_json(text: str) -> Optional[dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _candidate_files(out_dir: Path, filepath: Optional[str], video_id: str) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path.suffix.lower() not in _MEDIA_SUFFIXES:
            return
        if not path.is_file():
            return
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            return
        seen.add(key)
        found.append(path)

    if filepath:
        primary = Path(filepath)
        add(primary)
        merged_name = _INTERMEDIATE_RE.sub(".", primary.name)
        if merged_name != primary.name:
            add(primary.with_name(merged_name))
    if video_id and out_dir.is_dir():
        needle = f"[{video_id}]"
        try:
            children = list(out_dir.iterdir())
        except OSError:
            children = []
        for child in children:
            if needle in child.name:
                add(child)
    return found


def _file_rank(path: Path, container: str) -> tuple[int, int, int]:
    intermediate = 1 if _INTERMEDIATE_RE.search(path.name) else 0
    ext_penalty = 0 if path.suffix.lower().lstrip(".") == container else 1
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return (intermediate, ext_penalty, -size)


def select_downloaded_file(
    out_dir: Path,
    filepath: Optional[str],
    video_id: str,
    container: str,
) -> tuple[Optional[Path], Optional[bool]]:
    """在合并结果和临时视频里挑一个。只要有一条带音轨，就用那一条。"""
    candidates = _candidate_files(out_dir, filepath, video_id)
    if not candidates:
        return None, None
    probed = [(path, file_has_audio(str(path))) for path in candidates]
    audible = [path for path, has in probed if has is True]
    if audible:
        audible.sort(key=lambda path: _file_rank(path, container))
        return audible[0], True
    if all(has is False for _, has in probed):
        return probed[0][0], False
    return candidates[0], None


def _delete_files(paths: list[Path], log: ProgressCallback) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError as exc:
            log(f"无法删除无声文件：{path}（{exc}）")


def drop_silent_siblings(
    out_dir: Path,
    filepath: Optional[str],
    video_id: str,
    keep: Path,
    log: ProgressCallback,
) -> None:
    """合并结果已经有声音时，删掉旁边只剩画面的临时文件，避免打开错文件。"""
    try:
        keep_key = keep.resolve()
    except OSError:
        keep_key = keep
    for path in _candidate_files(out_dir, filepath, video_id):
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key == keep_key:
            continue
        if file_has_audio(str(path)) is False:
            _delete_files([path], log)


def download(
    opts: DownloadOptions,
    log: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    on_progress: Optional[RichProgressCallback] = None,
) -> DownloadResult:
    """
    执行下载。返回 DownloadResult（含最终文件路径，若可解析）。
    保存目录在本函数（工作线程）里创建，失败只影响这一条任务。
    普通视频下载必须带音轨。ffprobe 确认没有声音时会换格式重试一次，仍没有则失败。
    """
    log = log or (lambda _m: None)
    ensure_bundled_ffmpeg()
    log(f"准备保存目录：{opts.output_dir}")
    out_dir = ensure_output_dir(opts.output_dir, cancel_event=cancel_event)

    fmt = build_format_selector(opts)
    container = _merge_container(opts.video_format)
    outtmpl = str(out_dir / "%(title)s [%(id)s].%(ext)s")
    hook = _ProgressHook(log, on_progress=on_progress, cancel_event=cancel_event)

    ydl_opts: dict[str, Any] = {
        "format": fmt,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": False,
        "js_runtimes": {"deno": {}, "node": {}},
    }
    ffmpeg = resolve_tool("ffmpeg")
    if ffmpeg:
        ydl_opts["ffmpeg_location"] = str(Path(ffmpeg).parent)
    if _is_audio_only(opts):
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": opts.audio_format if opts.audio_format != "best" else "m4a",
                "preferredquality": "192",
            }
        ]
    else:
        ydl_opts["merge_output_format"] = container
        sort = format_sort_for(opts)
        if sort:
            ydl_opts["format_sort"] = sort

    log(f"格式选择器：{fmt}")
    if (
        not _is_audio_only(opts)
        and container == "mp4"
        and opts.audio_format not in ("m4a", "best", "")
    ):
        log("视频格式为 mp4，优先合并 aac/m4a 音轨，这样可以直接封装并保留声音。")
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
    else:
        log("默认不使用 Cookie。若站点返回 403 或 Cloudflare 验证，会自动用浏览器指纹再试一次。")

    if cancel_event and cancel_event.is_set():
        raise RuntimeError("已取消")

    attempts = build_extract_attempts(opts.url, opts.cookiefile, opts.cookiesfrombrowser)

    def on_success(ydl: Any, info: dict[str, Any]) -> DownloadResult:
        filepath: Optional[str] = None
        if "entries" in info and info["entries"]:
            info = next((e for e in info["entries"] if e), info)
        try:
            filepath = ydl.prepare_filename(info)
            # 音频提取后扩展名可能变化
            if _is_audio_only(opts):
                pref = opts.audio_format if opts.audio_format != "best" else "m4a"
                filepath = str(Path(filepath).with_suffix(f".{pref}"))
            elif container in ("mp4", "webm", "mkv"):
                p = Path(filepath)
                if p.suffix.lower().lstrip(".") != container:
                    candidate = p.with_suffix(f".{container}")
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
        video_id = str((info or {}).get("id") or "")
        return DownloadResult(
            title=title,
            filepath=filepath,
            output_dir=opts.output_dir,
            video_id=video_id,
        )

    def run(format_spec: str, *, overwrite: bool) -> DownloadResult:
        ydl_opts["format"] = format_spec
        if overwrite:
            ydl_opts["overwrites"] = True
        return _extract_with_retries(
            opts.url,
            ydl_opts,
            attempts,
            log,
            download=True,
            cancel_event=cancel_event,
            on_success=on_success,
        )

    def finish(result: DownloadResult) -> DownloadResult:
        log(f"全部完成：{result.title}")
        if on_progress:
            on_progress(100.0, f"全部完成：{result.title}", result.filepath)
        return result

    result = run(fmt, overwrite=False)
    if _is_audio_only(opts):
        return finish(result)

    chosen, has_audio = select_downloaded_file(
        out_dir, result.filepath, result.video_id, container
    )
    if chosen is not None:
        result.filepath = str(chosen)
    if has_audio is True and chosen is not None:
        drop_silent_siblings(out_dir, result.filepath, result.video_id, chosen, log)
    if has_audio is not False:
        if has_audio is None and resolve_tool("ffprobe") is None:
            log("未找到 ffprobe，无法确认音轨。")
        return finish(result)

    silent = _candidate_files(out_dir, result.filepath, result.video_id)
    log("下载的文件没有音轨，正在改用包含音频的格式重新下载…")
    _delete_files(silent, log)
    retry_fmt = build_audio_retry_selector(opts)
    log(f"重试格式选择器：{retry_fmt}")
    result = run(retry_fmt, overwrite=True)
    chosen, has_audio = select_downloaded_file(
        out_dir, result.filepath, result.video_id, container
    )
    if chosen is not None:
        result.filepath = str(chosen)
    if has_audio is True and chosen is not None:
        drop_silent_siblings(out_dir, result.filepath, result.video_id, chosen, log)
    # 确认没音轨，或重试后根本没有文件：失败。ffprobe 自己没跑成则不误伤。
    if has_audio is False or chosen is None:
        _delete_files(_candidate_files(out_dir, result.filepath, result.video_id), log)
        raise RuntimeError(NO_AUDIO_TRACK)
    return finish(result)
