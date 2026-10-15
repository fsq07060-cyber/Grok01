# -*- coding: utf-8 -*-
"""下载任务模型与状态常量。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TaskStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


STATUS_LABELS_ZH = {
    TaskStatus.PENDING: "待添加",
    TaskStatus.QUEUED: "排队中",
    TaskStatus.DOWNLOADING: "下载中",
    TaskStatus.COMPLETED: "已完成",
    TaskStatus.FAILED: "失败",
    TaskStatus.CANCELLED: "已取消",
}


@dataclass
class DownloadTask:
    """单个下载任务。"""

    url: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: TaskStatus = TaskStatus.QUEUED
    progress: float = 0.0  # 0–100
    filepath: Optional[str] = None
    output_dir: str = ""
    error: Optional[str] = None
    title: str = ""
    # 下载选项快照
    resolution: str = "1080"
    video_format: str = "mp4"
    audio_format: str = "m4a"
    audio_only: bool = False
    want_separate: bool = False

    def status_label(self) -> str:
        return STATUS_LABELS_ZH.get(self.status, str(self.status))

    def display_name(self) -> str:
        if self.title:
            return self.title
        u = self.url.strip()
        if len(u) > 64:
            return u[:61] + "…"
        return u or self.id
