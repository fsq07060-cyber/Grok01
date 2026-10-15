# -*- coding: utf-8 -*-
"""把 yt-dlp / 网络异常收成短的中文说明，供探测与下载失败共用。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional


_HTTP_410_RE = re.compile(r"HTTP(?:\s+Error)?\s*410\b|\b410\s*:?\s*Gone\b", re.IGNORECASE)
_HTTP_404_RE = re.compile(
    r"HTTP(?:\s+Error)?\s*404\b|\b404\s*:?\s*Not\s*Found\b|\b404\b",
    re.IGNORECASE,
)
_HTTP_403_RE = re.compile(
    r"HTTP(?:\s+Error)?\s*403\b|\b403\s*:?\s*Forbidden\b",
    re.IGNORECASE,
)
_COOKIE_FAIL_RE = re.compile(
    r"failed to load cookies|cookie 文件不存在|无法读取 cookie|不支持的浏览器|"
    r"could not copy .{0,40}cookie|cookies database|could not find .{0,40}cookie",
    re.IGNORECASE,
)
_BOT_RE = re.compile(
    r"not a bot|sign in to confirm|login required|age[- ]restricted|use --cookies",
    re.IGNORECASE,
)
# 某个 player client 自己拒绝，换下一个客户端仍可能成功。
_CLIENT_REJECT_RE = re.compile(
    r"playback on other websites has been disabled|"
    r"drm[- ]protected|only images are available",
    re.IGNORECASE,
)

_GONE_MESSAGE = "该视频已失效或已删除 (HTTP {code})"
_COOKIE_MESSAGE = "无法读取 Cookie。请确认浏览器可用，或改用 Netscape 格式的 Cookie 文件。"
_BOT_MESSAGE = (
    "需要登录或 Cookie（站点判定为机器人/要求登录）。"
    "请在界面选择浏览器 Cookie，或填写 Netscape 格式 Cookie 文件后重试。"
)
# YouTube 无 Cookie 客户端都试过之后的最后提示。不是「每条都能下」。
YOUTUBE_COOKIES_NEEDED = (
    "这个视频不用 Cookie 仍无法下载（YouTube 返回 403 或要求登录）。"
    "年龄限制或部分地区限制的视频可能仍需要 Cookie："
    "可在界面选择浏览器 Cookie，或填写 Netscape 格式的 Cookie 文件后再试。"
)
# 下载结果没有音轨。默认任务必须带声音，不能当成成功。
NO_AUDIO_TRACK = (
    "下载完成但文件没有音轨（只有画面）。"
    "默认下载必须带声音。已自动改用带音频的格式重试，仍然没有音轨。"
    "请换清晰度，或把音频格式设为 m4a 后再试。"
)
# 格式选择阶段就没有可合并的声音，不能退回只有画面的文件。
FORMAT_NEEDS_AUDIO = (
    "没有找到包含音轨的可用格式，已停止下载（不会保存只有画面的文件）。"
    "请换清晰度，或把音频格式设为 m4a 后再试。"
)
FFMPEG_REQUIRED = (
    "需要 ffmpeg 才能把画面和声音合并。"
    "请确认程序目录里的 ffmpeg 可用后重试。"
)

_FORMAT_UNAVAILABLE_RE = re.compile(
    r"requested format is not available|no video formats found|no audio formats found",
    re.IGNORECASE,
)
_FFMPEG_MISSING_RE = re.compile(
    r"ffmpeg is not installed|ffmpeg.{0,40}not found|merging of multiple formats but ffmpeg",
    re.IGNORECASE,
)


def _iter_exceptions(exc: BaseException):
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        cause = cur.__cause__
        context = cur.__context__
        if cause is not None:
            stack.append(cause)
        if context is not None and context is not cause:
            stack.append(context)
        exc_info = getattr(cur, "exc_info", None)
        if (
            isinstance(exc_info, tuple)
            and len(exc_info) >= 2
            and isinstance(exc_info[1], BaseException)
        ):
            stack.append(exc_info[1])


def _exception_blob(exc: BaseException) -> str:
    parts: list[str] = []
    for cur in _iter_exceptions(exc):
        parts.append(str(cur))
        parts.append(cur.__class__.__name__)
    return "\n".join(parts)


def _code_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    if _HTTP_410_RE.search(text):
        return 410
    if _HTTP_404_RE.search(text):
        return 404
    return None


def missing_media_code(exc: BaseException) -> Optional[int]:
    """从异常链识别 HTTP 410/404。同一条里两者都有时优先 410。"""
    found_404 = False
    for cur in _iter_exceptions(exc):
        for attr in ("code", "status"):
            try:
                val = getattr(cur, attr, None)
            except Exception:
                continue
            try:
                ival = int(val)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if ival == 410:
                return 410
            if ival == 404:
                found_404 = True
        text_code = _code_from_text(str(cur))
        if text_code == 410:
            return 410
        if text_code == 404:
            found_404 = True
    return 404 if found_404 else None


def is_cookie_load_failure(exc: BaseException) -> bool:
    blob = _exception_blob(exc)
    return _COOKIE_FAIL_RE.search(blob) is not None


def is_format_unavailable(exc: BaseException) -> bool:
    """yt-dlp 没有选出符合格式串的流。默认下载把这当成「没有可合并的音轨」。"""
    return _FORMAT_UNAVAILABLE_RE.search(_exception_blob(exc)) is not None


def is_ffmpeg_missing(exc: BaseException) -> bool:
    return _FFMPEG_MISSING_RE.search(_exception_blob(exc)) is not None


def is_youtube_client_retryable(exc: BaseException) -> bool:
    """
    YouTube 换下一个 player client 还有意义的失败：
    HTTP 403、登录/机器人确认、Cookie 读失败、嵌入或 DRM 被这个客户端拒绝。
    404/410、取消、不支持的浏览器名称不再重试。
    """
    if "已取消" in str(exc):
        return False
    if missing_media_code(exc) in (404, 410):
        return False
    blob = _exception_blob(exc)
    if "不支持的浏览器" in blob:
        return False
    if is_cookie_load_failure(exc):
        return True
    if _HTTP_403_RE.search(blob):
        return True
    if _BOT_RE.search(blob):
        return True
    if _CLIENT_REJECT_RE.search(blob):
        return True
    return False


def youtube_retry_reason(exc: BaseException) -> str:
    """日志里的短原因，避免在还有下一个客户端时弹出「请先填 Cookie」。"""
    if is_cookie_load_failure(exc):
        return "无法读取 Cookie"
    blob = _exception_blob(exc)
    if _HTTP_403_RE.search(blob):
        return "HTTP 403"
    if _BOT_RE.search(blob):
        return "需要登录确认"
    if _CLIENT_REJECT_RE.search(blob):
        return "该客户端不可用"
    return "失败"


def _short_reason(text: str, limit: int = 180) -> str:
    text = re.sub(r"^ERROR:\s*", "", (text or "").strip(), flags=re.IGNORECASE)
    text = " ".join(text.split())
    if not text:
        return "未知错误"
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def friendly_error(exc: BaseException) -> str:
    """
    探测 / 下载失败时给用户看的短原因。
    HTTP 410/404 → 「该视频已失效或已删除 (HTTP …)」。
    """
    if isinstance(exc, str):
        exc = RuntimeError(exc)
    blob = _exception_blob(exc)
    if NO_AUDIO_TRACK in blob:
        return NO_AUDIO_TRACK
    if FORMAT_NEEDS_AUDIO in blob:
        return FORMAT_NEEDS_AUDIO
    if YOUTUBE_COOKIES_NEEDED in blob:
        return YOUTUBE_COOKIES_NEEDED
    if is_ffmpeg_missing(exc):
        return FFMPEG_REQUIRED
    code = missing_media_code(exc)
    if code in (404, 410):
        return _GONE_MESSAGE.format(code=code)
    if is_cookie_load_failure(exc):
        return _COOKIE_MESSAGE
    if _BOT_RE.search(blob):
        return _BOT_MESSAGE
    return _short_reason(str(exc))


@dataclass
class ProbeBatchResult:
    successes: list[tuple[str, str]] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    untested: list[str] = field(default_factory=list)


def run_probe_batch(
    urls: list[str],
    probe_one: Callable[[str], str],
    log: Callable[[str], None],
) -> ProbeBatchResult:
    """
    逐条探测。单条失败继续下一条。
    Cookie 读取失败时停止，剩下的记入 untested（再探也会同样失败）。
    """
    result = ProbeBatchResult()
    total = len(urls)
    for index, url in enumerate(urls):
        try:
            detail = probe_one(url)
        except Exception as e:
            reason = friendly_error(e)
            result.failures.append((url, reason))
            log(f"探测失败 [{index + 1}/{total}] {url} — {reason}")
            if is_cookie_load_failure(e):
                result.untested = list(urls[index + 1 :])
                if result.untested:
                    log(
                        f"Cookie 无法读取，已停止探测。其余 {len(result.untested)} 个链接未探测。"
                    )
                break
            continue
        result.successes.append((url, detail))
        log(f"探测成功 [{index + 1}/{total}] {url} — {detail}")
    return result


def format_probe_failure_dialog(result: ProbeBatchResult) -> str:
    """失败弹窗：写明哪条 URL 失败；若有未测链接则明确列出。"""
    if not result.failures and not result.untested:
        return ""
    lines: list[str] = []
    if result.failures:
        lines.append("以下链接探测失败：")
        lines.append("")
        shown = result.failures[:8]
        for url, reason in shown:
            lines.append(url)
            lines.append(reason)
            lines.append("")
        extra = len(result.failures) - len(shown)
        if extra:
            lines.append(f"另有 {extra} 条失败，见下方日志。")
            lines.append("")
    if result.untested:
        lines.append("以下链接未探测（本次没有测试它们）：")
        for url in result.untested[:8]:
            lines.append(url)
        extra_untested = len(result.untested) - min(8, len(result.untested))
        if extra_untested:
            lines.append(f"另有 {extra_untested} 条未探测。")
    elif result.successes and result.failures:
        lines.append(f"其余 {len(result.successes)} 个链接已逐条探测，结果见日志。")
    return "\n".join(lines).strip()
