# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir：在 Windows（本机或 GitHub Actions windows-latest）上构建。
# 用法：pyinstaller --noconfirm build_windows.spec
#
# Linux 上无法交叉产出 Windows exe；请用 .github/workflows/build-windows.yml。

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None
ROOT = Path(SPECPATH).resolve()

# customtkinter 主题/字体/图标
datas = collect_data_files("customtkinter")

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
]
# yt-dlp 提取器/后处理：collect 保证 onedir 不缺模块
hiddenimports += collect_submodules("yt_dlp")

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["scripts/pyi_rth_ffmpeg.py"],
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
