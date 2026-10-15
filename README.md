# 流媒体下载器 + 人声/伴奏分离

Windows 桌面工具：粘贴（可批量）视频链接 → 选路径/清晰度/格式 → 用 yt-dlp 下载；可选 Demucs 分离**人声 + 背景音乐**（非乐器分轨）。

详细设计见 [TECH_PLAN.md](./TECH_PLAN.md)，打包见 [PACKAGING.md](./PACKAGING.md)。

## 快速启动（Windows）

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
rem 请确保已安装 ffmpeg 并在 PATH 中（或放入 ffmpeg\bin\）
python main.py
```

## 功能概览

| 功能 | 版本 |
|------|------|
| 批量粘贴 URL（换行/逗号分隔），每条独立任务 | v1.1 |
| 每任务独立进度条；打开文件 / 打开目录 / 删除 | v1.1 |
| 有限并发下载（默认 2） | v1.1 |
| 路径 / 分辨率 / 视频·音频格式 | v1 |
| 剪贴板 URL 检测弹窗（与批量输入共存） | v1 |
| 人声/伴奏分离（Demucs） | v2（UI 开关 + stub） |

## 使用提示

1. 在「视频链接」多行框粘贴一个或多个 URL，点 **添加任务**。  
2. 任务列表中查看进度；完成后可 **打开文件** 或 **打开目录**。  
3. **删除**：未完成会取消并移除；已完成仅从列表移除。  
4. 剪贴板监听开启时，检测到链接会询问是否追加到输入框。

## 打包（GitHub Actions）

见 [PACKAGING.md](./PACKAGING.md)。

**推荐：** 推送到 GitHub `main`，或手动跑 Actions 工作流 **Build Windows**（`.github/workflows/build-windows.yml`）。

1. `git push origin main`（或 Actions → Build Windows → Run workflow）
2. 等待 job 成功，下载 artifact **StreamDownloader-windows**
3. 解压后运行 `StreamDownloader.exe`（内含随包 ffmpeg）

本机 Windows 亦可：`scripts\build.bat`。  
**本开发环境未生成 .exe**；以 Actions artifact 为准。

## 合规提示

请仅下载你有权获取的内容，并遵守当地法律与网站服务条款。涉及 NSFW 站点时请自行确认合规性。
