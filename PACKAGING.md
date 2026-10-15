# Windows 打包方案（PyInstaller）

目标：为 Windows 用户产出可双击运行的便携目录（onedir）。  
**推荐路径：GitHub Actions `windows-latest`（不依赖用户本机）。**

---

## 1. 推荐形态：onedir

| 方案 | 说明 | 建议 |
|------|------|------|
| **onedir** | `dist/StreamDownloader/` 多文件，含 `_internal`、`ffmpeg/bin` | **推荐** |
| onefile | 单 `.exe` | 冷启动慢；ffmpeg 难捆绑 |

入口：`main.py`。规格：`build_windows.spec`。本机脚本：`scripts/build.bat`。

---

## 2. GitHub Actions（首选，已就绪）

工作流文件：`.github/workflows/build-windows.yml`

**触发方式：**

1. **push 到 `main`**：推送后自动跑 Windows 打包；
2. **workflow_dispatch**：在 GitHub → Actions → **Build Windows** → **Run workflow** 手动触发。

**流水线步骤摘要：**

1. checkout + setup Python 3.12  
2. `pip install -r requirements.txt pyinstaller`  
3. 从 [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases) 下载 `ffmpeg-master-latest-win64-gpl.zip`，仅解出 `ffmpeg.exe` / `ffprobe.exe` 到 `ffmpeg/bin/`  
4. `pyinstaller --noconfirm build_windows.spec`  
5. 上传 artifact：`StreamDownloader-windows`（整个 `dist/StreamDownloader/`），**retention 14 天**

**推仓并跑通（需你有 GitHub 仓库写权限）：**

```bash
# 在已关联 remote 的仓库根目录
git add .github/workflows/build-windows.yml build_windows.spec scripts/build.bat PACKAGING.md README.md
git commit -m "ci: Windows PyInstaller onedir via Actions"
git push origin main
```

然后打开仓库 **Actions** 页，等待 **Build Windows** 成功，下载 artifact **StreamDownloader-windows**，解压后运行 `StreamDownloader.exe`。

> 本 box **未创建**真实 GitHub 仓库；工作流文件已写好，推上去即可用。  
> **尚未在此环境生成 .exe**（需 Actions 或 Windows 本机跑通后才有）。

---

## 3. ffmpeg 随包策略

yt-dlp 合并音视频依赖 ffmpeg。

- Actions 自动下载 BtbN Windows 静态构建并放入 `ffmpeg/bin/`，再由 spec / 构建步骤打进 `dist/StreamDownloader/ffmpeg/bin/`。  
- 运行时：`scripts/pyi_rth_ffmpeg.py` 与 `core/ffmpeg_path.py` 会把该目录加入 `PATH`。  
- 仓库内 `ffmpeg/bin/` 仅占位（不提交大二进制）。

---

## 4. Demucs / torch：可选组件

- 默认包 **不含** Demucs / torch（体积极大）。  
- UI 保留「分离人声」开关；未安装时提示用户另行 `pip install demucs` 等。

---

## 5. Windows 本机打包（可选）

```bat
cd stream-downloader
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt pyinstaller
rem 将 ffmpeg.exe / ffprobe.exe 放入 ffmpeg\bin\
scripts\build.bat
```

产物：`dist\StreamDownloader\StreamDownloader.exe`。

---

## 6. Linux box / Wine

按产品选择 **只走 GitHub Actions**，本环境 **不再投入 Wine 交叉打包**。  
简要结论：Linux 上直接 `pyinstaller` 不能可靠产出 Windows `.exe`；Wine + Windows Python 理论可行但维护成本高，已降级为非目标路径。

便携目录「仅下载 Windows 二进制组装 zip、不执行」：customtkinter / tk 依赖原生 Tcl/Tk DLL 与解释器，无法用纯 Linux 的 `pip download win_amd64` 可靠拼出可运行 GUI，故 **放弃该备用方案**。

---

## 7. 诚实记录

- 功能应先在源码态跑稳，再谈打包。  
- **安装包 / .exe：尚未在本 box 生成**；以 Actions artifact 为准。  
- 勿在未于 Windows（CI 或本机）实际跑通前声称已有 `.exe`。
