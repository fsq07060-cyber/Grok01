# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir：在 Windows（本机或 GitHub Actions windows-latest）上构建。
# 用法：pyinstaller --noconfirm build_windows.spec
#
# Linux 上无法交叉产出 Windows exe；请用 .github/workflows/build-windows.yml。

import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

block_cipher = None
ROOT = Path(SPECPATH).resolve()

# customtkinter 主题/字体/图标
datas = collect_data_files("customtkinter")
binaries = []

# 若已放入 Windows ffmpeg 静态构建，打进包内 ffmpeg/bin/
for name in ("ffmpeg.exe", "ffprobe.exe"):
    p = ROOT / "ffmpeg" / "bin" / name
    if p.is_file():
        datas.append((str(p), "ffmpeg/bin"))

hiddenimports = [
    "customtkinter",
    "darkdetect",
    "packaging",
    "packaging.version",
    "packaging.specifiers",
    "packaging.requirements",
    "yt_dlp",
    "yt_dlp_ejs",
    "curl_cffi",
    "cffi",
    "_cffi_backend",
    "certifi",
]
# yt-dlp 提取器/后处理：collect 保证 onedir 不缺模块
hiddenimports += collect_submodules("yt_dlp")
hiddenimports += collect_submodules("yt_dlp_ejs")
datas += collect_data_files("yt_dlp_ejs")
datas += collect_data_files("certifi")

# curl_cffi：--impersonate 依赖它。collect_all 带上 _wrapper，但 Windows 轮子把
# libcurl-impersonate 放在旁边的 curl_cffi.libs（delvewheel）。那个目录不是 Python 包，
# 分析跟不进去。__init__.py 用 dirname(__file__)/../curl_cffi.libs 找 DLL，
# 所以必须作为 curl_cffi 的同级目录打进 _internal。
try:
    cffi_datas, cffi_binaries, cffi_hidden = collect_all("curl_cffi")
except Exception as exc:
    raise SystemExit(
        "未安装 curl_cffi。请先 pip install -r requirements.txt，"
        "Windows 包需要浏览器指纹，用户不应再单独 pip install。"
    ) from exc
datas += cffi_datas
binaries += cffi_binaries
hiddenimports += cffi_hidden

_curl_spec = importlib.util.find_spec("curl_cffi")
if _curl_spec is None or not _curl_spec.origin:
    raise SystemExit("curl_cffi 未安装，无法打包 impersonate 支持。")
_curl_pkg = Path(_curl_spec.origin).resolve().parent
_curl_libs = _curl_pkg.parent / "curl_cffi.libs"
if _curl_libs.is_dir():
    _dlls = [p for p in _curl_libs.iterdir() if p.is_file()]
    if not _dlls:
        raise SystemExit(f"{_curl_libs} 是空的，libcurl-impersonate 没有打进去。")
    for _dll in _dlls:
        binaries.append((str(_dll), "curl_cffi.libs"))
elif sys.platform == "win32":
    raise SystemExit(
        "Windows 上缺少 curl_cffi.libs（libcurl-impersonate）。"
        "请安装带 curl-cffi extra 的 yt-dlp 后再打包。"
    )

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["scripts/pyi_rth_ffmpeg.py", "scripts/pyi_rth_curl_cffi.py"],
    excludes=[
        "demucs",
        "torch",
        "torchaudio",
        "torchvision",
        "tensorflow",
        "matplotlib",
        "numpy.testing",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StreamDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # CI/防误杀更稳；体积略大可接受
    console=False,  # GUI；调试时可改 True
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="StreamDownloader",
)
