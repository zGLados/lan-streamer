"""Loads GStreamer through PyGObject on Windows and Linux."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType

_WINDOWS_SYSTEM_ROOTS = (
    # Per-user install (the default of the official installer and winget)
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/gstreamer/1.0/msvc_x86_64",
    Path(r"C:\Program Files\gstreamer\1.0\msvc_x86_64"),
    Path(r"C:\gstreamer\1.0\msvc_x86_64"),
)

_gst: ModuleType | None = None


def bundled_root() -> Path | None:
    """GStreamer shipped by the Windows installer: <app>\\gstreamer next to <app>\\python."""
    root = Path(sys.executable).resolve().parent.parent / "gstreamer"
    return root if (root / "bin").is_dir() else None


def windows_root() -> Path | None:
    """Returns a system-wide GStreamer installation on Windows, or None."""
    env = os.environ.get("GSTREAMER_1_0_ROOT_MSVC_X86_64")
    for root in ([Path(env)] if env else []) + list(_WINDOWS_SYSTEM_ROOTS):
        if (root / "bin" / "gst-launch-1.0.exe").exists():
            return root
    return None


def root() -> Path | None:
    """The GStreamer directory in use on Windows (None on Linux or if not found)."""
    if sys.platform != "win32":
        return None
    return bundled_root() or windows_root()


def _use_windows_root(root: Path, bundled: bool) -> None:
    bin_dir = str(root / "bin")
    # Python 3.8+ no longer uses PATH to find DLLs of extension modules, but
    # GStreamer's plugins and its plugin scanner still do.
    os.add_dll_directory(bin_dir)
    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    if bundled:
        # Keep the bundled GStreamer apart from any other installation.
        data = Path(os.environ.get("LOCALAPPDATA", str(root))) / "lan-streamer"
        data.mkdir(parents=True, exist_ok=True)
        os.environ["GST_REGISTRY_1_0"] = str(data / "gst-registry.bin")
        os.environ["GST_PLUGIN_SYSTEM_PATH_1_0"] = str(root / "lib" / "gstreamer-1.0")
        os.environ["GST_PLUGIN_SCANNER_1_0"] = str(
            root / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner.exe"
        )
        os.environ["GI_TYPELIB_PATH"] = str(root / "lib" / "girepository-1.0")


def init() -> ModuleType:
    """Initialises GStreamer once and returns the ``Gst`` module."""
    global _gst
    if _gst is not None:
        return _gst

    if sys.platform == "win32":
        bundled = bundled_root()
        win_root = bundled or windows_root()
        if win_root is None:
            raise RuntimeError("GStreamer not found. Reinstall lan-streamer.")
        # PyGObject itself comes from lan-streamer's own wheel: the bindings
        # inside GStreamer's install are built for Python 3.9 only.
        _use_windows_root(win_root, bundled is not None)

    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    # Without gst-python's overrides (not available for our PyGObject build)
    # Gst.init needs a list, not None.
    Gst.init([])
    _gst = Gst
    return Gst
