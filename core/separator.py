# -*- coding: utf-8 -*-
"""
Demucs 人声/伴奏分离封装（v2）。

v1：提供可用性检测与 stub；真正分离在安装 demucs 后启用。
仅做两轨：vocals（人声）+ no_vocals（背景音乐），不做乐器分轨。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional


ProgressCallback = Callable[[str], None]


def is_demucs_available() -> bool:
    """检测 demucs 命令或 Python 模块是否可用。"""
    if shutil.which("demucs"):
        return True
    try:
        import demucs  # noqa: F401

        return True
    except ImportError:
        return False


def check_status() -> tuple[bool, str]:
    """返回 (可用?, 说明文案)。"""
    if is_demucs_available():
        return True, "Demucs 已就绪（两轨：人声 + 背景）"
    return (
        False,
        "未安装 Demucs。v2 请执行：pip install demucs（并按需安装 torch）",
    )


def separate_vocals(
    audio_path: str | Path,
    output_dir: str | Path,
    model: str = "htdemucs",
    log: Optional[ProgressCallback] = None,
) -> tuple[Path, Path]:
    """
    将音轨分离为人声与背景音乐。

    Returns:
        (vocals_path, no_vocals_path)

    Raises:
        RuntimeError: 未安装 Demucs 或分离失败
    """
    log = log or (lambda _m: None)
    audio_path = Path(audio_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ok, msg = check_status()
    if not ok:
        raise RuntimeError(msg)

    if not audio_path.is_file():
        raise FileNotFoundError(f"找不到音频文件：{audio_path}")

    log(f"开始人声分离（模型 {model}，两轨 vocals / no_vocals）…")
    log("提示：无 GPU 时可能较慢，请耐心等待。")

    # 使用 CLI，避免直接 import torch 的复杂路径；与官方用法一致
    cmd = [
        sys.executable,
        "-m",
        "demucs",
        "--two-stems=vocals",
        "-n",
        model,
        "-o",
        str(output_dir),
        str(audio_path),
    ]
    log("命令：" + " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        raise RuntimeError(f"无法启动 Demucs：{e}") from e

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise RuntimeError(f"Demucs 失败（码 {proc.returncode}）：\n{err}")

    # 默认输出：<output_dir>/<model>/<track_name>/vocals.wav 与 no_vocals.wav
    stem = audio_path.stem
    base = output_dir / model / stem
    vocals = base / "vocals.wav"
    no_vocals = base / "no_vocals.wav"

    if not vocals.is_file() or not no_vocals.is_file():
        # 容错：搜索最近生成的文件
        found_v = list(output_dir.rglob("vocals.wav"))
        found_n = list(output_dir.rglob("no_vocals.wav"))
        if found_v and found_n:
            vocals, no_vocals = found_v[-1], found_n[-1]
        else:
            raise RuntimeError(
                f"Demucs 已结束但未找到输出文件，请检查目录：{output_dir}"
            )

    log(f"人声：{vocals}")
    log(f"背景：{no_vocals}")
    return vocals, no_vocals


def separate_vocals_stub(
    audio_path: str | Path,
    output_dir: str | Path,
    log: Optional[ProgressCallback] = None,
) -> None:
    """v1 占位：明确告知未实现完整流程。"""
    log = log or (lambda _m: None)
    ok, msg = check_status()
    log(msg)
    if not ok:
        log("人声分离将在 v2 完善；当前仅保留 UI 开关与接口占位。")
        raise RuntimeError(msg)
    # 若已安装，可直接调用真实实现
    separate_vocals(audio_path, output_dir, log=log)
