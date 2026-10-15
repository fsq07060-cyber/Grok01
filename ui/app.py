# -*- coding: utf-8 -*-
"""customtkinter 主窗口：批量 URL、任务列表、独立进度、打开/删除。"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Dict, Optional

import customtkinter as ctk

from core import separator as sep
from core.clipboard_watch import ClipboardWatcher
from core.downloader import list_formats
from core.platform_util import open_directory, open_file
from core.task_model import DownloadTask, TaskStatus
from core.task_queue import DownloadQueue
from core.url_parse import parse_urls


RESOLUTIONS = ["best", "2160", "1440", "1080", "720", "480", "audio"]
VIDEO_FORMATS = ["mp4", "webm", "mkv", "best"]
AUDIO_FORMATS = ["m4a", "mp3", "opus", "best"]
DEFAULT_CONCURRENCY = 2


class TaskRow(ctk.CTkFrame):
    """单行任务：标题、状态、进度条、操作按钮。"""

    def __init__(self, master: ctk.CTkScrollableFrame, app: "App", task: DownloadTask):
        super().__init__(master)
        self.app = app
        self.task_id = task.id
        self.grid_columnconfigure(1, weight=1)

        self.lbl_name = ctk.CTkLabel(self, text=task.display_name(), anchor="w")
        self.lbl_name.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 0))

        self.lbl_status = ctk.CTkLabel(self, text=task.status_label(), width=70, anchor="w")
        self.lbl_status.grid(row=1, column=0, sticky="w", padx=8, pady=2)

        self.progress = ctk.CTkProgressBar(self)
        self.progress.grid(row=1, column=1, sticky="ew", padx=8, pady=2)
        self.progress.set(max(0.0, min(1.0, task.progress / 100.0)))

        self.lbl_pct = ctk.CTkLabel(self, text=f"{task.progress:.0f}%", width=48)
        self.lbl_pct.grid(row=1, column=2, padx=4, pady=2)

        btn_frm = ctk.CTkFrame(self, fg_color="transparent")
        btn_frm.grid(row=0, column=2, rowspan=2, padx=4, pady=4)

        self.btn_open_file = ctk.CTkButton(
            btn_frm, text="打开文件", width=80, command=self._open_file
        )
        self.btn_open_file.pack(side="left", padx=2)
        self.btn_open_dir = ctk.CTkButton(
            btn_frm, text="打开目录", width=80, command=self._open_dir
        )
        self.btn_open_dir.pack(side="left", padx=2)
        self.btn_del = ctk.CTkButton(
            btn_frm,
            text="删除",
            width=60,
            fg_color="gray40",
            hover_color="gray30",
            command=self._delete,
        )
        self.btn_del.pack(side="left", padx=2)

        self.refresh(task)

    def refresh(self, task: DownloadTask) -> None:
        self.lbl_name.configure(text=task.display_name())
        self.lbl_status.configure(text=task.status_label())
        self.progress.set(max(0.0, min(1.0, task.progress / 100.0)))
        self.lbl_pct.configure(text=f"{task.progress:.0f}%")
        done = task.status == TaskStatus.COMPLETED and bool(task.filepath)
        self.btn_open_file.configure(state="normal" if done else "disabled")
        has_dir = bool(task.output_dir) or bool(task.filepath)
        self.btn_open_dir.configure(state="normal" if has_dir else "disabled")

    def _open_file(self) -> None:
        self.app.open_task_file(self.task_id)

    def _open_dir(self) -> None:
        self.app.open_task_dir(self.task_id)

    def _delete(self) -> None:
        self.app.remove_task(self.task_id)


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("流媒体下载器 · 人声/伴奏分离")
        self.geometry("900x720")
        self.minsize(720, 560)

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self._clipboard_prompting = False
        self._rows: Dict[str, TaskRow] = {}

        demucs_ok, demucs_msg = sep.check_status()
        self._demucs_ok = demucs_ok
        self._demucs_msg = demucs_msg

        self._queue = DownloadQueue(
            max_workers=DEFAULT_CONCURRENCY,
            on_update=self._on_task_update,
            on_log=self._log,
        )

        self._build_ui()
        self._queue.start()

        self._watcher = ClipboardWatcher(
            on_url=self._on_clipboard_url,
            get_clipboard=self._safe_clipboard_get,
        )
        if self.var_clip.get():
            self._watcher.start()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(6, weight=1)
        self.grid_rowconfigure(7, weight=1)

        # 批量 URL
        frm_url = ctk.CTkFrame(self)
        frm_url.grid(row=0, column=0, sticky="ew", **pad)
        frm_url.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frm_url,
            text="视频链接（可粘贴多个：换行 / 逗号分隔）",
            anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="ew", padx=8, pady=(8, 0))
        self.txt_urls = ctk.CTkTextbox(frm_url, height=72)
        self.txt_urls.grid(row=1, column=0, columnspan=3, sticky="ew", padx=8, pady=6)

        btn_row = ctk.CTkFrame(frm_url, fg_color="transparent")
        btn_row.grid(row=2, column=0, columnspan=3, sticky="ew", padx=4, pady=(0, 8))
        ctk.CTkButton(btn_row, text="添加任务", width=120, command=self._on_add).pack(
            side="left", padx=4
        )
        ctk.CTkButton(btn_row, text="探测首个链接", width=120, command=self._on_probe).pack(
            side="left", padx=4
        )
        ctk.CTkButton(
            btn_row,
            text="清空输入",
            width=100,
            fg_color="gray40",
            command=self._clear_url_input,
        ).pack(side="left", padx=4)

        # 保存路径
        frm_path = ctk.CTkFrame(self)
        frm_path.grid(row=1, column=0, sticky="ew", **pad)
        frm_path.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(frm_path, text="保存路径").grid(row=0, column=0, padx=8, pady=8)
        default_dir = str(Path.home() / "Downloads")
        self.var_path = tk.StringVar(value=default_dir)
        ctk.CTkEntry(frm_path, textvariable=self.var_path).grid(
            row=0, column=1, sticky="ew", padx=8, pady=8
        )
        ctk.CTkButton(frm_path, text="浏览…", width=100, command=self._browse_dir).grid(
            row=0, column=2, padx=8, pady=8
        )

        # 选项行
        frm_opts = ctk.CTkFrame(self)
        frm_opts.grid(row=2, column=0, sticky="ew", **pad)
        for i in range(8):
            frm_opts.grid_columnconfigure(i, weight=1)

        ctk.CTkLabel(frm_opts, text="分辨率").grid(row=0, column=0, padx=4, pady=8, sticky="e")
        self.var_res = tk.StringVar(value="1080")
        ctk.CTkOptionMenu(frm_opts, variable=self.var_res, values=RESOLUTIONS, width=100).grid(
            row=0, column=1, padx=4, pady=8, sticky="w"
        )

        ctk.CTkLabel(frm_opts, text="视频格式").grid(row=0, column=2, padx=4, pady=8, sticky="e")
        self.var_vfmt = tk.StringVar(value="mp4")
        ctk.CTkOptionMenu(frm_opts, variable=self.var_vfmt, values=VIDEO_FORMATS, width=100).grid(
            row=0, column=3, padx=4, pady=8, sticky="w"
        )

        ctk.CTkLabel(frm_opts, text="音频格式").grid(row=0, column=4, padx=4, pady=8, sticky="e")
        self.var_afmt = tk.StringVar(value="m4a")
        ctk.CTkOptionMenu(frm_opts, variable=self.var_afmt, values=AUDIO_FORMATS, width=100).grid(
            row=0, column=5, padx=4, pady=8, sticky="w"
        )

        ctk.CTkLabel(frm_opts, text="并发数").grid(row=0, column=6, padx=4, pady=8, sticky="e")
        self.var_conc = tk.StringVar(value=str(DEFAULT_CONCURRENCY))
        ctk.CTkOptionMenu(
            frm_opts,
            variable=self.var_conc,
            values=["1", "2", "3", "4"],
            width=70,
            command=self._on_conc_change,
        ).grid(row=0, column=7, padx=4, pady=8, sticky="w")

        # 开关行
        frm_sw = ctk.CTkFrame(self)
        frm_sw.grid(row=3, column=0, sticky="ew", **pad)

        self.var_separate = tk.BooleanVar(value=False)
        self.chk_sep = ctk.CTkCheckBox(
            frm_sw,
            text="分离人声（人声 + 背景音乐，非乐器分轨）",
            variable=self.var_separate,
            command=self._on_sep_toggle,
        )
        self.chk_sep.pack(side="left", padx=8, pady=8)
        if not self._demucs_ok:
            self.chk_sep.configure(text="分离人声（v2 / 需安装 Demucs）")

        self.var_clip = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            frm_sw,
            text="监听剪贴板链接并弹窗",
            variable=self.var_clip,
            command=self._on_clip_toggle,
        ).pack(side="left", padx=16, pady=8)

        # 状态
        frm_stat = ctk.CTkFrame(self)
        frm_stat.grid(row=4, column=0, sticky="ew", **pad)
        self.lbl_status = ctk.CTkLabel(frm_stat, text="就绪", anchor="w")
        self.lbl_status.pack(side="left", padx=12, pady=8, fill="x", expand=True)

        tip = (
            "合规提示：请仅下载你有权获取的内容；使用 NSFW 站点时请遵守当地法律与网站条款。"
        )
        ctk.CTkLabel(self, text=tip, text_color="gray60", wraplength=860, justify="left").grid(
            row=5, column=0, sticky="ew", padx=12, pady=(0, 4)
        )

        # 任务列表（row 6）+ 日志（row 7）
        self.task_list = ctk.CTkScrollableFrame(self, height=200, label_text="下载任务")
        self.task_list.grid(row=6, column=0, sticky="nsew", padx=12, pady=(4, 6))
        self.task_list.grid_columnconfigure(0, weight=1)

        # 日志
        self.log_box = ctk.CTkTextbox(self, height=140)
        self.log_box.grid(row=7, column=0, sticky="nsew", padx=12, pady=(4, 12))
        self._log("欢迎使用流媒体下载器。可一次粘贴多个链接后点击「添加任务」。")
        self._log(self._demucs_msg)
        self._log(f"默认并发：{DEFAULT_CONCURRENCY}（可在上方调整）")

    # ------------------------------------------------------------------ helpers
    def _log(self, msg: str) -> None:
        def append() -> None:
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")

        if threading.current_thread() is threading.main_thread():
            append()
        else:
            self.after(0, append)

    def _set_status(self, text: str) -> None:
        def upd() -> None:
            self.lbl_status.configure(text=text)

        if threading.current_thread() is threading.main_thread():
            upd()
        else:
            self.after(0, upd)

    def _safe_clipboard_get(self) -> str:
        try:
            return self.clipboard_get()
        except Exception:
            return ""

    def _browse_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.var_path.get() or None)
        if path:
            self.var_path.set(path)

    def _clear_url_input(self) -> None:
        self.txt_urls.delete("1.0", "end")

    def _on_conc_change(self, value: str) -> None:
        try:
            n = int(value)
        except ValueError:
            n = DEFAULT_CONCURRENCY
        self._queue.set_max_workers(n)
        self._log(f"并发数已设为 {n}")

    def _on_sep_toggle(self) -> None:
        if self.var_separate.get() and not self._demucs_ok:
            messagebox.showinfo(
                "人声分离尚未就绪",
                self._demucs_msg
                + "\n\n下载仍可正常进行；分离功能将在安装 Demucs 后于 v2 完善。",
            )

    def _on_clip_toggle(self) -> None:
        self._watcher.set_enabled(bool(self.var_clip.get()))

    def _on_clipboard_url(self, url: str) -> None:
        def ask() -> None:
            if self._clipboard_prompting:
                return
            current = self.txt_urls.get("1.0", "end")
            if url.strip() in current:
                return
            self._clipboard_prompting = True
            try:
                ok = messagebox.askyesno(
                    "检测到视频链接",
                    f"剪贴板中发现链接：\n\n{url}\n\n是否追加到输入框？",
                )
                if ok:
                    existing = self.txt_urls.get("1.0", "end").strip()
                    if existing:
                        self.txt_urls.insert("end", "\n" + url)
                    else:
                        self.txt_urls.insert("1.0", url)
                    self.lift()
                    self.focus_force()
            finally:
                self._clipboard_prompting = False

        self.after(0, ask)

    def _current_opts_snapshot(self) -> dict:
        res = self.var_res.get()
        out = self.var_path.get().strip()
        return {
            "output_dir": out,
            "resolution": res,
            "video_format": self.var_vfmt.get(),
            "audio_format": self.var_afmt.get(),
            "audio_only": (res == "audio"),
            "want_separate": bool(self.var_separate.get()),
        }

    def _on_add(self) -> None:
        text = self.txt_urls.get("1.0", "end")
        urls = parse_urls(text)
        if not urls:
            messagebox.showwarning("提示", "未识别到有效 http(s) 链接。")
            return
        out = self.var_path.get().strip()
        if not out:
            messagebox.showwarning("提示", "请选择保存路径。")
            return
        Path(out).mkdir(parents=True, exist_ok=True)

        snap = self._current_opts_snapshot()
        added = 0
        for url in urls:
            task = DownloadTask(url=url, **snap)
            self._queue.add(task)
            self._ensure_row(task)
            added += 1

        self._log(f"已添加 {added} 个任务（并发上限 {self._queue.max_workers}）")
        self._set_status(f"队列中 / 已添加 {added} 个")
        self._clear_url_input()

    def _on_probe(self) -> None:
        text = self.txt_urls.get("1.0", "end")
        urls = parse_urls(text)
        if not urls:
            messagebox.showwarning("提示", "请先粘贴至少一个视频链接。")
            return
        url = urls[0]

        def work() -> None:
            try:
                self._set_status("探测中…")
                result = list_formats(url, log=self._log)
                lines = [f"标题：{result.title}", f"共 {len(result.formats)} 个格式（节选）："]
                for f in result.formats[:30]:
                    size = f"{f.filesize // 1024 // 1024}MB" if f.filesize else "?"
                    lines.append(
                        f"  [{f.format_id}] {f.ext} {f.resolution} {f.note} ({size})"
                    )
                if len(result.formats) > 30:
                    lines.append(f"  … 其余 {len(result.formats) - 30} 项省略")
                self._log("\n".join(lines))
                self._set_status("探测完成")
            except Exception as e:
                self._log(f"探测失败：{e}")
                self._set_status("探测失败")
                self.after(0, lambda: messagebox.showerror("探测失败", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _ensure_row(self, task: DownloadTask) -> None:
        if task.id in self._rows:
            self._rows[task.id].refresh(task)
            return
        row = TaskRow(self.task_list, self, task)
        row.pack(fill="x", padx=4, pady=4)
        self._rows[task.id] = row

    def _on_task_update(self, task: DownloadTask) -> None:
        # 工作线程 → 主线程
        def apply() -> None:
            if task.id not in self._rows:
                # 可能已被删除
                if self._queue.get(task.id) is None:
                    return
                self._ensure_row(task)
            else:
                self._rows[task.id].refresh(task)
            # 汇总状态
            tasks = self._queue.tasks()
            n_dl = sum(1 for t in tasks if t.status == TaskStatus.DOWNLOADING)
            n_q = sum(1 for t in tasks if t.status == TaskStatus.QUEUED)
            n_ok = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)
            self._set_status(f"下载中 {n_dl} · 排队 {n_q} · 完成 {n_ok} · 共 {len(tasks)}")

        self.after(0, apply)

    def open_task_file(self, task_id: str) -> None:
        task = self._queue.get(task_id)
        if not task or not task.filepath:
            messagebox.showwarning("提示", "尚无可用文件路径。")
            return
        try:
            open_file(task.filepath)
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def open_task_dir(self, task_id: str) -> None:
        task = self._queue.get(task_id)
        if not task:
            return
        target = task.output_dir
        if task.filepath:
            p = Path(task.filepath)
            target = str(p.parent if p.exists() else (task.output_dir or p.parent))
        if not target:
            messagebox.showwarning("提示", "无保存目录。")
            return
        try:
            open_directory(target)
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def remove_task(self, task_id: str) -> None:
        task = self._queue.get(task_id)
        if task and task.status == TaskStatus.DOWNLOADING:
            if not messagebox.askyesno("确认", "任务正在下载，确定取消并删除？"):
                return
        self._queue.remove(task_id)
        row = self._rows.pop(task_id, None)
        if row:
            row.destroy()
        self._log(f"已移除任务 {task_id}")

    def _on_close(self) -> None:
        self._watcher.stop()
        self._queue.stop()
        self.destroy()
