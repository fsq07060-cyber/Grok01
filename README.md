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
| YouTube 默认不读 Cookie，403 / 登录确认时自动换客户端 | v1.3 |
| 默认下载合并画面和声音；mp4 优先 aac/m4a；无音轨则重试并失败 | v1.3.1 |
| Cloudflare / HTTP 403 先用浏览器指纹重试；Cookie 仍默认关闭 | v1.3.2 |
| 未声明编码的合体视频也会下载；只有 ffprobe 确认是纯画面才失败 | v1.3.3 |
| 人声/伴奏分离（Demucs） | v2（UI 开关 + stub） |

## 使用提示

1. 在「视频链接」多行框粘贴一个或多个 URL，点 **添加任务**。任务行会马上出现；保存目录在后台创建（慢盘或未连接的盘符不会把窗口卡成「未响应」）。某一条失败不会取消其余任务，原因写在该任务行和日志里（含完整 URL）。  
2. **探测全部链接** 在后台逐条检查输入框中的每个 URL。某一条返回 410/404 只说明那一条失效，日志和弹窗会写明是哪一个链接。  
3. 任务列表中查看进度；完成后可 **打开文件** 或 **打开目录**。  
4. **删除**：未完成会取消并移除；已完成仅从列表移除。  
5. 剪贴板监听开启时，检测到链接会询问是否追加到输入框。
6. 默认视频下载优先合并独立的画面和声音（mp4 优先 aac/m4a，便于直接封装）。有的站点只给一条已经含音轨的视频，提取器却不写编码；这种合体流也会下载，并按地址里的 `1080p` 这类清晰度挑选。下载后用 ffprobe 检查。只有确认是纯画面才会换格式重试一次，仍然没有音轨才失败。YouTube 上能分开的音视频仍会合并，不会保存无声 AV1。窗口标题含版本号，v1.3.3 起放宽了这项检查。

当前 Windows 包：<https://github.com/fsq07060-cyber/Grok01/releases/download/v1.3.3/StreamDownloader-windows-v1.3.3.zip>
7. 默认不读取浏览器 Cookie。遇到 HTTP 403 或 Cloudflare 验证时，会先用浏览器指纹再试一次。年龄限制、较重的 Cloudflare 验证或 DRM 仍可能失败，不能保证每个网站都能下载。仍失败时，再选浏览器 Cookie 或 Netscape Cookie 文件。窗口标题含 v1.3.2 起才有这项自动重试。

## YouTube

普通公开视频默认不读 Cookie。程序会按 yt-dlp 2026.10 的推荐客户端依次尝试 `tv_samsung`、`visionos`、`web`，失败再试 `visionos`、`web_embedded`、`tv_downgraded`（不使用已删除的 `tv_embedded`，也不使用没有 PO Token 就会 403 的 `android` / `ios` / `mweb`）。HTTP 403、「Sign in to confirm」或 Cookie 读失败会自动换下一个客户端。本机有 Deno 或 Node 时，`tv_samsung` / `web` 的清晰度更完整；没有时仍会用 `visionos`（不需要 JS）。

年龄限制或部分地区封锁的视频仍可能需要 Cookie，不能保证每条都成功。YouTube 客户端都因 HTTP 403 失败时，会再用浏览器指纹试一次，然后才提示 Cookie。最后才需要：在「浏览器 Cookie」选 **chrome** / **edge** / **firefox**，或填写 Netscape 格式的 `cookies.txt`。两条都填时会一起传给 yt-dlp。默认选项是「不使用」。浏览器正在占用 Cookie 数据库时可能读失败，关掉浏览器或改用导出的文件即可；读失败后仍会再试无 Cookie 客户端。

## 其它站点（Cloudflare）

普通请求失败且返回 HTTP 403 或 Cloudflare 验证时，程序会自动加上 yt-dlp 的 `--impersonate chrome`，以及 `generic:impersonate=chrome`。Windows 安装包里已经带了 `curl_cffi` 和 `libcurl-impersonate`，不用再执行 `pip install`。

这一步解决的是「站点按 TLS 指纹把脚本拦在门外」这一类问题。年龄门槛、要人工点选的 Cloudflare、DRM 仍然可能失败。界面上的说明也是这个口径，不是「所有网站都能下」。

Pornhub 等站点返回 **HTTP 410 / 404** 表示该视频已删除或下架，换一条还能打开的链接即可，不是下载器坏了。不会因此去改浏览器指纹。

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
