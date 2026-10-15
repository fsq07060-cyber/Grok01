# -*- coding: utf-8 -*-
"""下载任务队列：有限并发、线程安全状态更新。"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from core.downloader import DownloadOptions, download
from core.errors import friendly_error
from core.task_model import DownloadTask, TaskStatus

# UI 侧在主线程用 after() 消费；队列回调本身在工作线程触发
TaskListener = Callable[[DownloadTask], None]


class DownloadQueue:
    """
    简单任务队列。
    - max_workers：默认 2，避免打爆网络
    - 每个任务独立 cancel_event
    - on_update：任意状态/进度变化时回调（工作线程）
    """

    def __init__(
        self,
        max_workers: int = 2,
        on_update: Optional[TaskListener] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self.max_workers = max(1, int(max_workers))
        self.on_update = on_update
        self.on_log = on_log or (lambda _m: None)
        self._lock = threading.Lock()
        self._tasks: dict[str, DownloadTask] = {}
        self._order: list[str] = []
        self._cancel_events: dict[str, threading.Event] = {}
        self._active = 0
        self._cv = threading.Condition(self._lock)
        self._stopped = False
        self._dispatcher: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._dispatcher and self._dispatcher.is_alive():
            return
        self._stopped = False
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="dl-queue", daemon=True
        )
        self._dispatcher.start()

    def stop(self) -> None:
        with self._cv:
            self._stopped = True
            for ev in self._cancel_events.values():
                ev.set()
            self._cv.notify_all()

    def tasks(self) -> list[DownloadTask]:
        with self._lock:
            return [self._tasks[i] for i in self._order if i in self._tasks]

    def get(self, task_id: str) -> Optional[DownloadTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def add(self, task: DownloadTask) -> DownloadTask:
        with self._cv:
            task.status = TaskStatus.QUEUED
            task.progress = 0.0
            self._tasks[task.id] = task
            self._order.append(task.id)
            self._cancel_events[task.id] = threading.Event()
            self._cv.notify_all()
        self._emit(task)
        return task

    def remove(self, task_id: str) -> bool:
        """
        删除任务：未完成则先取消；已完成/失败/取消仅从列表移除。
        返回是否移除成功。
        """
        with self._cv:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status in (TaskStatus.DOWNLOADING, TaskStatus.QUEUED, TaskStatus.PENDING):
                ev = self._cancel_events.get(task_id)
                if ev:
                    ev.set()
                if task.status == TaskStatus.QUEUED:
                    task.status = TaskStatus.CANCELLED
            # 从列表移除（进行中的工作线程结束时会再检查）
            self._tasks.pop(task_id, None)
            if task_id in self._order:
                self._order.remove(task_id)
            self._cv.notify_all()
        return True

    def cancel(self, task_id: str) -> None:
        emit_task: Optional[DownloadTask] = None
        with self._lock:
            ev = self._cancel_events.get(task_id)
            task = self._tasks.get(task_id)
            if ev:
                ev.set()
            if task and task.status == TaskStatus.QUEUED:
                task.status = TaskStatus.CANCELLED
                emit_task = task
        if emit_task is not None:
            self._emit(emit_task)

    def set_max_workers(self, n: int) -> None:
        with self._cv:
            self.max_workers = max(1, int(n))
            self._cv.notify_all()

    def _emit(self, task: DownloadTask) -> None:
        if self.on_update:
            try:
                self.on_update(task)
            except Exception:
                pass

    def _dispatch_loop(self) -> None:
        while True:
            task: Optional[DownloadTask] = None
            cancel_ev: Optional[threading.Event] = None
            to_emit: Optional[DownloadTask] = None
            start_worker = False
            with self._cv:
                while not self._stopped:
                    if self._active >= self.max_workers:
                        self._cv.wait(timeout=0.5)
                        continue
                    next_id = None
                    for tid in self._order:
                        t = self._tasks.get(tid)
                        if t and t.status == TaskStatus.QUEUED:
                            next_id = tid
                            break
                    if next_id is None:
                        self._cv.wait(timeout=0.5)
                        if self._stopped:
                            break
                        continue
                    task = self._tasks[next_id]
                    cancel_ev = self._cancel_events.get(next_id)
                    if cancel_ev is None or cancel_ev.is_set():
                        task.status = TaskStatus.CANCELLED
                        to_emit = task
                        task = None
                        cancel_ev = None
                        break
                    task.status = TaskStatus.DOWNLOADING
                    self._active += 1
                    to_emit = task
                    start_worker = True
                    break
                if self._stopped and not start_worker and to_emit is None:
                    return
            # 锁外回调。回调里会 after() 到 Tk；持锁调用会和主线程 tasks() 交叉死锁。
            if to_emit is not None:
                self._emit(to_emit)
            if not start_worker or task is None or cancel_ev is None:
                continue
            threading.Thread(
                target=self._run_one,
                args=(task.id, cancel_ev),
                name=f"dl-{task.id}",
                daemon=True,
            ).start()

    def _run_one(self, task_id: str, cancel_ev: threading.Event) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
        if not task:
            with self._cv:
                self._active = max(0, self._active - 1)
                self._cv.notify_all()
            return

        def log(msg: str) -> None:
            try:
                self.on_log(f"[{task.display_name()}] {msg}")
            except Exception:
                # 日志走 Tk after()。界面回调失败不能把下载线程打成任务失败。
                pass

        def on_progress(pct: float, msg: str, filepath: Optional[str]) -> None:
            with self._lock:
                t = self._tasks.get(task_id)
                if not t:
                    return
                t.progress = pct
                if filepath:
                    t.filepath = filepath
                snap = t
            self._emit(snap)

        try:
            if cancel_ev.is_set():
                raise RuntimeError("已取消")
            opts = DownloadOptions(
                url=task.url,
                output_dir=task.output_dir,
                resolution=task.resolution,
                video_format=task.video_format,
                audio_format=task.audio_format,
                audio_only=task.audio_only,
                cookiefile=task.cookiefile,
                cookiesfrombrowser=task.cookiesfrombrowser,
            )
            result = download(
                opts,
                log=log,
                cancel_event=cancel_ev,
                on_progress=on_progress,
            )
            with self._lock:
                t = self._tasks.get(task_id)
                if not t:
                    return
                if cancel_ev.is_set():
                    t.status = TaskStatus.CANCELLED
                    t.error = "已取消"
                else:
                    t.status = TaskStatus.COMPLETED
                    t.progress = 100.0
                    t.title = result.title or t.title
                    t.filepath = result.filepath or t.filepath
                    t.error = None
                snap = t
            self._emit(snap)
            if task.want_separate and snap.status == TaskStatus.COMPLETED:
                log(
                    "已勾选「分离人声」：v2 stub，下载完成后请在安装 Demucs 后手动对接。"
                )
        except Exception as e:
            msg = str(e)
            reason = friendly_error(e)
            cancelled = cancel_ev.is_set() or "已取消" in msg or reason == "已取消"
            with self._lock:
                t = self._tasks.get(task_id)
                if not t:
                    return
                if cancelled:
                    t.status = TaskStatus.CANCELLED
                    t.error = "已取消"
                else:
                    t.status = TaskStatus.FAILED
                    t.error = reason
                snap = t
            self._emit(snap)
            try:
                if cancelled:
                    self.on_log(f"已取消 {task.url}")
                else:
                    self.on_log(f"任务失败 url={task.url} 原因：{reason}")
            except Exception:
                pass
        finally:
            with self._cv:
                self._active = max(0, self._active - 1)
                self._cv.notify_all()
