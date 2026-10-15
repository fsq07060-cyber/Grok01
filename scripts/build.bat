@echo off
REM Windows 本机打包。CI 请用 .github/workflows/build-windows.yml（会先下 ffmpeg 再 pyinstaller）。
REM Linux box 上执行本脚本无法产出 .exe。

setlocal EnableExtensions
cd /d "%~dp0.."

echo === StreamDownloader Windows build ===
where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] 未找到 python，请先安装 Python 3.10+ 并加入 PATH。
  exit /b 1
)

python -c "import customtkinter, yt_dlp" 2>nul
if errorlevel 1 (
  echo [INFO] 安装依赖…
  pip install -r requirements.txt pyinstaller
)

python -c "import PyInstaller" 2>nul
if errorlevel 1 (
  pip install pyinstaller
)

if not exist "ffmpeg\bin\ffmpeg.exe" (
  echo [WARN] 未找到 ffmpeg\bin\ffmpeg.exe
  echo        Actions 工作流会自动下载；本机请先放入 BtbN/gyan 静态构建。
)

echo [INFO] 运行 PyInstaller（onedir）…
pyinstaller --noconfirm build_windows.spec
if errorlevel 1 (
  echo [ERROR] PyInstaller 失败。
  exit /b 1
)

REM 双保险：复制 ffmpeg 进 dist（spec 也会 datas 打入，若存在）
if exist "ffmpeg\bin\ffmpeg.exe" (
  if not exist "dist\StreamDownloader\ffmpeg\bin" mkdir "dist\StreamDownloader\ffmpeg\bin"
  copy /Y "ffmpeg\bin\*.exe" "dist\StreamDownloader\ffmpeg\bin\" >nul
  echo [INFO] 已复制 ffmpeg 到 dist\StreamDownloader\ffmpeg\bin\
)

if not exist "dist\StreamDownloader\StreamDownloader.exe" (
  echo [ERROR] 未找到 dist\StreamDownloader\StreamDownloader.exe
  exit /b 1
)

echo.
echo 完成：dist\StreamDownloader\StreamDownloader.exe
echo 请在 Windows 资源管理器中测试打开与下载。
endlocal
