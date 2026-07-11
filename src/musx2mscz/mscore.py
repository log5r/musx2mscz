"""Locate and drive the MuseScore command line."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_CANDIDATES = [
    "/Applications/MuseScore 4.app/Contents/MacOS/mscore",
    "/Applications/MuseScore Studio 4.app/Contents/MacOS/mscore",
    "/Applications/MuseScore 3.app/Contents/MacOS/mscore",
]


def find_mscore() -> str:
    env = os.environ.get("MSCORE")
    if env and Path(env).exists():
        return env
    for c in _CANDIDATES:
        if Path(c).exists():
            return c
    for name in ("mscore", "musescore", "mscore4portable"):
        p = shutil.which(name)
        if p:
            return p
    raise FileNotFoundError(
        "MuseScore not found. Install MuseScore 4 or set the MSCORE environment variable.")


def convert(input_path: Path, output_path: Path, style_path: Path | None = None,
            timeout: int = 600) -> None:
    cmd = [find_mscore()]
    if style_path is not None:
        cmd += ["--style", str(style_path)]
    cmd += ["-o", str(output_path), str(input_path)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("DYLD_")}
    env["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "")
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env=env,
    )
    if proc.returncode != 0 or not output_path.exists():
        noise = ("qt.qml", "Warning:", "QML")
        stderr = "\n".join(l for l in proc.stderr.splitlines()
                           if l.strip() and not l.startswith(noise))
        raise RuntimeError(
            f"MuseScore conversion failed (exit {proc.returncode}).\n{stderr[-3000:]}")
