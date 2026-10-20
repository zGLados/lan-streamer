"""Loads GStreamer through PyGObject on Windows and Linux."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType

_WINDOWS_DEFAULT_ROOTS = (
    # Per-user install (the default of the official installer and winget)
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/gstreamer/1.0/msvc_x86_64",
    Path(r"C:\Program Files\gstreamer\1.0\msvc_x86_64"),
    Path(r"C:\gstreamer\1.0\msvc_x86_64"),
)

_gst: ModuleType | None = None


def windows_root() -> Path | None:
    """Returns the GStreamer install directory on Windows, or None."""
    env = os.environ.get("GSTREAMER_1_0_ROOT_MSVC_X86_64")
    for root in ([Path(env)] if env else []) + list(_WINDOWS_DEFAULT_ROOTS):
        if (root / "bin" / "gst-launch-1.0.exe").exists():
            return root
    return None


def init() -> ModuleType:
    """Initialises GStreamer once and returns the ``Gst`` module."""
    global _gst
    if _gst is not None:
        return _gst

    if sys.platform == "win32":
        root = windows_root()
        if root is None:
            raise RuntimeError("GStreamer not found. Run scripts\\setup-windows.ps1.")
        # Python 3.8+ no longer uses PATH to find DLLs of extension modules.
        # PyGObject itself comes from lan-streamer's own wheel: the bindings
        # inside GStreamer's install are built for Python 3.9 only.
        os.add_dll_directory(str(root / "bin"))

    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    # Without gst-python's overrides (not available for our PyGObject build)
    # Gst.init needs a list, not None.
    Gst.init([])
    _gst = Gst
    return Gst
