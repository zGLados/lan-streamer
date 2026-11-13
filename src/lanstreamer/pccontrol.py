"""The "This PC" admin controls (Windows): autorun on login, restart/shut down, run a command.

These act on the streaming PC itself, so they are offered only on Windows (the owner's
display PC) and only behind the admin login. The Linux package runs as a systemd service:
autorun is systemd's job there and a reboot needs root, so the section is hidden.

Running a command is off by default: it runs whatever is typed with the rights of the
account lan-streamer runs under, triggered from the admin page on the LAN. It is enabled
by an explicit setting, and every command that runs is written to the log.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from lanstreamer import paths

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "lan-streamer"
COMMANDS_SETTING = "pc_commands_enabled"
SHORTCUTS_SETTING = "pc_shortcuts"  # named launch buttons, JSON [{"label", "command"}]
MAX_SHORTCUTS = 20
OUTPUT_LIMIT = 20_000  # characters of stdout/stderr returned to the admin page
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
DETACHED = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP


def autorun_command() -> str:
    """The command the autorun entry runs: lan-streamer in tray mode, windowless."""
    exe = Path(sys.executable)
    if root := paths.app_root():  # the installed app: <app>\python\pythonw.exe
        exe = root / "python" / "pythonw.exe"
    elif (windowless := exe.with_name("pythonw.exe")).exists():
        exe = windowless
    return f'"{exe}" -m lanstreamer --tray'


class PcControl:
    """Controls for the Windows PC lan-streamer runs on. Methods raise on other systems."""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def available() -> bool:
        return sys.platform == "win32"

    def _require_windows(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("this control works only on Windows")

    # --- start lan-streamer when Windows logs in ----------------------------------------

    def autorun_enabled(self) -> bool:
        if sys.platform != "win32":
            return False
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, RUN_NAME)
            return True
        except FileNotFoundError:
            return False

    def set_autorun(self, on: bool) -> None:
        self._require_windows()
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:  # fmt: skip
            if on:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autorun_command())
                log.info("Autorun on: %s", autorun_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                    log.info("Autorun off")
                except FileNotFoundError:
                    pass

    # --- restart / shut down ------------------------------------------------------------

    def restart(self, delay_s: int = 5) -> None:
        self._power("/r", delay_s)

    def shutdown(self, delay_s: int = 5) -> None:
        self._power("/s", delay_s)

    def cancel(self) -> None:
        """Aborts a restart/shutdown still in its delay (an accidental press)."""
        self._require_windows()
        subprocess.run(["shutdown", "/a"], creationflags=NO_WINDOW, check=False)

    def _power(self, flag: str, delay_s: int) -> None:
        self._require_windows()
        delay_s = max(0, min(int(delay_s), 600))
        log.warning("admin asked to %s the PC in %d s", "restart" if flag == "/r" else "shut down", delay_s)
        subprocess.run(["shutdown", flag, "/t", str(delay_s)], creationflags=NO_WINDOW, check=True)

    # --- run a command ------------------------------------------------------------------

    def commands_enabled(self) -> bool:
        return self.store.get_setting(COMMANDS_SETTING) == "1"

    def set_commands_enabled(self, on: bool) -> None:
        self.store.set_setting(COMMANDS_SETTING, "1" if on else "0")
        log.warning("admin %s running commands on this PC", "enabled" if on else "disabled")

    def run(self, command: str, wait: bool = True, timeout_s: int = 30) -> dict:
        """Runs a command (only when enabled). wait: capture output; otherwise launch detached."""
        if not self.commands_enabled():
            raise PermissionError("running commands is turned off")
        command = command.strip()
        if not command:
            raise ValueError("no command given")
        log.warning("admin runs on this PC (wait=%s): %s", wait, command)
        if not wait:
            return self._spawn(command)
        try:
            done = subprocess.run(command, shell=True, capture_output=True, text=True, check=False,
                                  timeout=max(1, min(int(timeout_s), 600)), creationflags=NO_WINDOW)  # fmt: skip
        except subprocess.TimeoutExpired as exc:
            return {"started": True, "timed_out": True, "returncode": None,
                    "stdout": _clip(exc.stdout), "stderr": _clip(exc.stderr)}  # fmt: skip
        return {"started": True, "returncode": done.returncode,
                "stdout": _clip(done.stdout), "stderr": _clip(done.stderr)}  # fmt: skip

    def _spawn(self, command: str) -> dict:
        """Launches a command detached (left running) and returns its pid."""
        proc = subprocess.Popen(command, shell=True, creationflags=DETACHED,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True)  # fmt: skip
        return {"started": True, "pid": proc.pid}

    # --- program buttons ----------------------------------------------------------------
    # Named shortcuts the admin defines once (label + command), shown as buttons. Defining
    # them needs the command switch on (same risk as the command box); once saved they can
    # be launched by button even with the box off, as a fixed, admin-curated list.

    def shortcuts(self) -> list[dict]:
        raw = self.store.get_setting(SHORTCUTS_SETTING)
        if not raw:
            return []
        try:
            items = json.loads(raw)
        except (ValueError, TypeError):
            return []
        out = []
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and str(it.get("label", "")).strip() and str(it.get("command", "")).strip():
                out.append({"label": str(it["label"]).strip(), "command": str(it["command"]).strip()})
        return out[:MAX_SHORTCUTS]

    def set_shortcuts(self, items: list[dict]) -> None:
        if not self.commands_enabled():
            raise PermissionError("turn on running commands to change the program buttons")
        clean = []
        for it in list(items)[:MAX_SHORTCUTS]:
            label = str(it.get("label", "")).strip()
            command = str(it.get("command", "")).strip()
            if label and command:
                clean.append({"label": label, "command": command})
        self.store.set_setting(SHORTCUTS_SETTING, json.dumps(clean))
        log.warning("admin set %d program button(s) on this PC", len(clean))

    def run_shortcut(self, index: int) -> dict:
        """Launches the stored shortcut at index (detached). Allowed without the command box."""
        items = self.shortcuts()
        if not 0 <= index < len(items):
            raise IndexError("no such program button")
        self._require_windows()
        item = items[index]
        log.warning("admin runs program button %r on this PC: %s", item["label"], item["command"])
        return self._spawn(item["command"])

    # --- for the admin page -------------------------------------------------------------

    def state(self) -> dict:
        return {
            "platform": sys.platform,
            "can_power": sys.platform == "win32",
            "autorun": self.autorun_enabled(),
            "commands_enabled": self.commands_enabled(),
            "shortcuts": self.shortcuts(),
        }


def _clip(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return text if len(text) <= OUTPUT_LIMIT else text[:OUTPUT_LIMIT] + "\n… (output cut off)"
