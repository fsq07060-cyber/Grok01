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
_COOKIE_FAIL_RE = re.compile(
    r"failed to load cookies|cookie 文件不存在|无法读取 cookie|不支持的浏览器|"
    r"could not copy .{0,40}cookie|cookies database|could not find .{0,40}cookie",
    re.IGNORECASE,
)
_BOT_RE = re.compile(
    r"not a bot|sign in to confirm|login required|age[- ]restricted|use --cookies",
    re.IGNORECASE,
)

_GONE_MESSAGE = "该视频已失效或已删除 (HTTP {code})"
_COOKIE_MESSAGE = "无法读取 Cookie。请确认浏览器可用，或改用 Netscape 格式的 Cookie 文件。"
_BOT_MESSAGE = (
    "需要登录或 Cookie（站点判定为机器人/要求登录）。"
    "请在界面选择浏览器 Cookie，或填写 Netscape 格式 Cookie 文件后重试。"
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
    code = missing_media_code(exc)
    if code in (404, 410):
        return _GONE_MESSAGE.format(code=code)
    if is_cookie_load_failure(exc):
        return _COOKIE_MESSAGE
    blob = _exception_blob(exc)
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
