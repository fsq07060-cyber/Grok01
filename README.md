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
| 探测全部链接（逐条成功/失败） | v1.2 |
| 可选 Cookie 文件 / 浏览器 Cookie（默认关闭） | v1.2 |
| 失效视频（HTTP 410/404）中文提示 | v1.2 |
| 人声/伴奏分离（Demucs） | v2（UI 开关 + stub） |

## 使用提示

1. 在「视频链接」多行框粘贴一个或多个 URL，点 **添加任务**。任务行会马上出现；保存目录在后台创建（慢盘或未连接的盘符不会把窗口卡成「未响应」）。某一条失败不会取消其余任务，原因写在该任务行和日志里（含完整 URL）。  
2. **探测全部链接** 在后台逐条检查输入框中的每个 URL。某一条返回 410/404 只说明那一条失效，日志和弹窗会写明是哪一个链接。  
3. 任务列表中查看进度；完成后可 **打开文件** 或 **打开目录**。  
4. **删除**：未完成会取消并移除；已完成仅从列表移除。  
5. 剪贴板监听开启时，检测到链接会询问是否追加到输入框。

## YouTube 提示登录 / 机器人

默认不读取 Cookie。若 YouTube 提示 “Sign in to confirm you’re not a bot” 或要求登录：

1. 在「浏览器 Cookie」选择 **chrome** / **edge** / **firefox**（本机已安装该浏览器），或  
2. 用浏览器扩展导出 **Netscape** 格式的 `cookies.txt`，填到「Cookie 文件」。

两条都填时会一起传给 yt-dlp（`cookiesfrombrowser` 与 `cookiefile`）。浏览器正在占用 Cookie 数据库时可能读失败，关掉浏览器或改用导出的文件即可。

Pornhub 等站点返回 **HTTP 410 / 404** 表示该视频已删除或下架，换一条还能打开的链接即可，不是下载器坏了。

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
