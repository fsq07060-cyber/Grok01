# -*- coding: utf-8 -*-
"""customtkinter 主窗口：批量 URL、任务列表、独立进度、打开/删除。"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable, Dict, Optional

import customtkinter as ctk

from core import separator as sep
from core.clipboard_watch import ClipboardWatcher
from core.downloader import list_formats
from core.errors import format_probe_failure_dialog, friendly_error, run_probe_batch
from core.platform_util import open_directory, open_file
from core.task_model import DownloadTask, TaskStatus
from core.task_queue import DownloadQueue
from core.url_parse import parse_urls


RESOLUTIONS = ["best", "2160", "1440", "1080", "720", "480", "audio"]
VIDEO_FORMATS = ["mp4", "webm", "mkv", "best"]
AUDIO_FORMATS = ["m4a", "mp3", "opus", "best"]
BROWSER_MENU = ["不使用", "chrome", "edge", "firefox"]
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

        self.lbl_error = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            justify="left",
            wraplength=760,
            text_color=("#b00020", "#ff8a80"),
        )
        self.lbl_error.grid(row=2, column=0, columnspan=3, sticky="ew", padx=8, pady=(0, 6))
        self.lbl_error.grid_remove()

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
        if task.status == TaskStatus.FAILED:
            self.lbl_status.configure(text_color=("#b00020", "#ff8a80"))
        else:
            self.lbl_status.configure(text_color=("gray10", "#DCE4EE"))
        if task.status == TaskStatus.FAILED and task.error:
            self.lbl_error.configure(text=task.error)
            self.lbl_error.grid()
        else:
            self.lbl_error.grid_remove()

    def _open_file(self) -> None:
        self.app.open_task_file(self.task_id)

    def _open_dir(self) -> None:
        self.app.open_task_dir(self.task_id)

    def _delete(self) -> None:
        self.app.remove_task(self.task_id)


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("流媒体下载器 v1.3.1 · 人声/伴奏分离")
        self.geometry("920x800")
        self.minsize(760, 640)

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self._clipboard_prompting = False
        self._probing = False
        self._closing = False
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._clip_ticks = 0
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
        # 在主线程轮询。后台线程里 clipboard_get / after() 会和 Tk 抢锁，窗口会「未响应」。
        self._watcher.enabled = bool(self.var_clip.get())

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(30, self._drain_ui)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(7, weight=1)
        self.grid_rowconfigure(8, weight=1)

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
        self.btn_probe = ctk.CTkButton(
            btn_row, text="探测全部链接", width=140, command=self._on_probe
        )
        self.btn_probe.pack(side="left", padx=4)
        ctk.CTkButton(
            btn_row,
            text="清空输入",
            width=100,
            fg_color="gray40",
            command=self._clear_url_input,
        ).pack(side="left", padx=4)
        ctk.CTkLabel(
            frm_url,
            text="「探测全部链接」会逐条检查输入框里的每个 URL（后台进行，不卡住窗口）。某一条失败不会让后面的链接被跳过。",
            text_color="gray60",
            anchor="w",
            justify="left",
            wraplength=840,
        ).grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=(0, 8))

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

        # Cookie（默认关闭；YouTube 提示 bot/登录时再填）
        frm_ck = ctk.CTkFrame(self)
        frm_ck.grid(row=3, column=0, sticky="ew", **pad)
        frm_ck.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(frm_ck, text="Cookie 文件").grid(row=0, column=0, padx=8, pady=(8, 4))
        self.var_cookie_file = tk.StringVar(value="")
        ctk.CTkEntry(
            frm_ck,
            textvariable=self.var_cookie_file,
            placeholder_text="可选，Netscape 格式 cookies.txt",
        ).grid(row=0, column=1, sticky="ew", padx=8, pady=(8, 4))
        ctk.CTkButton(frm_ck, text="浏览…", width=100, command=self._browse_cookie).grid(
            row=0, column=2, padx=8, pady=(8, 4)
        )
        ctk.CTkLabel(frm_ck, text="浏览器 Cookie").grid(row=1, column=0, padx=8, pady=(4, 8))
        self.var_browser = tk.StringVar(value="不使用")
        ctk.CTkOptionMenu(frm_ck, variable=self.var_browser, values=BROWSER_MENU, width=140).grid(
            row=1, column=1, padx=8, pady=(4, 4), sticky="w"
        )
        ctk.CTkLabel(
            frm_ck,
            text="默认不用 Cookie。YouTube 会自动换客户端；年龄限制或地区限制仍失败时，再选浏览器或 Netscape Cookie 文件。",
            text_color="gray60",
            anchor="w",
            justify="left",
            wraplength=860,
        ).grid(row=2, column=0, columnspan=3, sticky="ew", padx=8, pady=(0, 8))

        # 开关行
        frm_sw = ctk.CTkFrame(self)
        frm_sw.grid(row=4, column=0, sticky="ew", **pad)

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
        frm_stat.grid(row=5, column=0, sticky="ew", **pad)
        self.lbl_status = ctk.CTkLabel(frm_stat, text="就绪", anchor="w")
        self.lbl_status.pack(side="left", padx=12, pady=8, fill="x", expand=True)

        tip = (
            "合规提示：请仅下载你有权获取的内容；使用 NSFW 站点时请遵守当地法律与网站条款。"
        )
        ctk.CTkLabel(self, text=tip, text_color="gray60", wraplength=860, justify="left").grid(
            row=6, column=0, sticky="ew", padx=12, pady=(0, 4)
        )

        # 任务列表（row 7）+ 日志（row 8）
        self.task_list = ctk.CTkScrollableFrame(self, height=180, label_text="下载任务")
        self.task_list.grid(row=7, column=0, sticky="nsew", padx=12, pady=(4, 6))
        self.task_list.grid_columnconfigure(0, weight=1)

        # 日志
        self.log_box = ctk.CTkTextbox(self, height=120)
        self.log_box.grid(row=8, column=0, sticky="nsew", padx=12, pady=(4, 12))
        self._log("欢迎使用流媒体下载器。可一次粘贴多个链接后点击「添加任务」（每条单独入队，失败互不影响）。")
        self._log("「探测全部链接」会逐条检查，失败的 URL 会写进日志；不会只测第一条。")
        self._log(self._demucs_msg)
        self._log(f"默认并发：{DEFAULT_CONCURRENCY}（可在上方调整）")

    # ------------------------------------------------------------------ helpers
    def _ui(self, fn: Callable[[], None]) -> None:
        """只在 Tk 主线程碰控件。工作线程只入队。"""
        if self._closing:
            return
        if threading.current_thread() is threading.main_thread():
            fn()
            return
        self._ui_queue.put(fn)

    def _drain_ui(self) -> None:
        if self._closing:
            return
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self._clip_ticks += 1
        if self._clip_ticks >= 33:
            self._clip_ticks = 0
            try:
                self._watcher.poll()
            except Exception:
                pass
        try:
            self.after(30, self._drain_ui)
        except Exception:
            return

    def _log(self, msg: str) -> None:
        def append() -> None:
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")

        self._ui(append)

    def _set_status(self, text: str) -> None:
        def upd() -> None:
            self.lbl_status.configure(text=text)

        self._ui(upd)

    def _safe_clipboard_get(self) -> str:
        try:
            return self.clipboard_get()
        except Exception:
            return ""

    def _browse_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.var_path.get() or None)
        if path:
            self.var_path.set(path)

    def _browse_cookie(self) -> None:
        current = self.var_cookie_file.get().strip()
        initial = str(Path(current).parent) if current else str(Path.home())
        path = filedialog.askopenfilename(
            title="选择 Netscape Cookie 文件",
            initialdir=initial if Path(initial).is_dir() else str(Path.home()),
            filetypes=[("Cookie 文本", "*.txt"), ("所有文件", "*.*")],
        )
        if path:
            self.var_cookie_file.set(path)

    def _cookie_settings(self) -> tuple[str, str]:
        browser = self.var_browser.get().strip()
        if browser in ("不使用", "none", "无"):
            browser = ""
        return self.var_cookie_file.get().strip(), browser

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
        self._watcher.enabled = bool(self.var_clip.get())

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

        self._ui(ask)

    def _current_opts_snapshot(self) -> dict:
        res = self.var_res.get()
        out = self.var_path.get().strip()
        cookiefile, browser = self._cookie_settings()
        return {
            "output_dir": out,
            "resolution": res,
            "video_format": self.var_vfmt.get(),
            "audio_format": self.var_afmt.get(),
            "audio_only": (res == "audio"),
            "want_separate": bool(self.var_separate.get()),
            "cookiefile": cookiefile,
            "cookiesfrombrowser": browser,
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
        # 不在主线程 mkdir。F: 这类未就绪盘符会卡住 CreateDirectory，
        # 窗口变成「未响应」，而且任务行还没创建。目录改由下载线程准备。
        snap = self._current_opts_snapshot()
        added = 0
        for url in urls:
            task = DownloadTask(url=url, **snap)
            self._queue.add(task)
            self._ensure_row(task)
            added += 1

        cookie_note = []
        if snap.get("cookiefile"):
            cookie_note.append("Cookie 文件")
        if snap.get("cookiesfrombrowser"):
            cookie_note.append(f"浏览器 {snap['cookiesfrombrowser']}")
        extra = f"（{'、'.join(cookie_note)}）" if cookie_note else ""
        self._log(
            f"已添加 {added} 个任务（并发上限 {self._queue.max_workers}）{extra}。"
            "保存目录在后台创建，不会卡住窗口。"
        )
        self._set_status(f"队列中 / 已添加 {added} 个")
        self._clear_url_input()
        self.update_idletasks()

    def _on_probe(self) -> None:
        if self._probing:
            return
        text = self.txt_urls.get("1.0", "end")
        urls = parse_urls(text)
        if not urls:
            messagebox.showwarning("提示", "请先粘贴至少一个视频链接。")
            return
        # Cookie 文件是否存在放到探测线程里检查，避免主线程踩到慢盘。
        cookiefile, browser = self._cookie_settings()

        self._probing = True
        self.btn_probe.configure(state="disabled", text="探测中…")
        format_limit = 30 if len(urls) == 1 else 8

        def work() -> None:
            try:
                self._set_status(f"探测中 0/{len(urls)}…")
                self._log(f"开始逐条探测 {len(urls)} 个链接（某一条失败不会跳过其余链接）")

                def probe_one(url: str) -> str:
                    result = list_formats(
                        url,
                        log=self._log,
                        cookiefile=cookiefile,
                        cookiesfrombrowser=browser,
                    )
                    lines = [
                        f"标题：{result.title}",
                        f"共 {len(result.formats)} 个格式（节选）：",
                    ]
                    for fmt in result.formats[:format_limit]:
                        size = f"{fmt.filesize // 1024 // 1024}MB" if fmt.filesize else "?"
                        lines.append(
                            f"  [{fmt.format_id}] {fmt.ext} {fmt.resolution} {fmt.note} ({size})"
                        )
                    if len(result.formats) > format_limit:
                        lines.append(f"  … 其余 {len(result.formats) - format_limit} 项省略")
                    self._log("\n".join(lines))
                    return f"{result.title}（{len(result.formats)} 个格式）"

                batch = run_probe_batch(urls, probe_one, self._log)
                summary = (
                    f"探测结束：成功 {len(batch.successes)}，失败 {len(batch.failures)}，"
                    f"未探测 {len(batch.untested)}，共 {len(urls)}"
                )
                self._log(summary)
                self._set_status(summary)
                dialog = format_probe_failure_dialog(batch)
                if dialog:
                    self._ui(lambda m=dialog: messagebox.showerror("探测失败", m))
            except Exception as e:
                reason = friendly_error(e)
                self._log(f"探测中断：{reason}")
                self._set_status("探测失败")
                self._ui(
                    lambda m=reason: messagebox.showerror(
                        "探测失败",
                        m + "\n\n若输入框里还有其它链接，请看日志确认哪些还没测。",
                    )
                )
            finally:
                def enable() -> None:
                    self._probing = False
                    if self.btn_probe.winfo_exists():
                        self.btn_probe.configure(state="normal", text="探测全部链接")

                self._ui(enable)

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
            n_fail = sum(1 for t in tasks if t.status == TaskStatus.FAILED)
            self._set_status(
                f"下载中 {n_dl} · 排队 {n_q} · 完成 {n_ok} · 失败 {n_fail} · 共 {len(tasks)}"
            )

        self._ui(apply)

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
        self._closing = True
        self._watcher.stop()
        self._queue.stop()
        self.destroy()
