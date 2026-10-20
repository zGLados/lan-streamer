"""Where lan-streamer finds its bundled tools and keeps its data."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def app_root() -> Path | None:
    """The install directory of the Windows installer (<app>\\python\\python.exe), or None."""
    root = Path(sys.executable).resolve().parent.parent
    return root if (root / "mediamtx").is_dir() else None


def mediamtx() -> Path | None:
    """The MediaMTX executable: bundled, $LANSTREAMER_MEDIAMTX, a repo checkout's .deps or PATH."""
    name = "mediamtx.exe" if sys.platform == "win32" else "mediamtx"
    candidates = []
    if root := app_root():
        candidates.append(root / "mediamtx" / name)
    if env := os.environ.get("LANSTREAMER_MEDIAMTX"):
        candidates.append(Path(env))
    candidates.append(Path(__file__).resolve().parents[2] / ".deps" / "mediamtx" / name)
    for path in candidates:
        if path.is_file():
            return path
    found = shutil.which(name)
    return Path(found) if found else None


def data_dir() -> Path:
    """Per-user data: logs, GStreamer registry, later the database."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    path = base / "lan-streamer"
    path.mkdir(parents=True, exist_ok=True)
    return path


def plugins_dir() -> Path:
    """Uploaded plugins, one folder each (plugin.toml + code). Instance data lives in plugin-data/."""
    return data_dir() / "plugins"
