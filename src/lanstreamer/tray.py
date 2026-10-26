"""Tray icon (Windows notification area) while lan-streamer runs without a console."""

from __future__ import annotations

import logging
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from importlib import resources
from pathlib import Path

log = logging.getLogger("lanstreamer")

APP_ID = "zGLados.lan-streamer"  # same AppUserModelID as the start menu shortcuts
RUNNING_MUTEX = "lan-streamer-running"  # the installer waits for it to go (lan-streamer.iss)
_mutexes: list = []


def set_app_id() -> None:
    """Groups our windows and notifications under the lan-streamer shortcut and icon."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


def hold_running_mutex() -> None:
    """A named mutex while this process runs; Windows removes it when the process ends.

    An update's installer waits until it is gone, so no file is in use any more.
    """
    if sys.platform == "win32" and not _mutexes:
        import ctypes

        # the session's one, and a Global\ one the update task (SYSTEM) can see
        for name in (RUNNING_MUTEX, f"Global\\{RUNNING_MUTEX}"):
            _mutexes.append(ctypes.windll.kernel32.CreateMutexW(None, False, name))


def message_box(title: str, text: str) -> None:
    """Shows an error when there is no console to print it to."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR


class Tray:
    def __init__(self, url: str, logs: Path, status: Callable[[], str], on_quit: Callable[[], None]):
        self.url = url
        self.logs = logs
        self.status = status
        self.on_quit = on_quit
        self.icon = None

    def start(self) -> bool:
        """Shows the icon. Returns False if no tray is available (the app runs on)."""
        try:
            import pystray
            from PIL import Image
        except ImportError as exc:
            log.warning("No tray icon: %s", exc)
            return False

        image = Image.open(resources.files("lanstreamer.assets").joinpath("icon.png"))
        menu = pystray.Menu(
            pystray.MenuItem("Open player", lambda: webbrowser.open(self.url), default=True),
            pystray.MenuItem(lambda _item: self.status(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open log", self._open_log),
            pystray.MenuItem("Open log folder", self._open_log_folder),
            pystray.MenuItem("Self-test (encoders, GStreamer)", self._self_test),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit lan-streamer", self._quit),
        )
        try:
            self.icon = pystray.Icon("lan-streamer", image, "lan-streamer", menu)
            self.icon.run_detached()
        except Exception as exc:  # noqa: BLE001 – e.g. no desktop session
            log.warning("No tray icon: %s", exc)
            return False
        return True

    def refresh(self) -> None:
        if self.icon is not None:
            self.icon.title = f"lan-streamer – {self.status()}"[:127]
            self.icon.update_menu()

    def stop(self) -> None:
        if self.icon is not None:
            self.icon.stop()

    def _quit(self) -> None:
        self.on_quit()

    # Explicit programs instead of os.startfile(): opening the folder through the
    # shell's file associations did nothing from the tray thread on the test PC.
    def _open_log(self) -> None:
        self._run(["notepad.exe", str(self.logs / "lan-streamer.log")])

    def _open_log_folder(self) -> None:
        self._run(["explorer.exe", str(self.logs)])

    def _self_test(self) -> None:
        """The doctor in its own console window (python.exe next to our pythonw.exe)."""
        python = Path(sys.executable).with_name("python.exe")
        self._run([str(python), "-m", "lanstreamer.doctor", "--pause"],
                  creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))  # fmt: skip

    def _run(self, cmd: list[str], creationflags: int = 0) -> None:
        try:
            subprocess.Popen(cmd, creationflags=creationflags)
        except OSError as exc:
            message_box("lan-streamer", f"Could not open {cmd[-1]}:\n{exc}")
