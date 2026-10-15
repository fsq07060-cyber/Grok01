# -*- coding: utf-8 -*-
"""从多行 / 逗号分隔文本中解析 URL 列表。"""

from __future__ import annotations

import re
from typing import List

# 粗匹配 http(s) URL；后续由下载器校验是否可下
_URL_RE = re.compile(r"https?://[^\s,;，；<>\"']+", re.IGNORECASE)


def parse_urls(text: str) -> List[str]:
    """
    解析批量输入：支持换行、逗号、分号、中文逗号/分号分隔。
    去重且保持首次出现顺序；去掉尾部标点。
    """
    if not text or not text.strip():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(").,];}'\"」』）")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out
