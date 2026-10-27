"""The page lan-streamer opens on its own PC when it starts (admin page → "Page on this PC").

- ``player``: the player page in the default browser (as before)
- ``fullscreen``: only the stream (/tv) in full screen with sound – Microsoft Edge in
  kiosk mode, the only way to get full screen without a click on the PC itself
  (browsers allow it from a page only after a user action); Alt+F4 ends it
- ``off``: nothing

After an update lan-streamer starts with --no-browser and opens nothing.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

log = logging.getLogger(__name__)

SETTING = "local_page"
MODES = ("player", "fullscreen", "off")
DEFAULT = "player"


def find_edge() -> str | None:
    if found := shutil.which("msedge"):
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and (path := Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe").exists():
            return str(path)
    return None


class LocalPage:
    def __init__(self, store, url: str, profile: Path):
        self.store = store
        self.url = url  # http://localhost:<port>
        self.profile = profile  # Edge's own profile for the kiosk window
        self.kiosk: subprocess.Popen | None = None

    @staticmethod
    def available() -> bool:
        """False for a service without a desktop (the Linux package)."""
        return sys.platform == "win32" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    def mode(self) -> str:
        mode = self.store.get_setting(SETTING) or DEFAULT
        return mode if mode in MODES else DEFAULT

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r} – one of {', '.join(MODES)}")
        self.store.set_setting(SETTING, mode)

    def state(self) -> dict:
        return {"mode": self.mode(), "modes": list(MODES), "edge": find_edge() is not None,
                "kiosk_open": self.kiosk is not None and self.kiosk.poll() is None}  # fmt: skip

    def open(self) -> str:
        """Opens the page as set; a kiosk window opened before is closed. Returns what it did."""
        self.close()
        mode = self.mode()
        if mode == "off":
            return "Nothing opened (set to off)."
        if mode == "player":
            webbrowser.open(self.url)
            return "Player page opened."
        edge = find_edge()
        if edge is None:
            webbrowser.open(self.url + "/tv")
            return "Microsoft Edge not found: opened the TV view – a click there goes full screen."
        self.profile.mkdir(parents=True, exist_ok=True)
        self.kiosk = subprocess.Popen([
            edge, "--kiosk", self.url + "/tv", "--edge-kiosk-type=fullscreen",
            "--autoplay-policy=no-user-gesture-required", "--no-first-run",
            f"--user-data-dir={self.profile}",  # own instance: closing it leaves other Edge windows alone
        ])  # fmt: skip
        log.info("Full screen page opened (Edge kiosk, Alt+F4 ends it)")
        return "Full screen opened (Alt+F4 on the PC ends it)."

    def close(self) -> None:
        """Ends the kiosk window lan-streamer opened, if it is still there."""
        if self.kiosk is not None and self.kiosk.poll() is None:
            self.kiosk.terminate()
            try:
                self.kiosk.wait(5)
            except subprocess.TimeoutExpired:
                self.kiosk.kill()
        self.kiosk = None
