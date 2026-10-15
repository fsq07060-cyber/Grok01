# 流媒体下载器 + 人声/伴奏分离 — 技术方案

面向用户：**Takashi（Windows）**  
目标：粘贴主流站点视频链接（支持批量）→ 选择路径/清晰度/格式 → 下载；可选将音轨分离为**人声 + 背景音乐**（非乐器分轨）。

---

## 1. 推荐技术栈及理由

| 层级 | 选型 | 理由 |
|------|------|------|
| 语言 | Python 3.10+ | 与 yt-dlp、Demucs 生态一致，Windows 安装成本低 |
| 下载 | **yt-dlp** | 维护最活跃的开源下载器，覆盖 YouTube / Pornhub 等大量站点，支持格式探测与合并 |
| 音视频处理 | **ffmpeg**（系统依赖或随包） | yt-dlp 合并音视频、转封装所必需 |
| 人声分离 | **Demucs**（`htdemucs` 模型，两轨：vocals + no_vocals） | Meta 开源、成熟、本地可跑；输出即为人声与伴奏，**不做** drums/bass/other 乐器分轨 |
| GUI | **customtkinter** | 见下文对比 |
| 打包 | **PyInstaller onedir** | 见 [PACKAGING.md](./PACKAGING.md) |

### 为何选 customtkinter，而不是 Electron / PyQt

1. **相对 Electron**：不引入 Node + Chromium，安装包小、内存占用低。  
2. **相对 PyQt / PySide**：许可与体积更轻；基于标准库 tkinter，暗色主题开箱。  
3. 若日后需要复杂多窗体或商业签名，可再评估 PySide6。

---

## 2. 依赖列表

### Python 包（`requirements.txt`）

| 包 | 用途 | 版本建议 |
|----|------|----------|
| `customtkinter` | 桌面 UI | >=5.2 |
| `yt-dlp` | 下载与格式探测 | 最新稳定版 |
| `demucs` | 人声/伴奏分离（v2） | >=4.0（可选安装） |
| `torch` / `torchaudio` | Demucs 运行时（v2） | 按 CUDA/CPU 官方指引选轮子 |

> v1/v1.1 只装 `customtkinter` + `yt-dlp`；Demucs 相关在用户勾选「分离人声」且未安装时提示。默认安装包**不含** Demucs。

### 系统依赖（Windows）

| 依赖 | 用途 | 获取方式概要 |
|------|------|----------------|
| **ffmpeg** | 合并/转码 | 官方/静态构建放入 `ffmpeg/bin`，或 `winget install ffmpeg` |
| **Python 3.10+** | 源码运行 | python.org 或 Microsoft Store |
| **（可选）NVIDIA GPU + CUDA** | 加速 Demucs | 无 GPU 时用 CPU |

---

## 3. 文件结构

```
stream-downloader/
  TECH_PLAN.md
  PACKAGING.md           # Windows 打包方案与 blocker
  README.md
  requirements.txt
  main.py                # 入口
  build_windows.spec     # PyInstaller 草稿（onedir）
  scripts/
    build.bat            # Windows 构建脚本草稿
    pyi_rth_ffmpeg.py    # 运行时把随包 ffmpeg 加入 PATH
  ffmpeg/bin/            # 放置 ffmpeg.exe（不强制提交二进制）
  ui/
    app.py               # 主窗口：批量 URL + 任务列表
  core/
    downloader.py        # yt-dlp + progress_hooks
    task_model.py        # DownloadTask / TaskStatus
    task_queue.py        # 有限并发队列（默认 2）
    url_parse.py         # 批量 URL 解析
    platform_util.py     # 打开文件/目录（Windows/macOS/Linux）
    ffmpeg_path.py       # 随包 ffmpeg PATH
    separator.py         # Demucs stub / v2
    clipboard_watch.py   # 剪贴板 URL 轮询
```

---

## 4. v1.1 架构要点

### 任务模型 `DownloadTask`

字段：`id, url, status, progress(0–100), filepath, output_dir, error, title`，以及分辨率/格式快照。

状态：`queued → downloading → completed | failed | cancelled`。

### 任务队列 `DownloadQueue`

- 默认 `max_workers=2`（UI 可调 1–4）。  
- 每任务独立 `threading.Event` 取消。  
- `on_update` 在工作线程触发 → UI 用 `after(0, …)` 刷新进度条（线程安全）。

### 下载器进度

`core/downloader.py` 的 `progress_hooks` 解析 `_percent_str` / `downloaded_bytes`，通过 `on_progress(pct, msg, filepath)` 回传；下载结束尽量解析最终文件路径供「打开文件」。

### UI

- 多行文本框批量粘贴 URL（换行/逗号/分号）。  
- `CTkScrollableFrame` 任务行：进度条 + 打开文件 / 打开目录 / 删除。  
- 剪贴板监听与批量输入共存（弹窗询问是否追加到输入框）。

### 打开文件/目录

`core/platform_util.py`：Windows `os.startfile`；macOS `open`；Linux `xdg-open`。打包到 Windows 后生效。

---

## 5. 本地安装步骤概要

1. 安装 Python 3.10+，勾选「Add to PATH」。  
2. 安装 ffmpeg，或放入项目 `ffmpeg/bin`。  
3. ```bat
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   python main.py
   ```  
4. （v2）`pip install demucs` 及匹配 torch。

---

## 6. 版本范围

### v1 / v1.1（当前）

- 批量链接 → 独立任务、独立进度  
- 打开文件 / 打开目录 / 删除（未完成取消+移除）  
- 有限并发、路径/分辨率/格式、剪贴板监听  
- 「分离人声」开关（stub）  
- 打包文档与脚本草稿（**本 Linux box 不产出 .exe**）

### v2（后续）

- 下载完成后自动对接 Demucs `--two-stems=vocals`  
- GPU/CPU 检测与耗时提示  
- 字幕/封面可选等

**明确不做：** 乐器分轨（drums / bass / other 单独导出）。

---

## 7. 已知限制

1. **站点风控**：YouTube 等可能限速、要 cookies；需定期升级 yt-dlp。  
2. **合规**：用户须自行确保有权下载与遵守法律/ToS；工具不提供破解会员墙方案。  
3. **Demucs**：默认不打包；CPU 上长视频可能很慢。  
4. **格式合并**：依赖 ffmpeg。  
5. **剪贴板**：轮询非全局钩子，可能漏检极短粘贴。  
6. **取消时机**：yt-dlp 在 hook/检查点响应取消，未必瞬时中断网络。  
7. **打包**：Linux 开发环境无法交叉生成 Windows exe，见 PACKAGING.md。

---

## 8. 合规与使用声明

本软件仅供个人合法备份与学习研究。下载受版权保护的内容、传播 NSFW 内容须符合适用法律。开发者不承担用户滥用责任。
