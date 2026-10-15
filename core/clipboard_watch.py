# -*- coding: utf-8 -*-
"""剪贴板轮询：检测到常见视频站 http(s) 链接则回调。"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlparse


# 常见视频/流媒体主机（可扩展）；降低误报
VIDEO_HOST_HINTS = (
    "youtube.com",
    "youtu.be",
    "www.youtube.com",
    "m.youtube.com",
    "pornhub.com",
    "www.pornhub.com",
    "xvideos.com",
    "www.xvideos.com",
    "bilibili.com",
    "www.bilibili.com",
    "b23.tv",
    "vimeo.com",
    "www.vimeo.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "www.tiktok.com",
    "twitch.tv",
    "www.twitch.tv",
    "nicovideo.jp",
    "www.nicovideo.jp",
)

URL_RE = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)


OnUrlCallback = Callable[[str], None]
ClipboardGetter = Callable[[], str]


def extract_video_url(text: str) -> Optional[str]:
    """从文本中提取第一个疑似视频站 URL。"""
    if not text:
        return None
    for m in URL_RE.finditer(text.strip()):
        url = m.group(0).rstrip(").,];}'\"")
        host = (urlparse(url).hostname or "").lower()
        for hint in VIDEO_HOST_HINTS:
            hint_l = hint.lower()
            if host == hint_l or host.endswith("." + hint_l):
                return url
            if hint_l in host:
                return url
    return None


class ClipboardWatcher:
    """
    后台线程轮询剪贴板。
    同一 URL 在冷却期内不重复触发。
    请传入 get_clipboard（例如主窗口的 clipboard_get），避免额外创建 Tk 根窗口。
    """

    def __init__(
        self,
        on_url: OnUrlCallback,
        get_clipboard: Optional[ClipboardGetter] = None,
        interval_sec: float = 1.0,
        cooldown_sec: float = 30.0,
    ):
        self.on_url = on_url
        self.get_clipboard = get_clipboard
        self.interval_sec = interval_sec
        self.cooldown_sec = cooldown_sec
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_url: Optional[str] = None
        self._last_fire: float = 0.0
        self.enabled = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            self.enabled = True
            return
        self._stop.clear()
        self.enabled = True
        self._thread = threading.Thread(target=self._loop, name="clipboard-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.enabled = False
        self._stop.set()

    def poll(self) -> None:
        """检查一次剪贴板。必须在 Tk 主线程调用，clipboard_get 不能跨线程。"""
        if not self.enabled or self._stop.is_set():
            return
        try:
            url = extract_video_url(self._read())
        except Exception:
            return
        now = time.monotonic()
        if url and (url != self._last_url or (now - self._last_fire) >= self.cooldown_sec):
            self._last_url = url
            self._last_fire = now
            try:
                self.on_url(url)
            except Exception:
                pass

    def set_enabled(self, value: bool) -> None:
        if value:
            self.start()
        else:
            self.enabled = False

    def _read(self) -> str:
        if not self.get_clipboard:
            return ""
        try:
            data = self.get_clipboard()
            return data if isinstance(data, str) else ""
        except Exception:
            return ""

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.enabled:
                try:
                    url = extract_video_url(self._read())
                    now = time.monotonic()
                    if url and (
                        url != self._last_url
                        or (now - self._last_fire) >= self.cooldown_sec
                    ):
                        self._last_url = url
                        self._last_fire = now
                        try:
                            self.on_url(url)
                        except Exception:
                            pass
                except Exception:
                    pass
            self._stop.wait(self.interval_sec)
