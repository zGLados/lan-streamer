"""Tray icon (Windows notification area) while lan-streamer runs without a console."""

from __future__ import annotations

import logging
import os
import sys
import webbrowser
from collections.abc import Callable
from importlib import resources
from pathlib import Path

log = logging.getLogger("lanstreamer")

APP_ID = "zGLados.lan-streamer"  # same AppUserModelID as the start menu shortcuts


def set_app_id() -> None:
    """Groups our windows and notifications under the lan-streamer shortcut and icon."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


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
            pystray.MenuItem("Open log folder", lambda: os.startfile(self.logs)),
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
