"""Updates of lan-streamer itself, from the GitHub releases.

Checks the latest release on start and every CHECK_EVERY_S (can be turned off
on the admin page) and shows it there. Installing is the admin's click:

Windows, install for all users (C:\\Program Files): no admin prompt. The installer
registered the task "lan-streamer Update", which runs update-helper.ps1 as SYSTEM
and which every user may start. lan-streamer starts it, leaves a small relauncher
behind (PowerShell, in the user's session) and ends; the task downloads the
release's installer, checks its SHA-256 and installs silently; the relauncher
then starts lan-streamer again. The task's result (updates/result.json in the
program folder) is shown after the restart.

Windows, install "only for me": the release's installer is downloaded, its size
and SHA-256 checked, and started silently (no admin rights needed); lan-streamer
ends, the installer starts it again (/LAUNCH, see lan-streamer.iss).

Linux and repo checkouts: the update is only shown, with the link.

LANSTREAMER_UPDATE_FEED replaces the GitHub address (tests, CI): a URL that
answers like GitHub's "latest release" API.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from lanstreamer import paths

log = logging.getLogger("lanstreamer.updater")

FEED = "https://api.github.com/repos/zGLados/lan-streamer/releases/latest"
FIRST_CHECK_S = 60.0  # after the start: the outputs come first
CHECK_EVERY_S = 12 * 3600.0
SETTING = "update_check"  # "on" / "off"
INSTALLER = re.compile(r"^lan-streamer-setup-[\w.\-]+\.exe$")
TIMEOUT_S = 30.0
TASK = "lan-streamer Update"  # registered by the installer (update-task.ps1)
RESULT_FRESH_S = 3600.0  # a task result this old is still shown after the restart

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?$")


def version_key(text: str) -> tuple:
    """Semantic Versioning order: 0.1.1-test.5 < 0.1.1 < 0.1.2. Unknown text sorts first."""
    match = _VERSION.match(text.strip())
    if not match:
        return (-1,)
    major, minor, patch, pre = match.groups()
    if pre is None:
        return (int(major), int(minor), int(patch), 1, ())
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (int(major), int(minor), int(patch), 0, parts)


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


@dataclass
class UpdateState:
    current: str
    latest: str = ""
    available: bool = False
    notes_url: str = ""
    checked_at: float = 0.0  # time.time() of the last answer
    state: str = "idle"  # idle, checking, downloading, installing
    progress: int = 0  # download, percent
    error: str = ""
    installable: bool = False  # this install can update itself (Windows installer)
    how: str = ""  # what the admin page says instead of the button


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "lan-streamer-updater",
    })  # fmt: skip
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def download(url: str, target: Path, size: int, progress: Callable[[int], None]) -> str:
    """Blocking: writes url to target, returns the SHA-256 (hex)."""
    digest = hashlib.sha256()
    done = 0
    request = urllib.request.Request(url, headers={"User-Agent": "lan-streamer-updater"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response, open(target, "wb") as file:
        while chunk := response.read(1 << 20):
            file.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if size:
                progress(min(100, done * 100 // size))
    if size and done != size:
        raise ValueError(f"download incomplete ({done} of {size} bytes)")
    return digest.hexdigest()


def task_exists() -> bool:
    """Windows: is the update task there (install for all users)?"""
    if sys.platform != "win32":
        return False
    result = subprocess.run(["schtasks.exe", "/Query", "/TN", TASK], check=False, capture_output=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # fmt: skip
    return result.returncode == 0


def run_task(root: Path, relaunch: list[str], log_file: Path | None = None) -> None:
    """Windows: starts the update task (SYSTEM) and a relauncher in this session.

    The relauncher waits for the task's result file (a newer one than now), then
    starts lan-streamer again – also after a failed update, so it does not stay off.
    It writes what it does to log_file (logs/relaunch.log).
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(["schtasks.exe", "/Run", "/TN", TASK], check=False, capture_output=True, text=True,
                            creationflags=flags)  # fmt: skip
    if result.returncode != 0:
        raise OSError(f"the update task could not be started: {(result.stdout + result.stderr).strip()}")
    marker = root / "updates" / "result.json"
    args = ", ".join("'" + a.replace("'", "''") + "'" for a in relaunch[1:])
    out = str(log_file or os.devnull).replace("'", "''")
    script = (
        f"$log = '{out}'; function Say($t) {{ Add-Content -Path $log -Value \"$(Get-Date -Format o) $t\" }}; "
        "Say 'waiting for the update task'; "
        f"$start = (Get-Date).ToUniversalTime(); $file = '{marker}'; "
        "for ($i = 0; $i -lt 1200; $i++) { Start-Sleep 1; "
        "if ((Test-Path $file) -and (Get-Item $file).LastWriteTimeUtc -gt $start) { break } }; "
        "Say \"task finished: $(if (Test-Path $file) { Get-Content $file -Raw } else { 'no result after 20 min' })\"; "
        f"Start-Sleep 2; try {{ Start-Process -FilePath '{relaunch[0]}' -ArgumentList @({args}); "
        "Say 'lan-streamer started' } catch { Say \"start failed: $_\" }"
    )
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    # no DETACHED_PROCESS: PowerShell started without any console does not run at all
    # (tested from pythonw.exe, 2026-10-26); CREATE_NO_WINDOW gives it a hidden one
    subprocess.Popen(
        [str(powershell), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
        creationflags=flags | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), close_fds=True,
    )  # fmt: skip


def start_installer(installer: Path, args: list[str], elevate: bool) -> None:
    """Windows: starts the installer detached; with UAC if the install is for all users."""
    import ctypes  # Windows only

    params = " ".join(args)
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas" if elevate else "open", str(installer), params, str(installer.parent), 0
    )
    if result <= 32:  # ShellExecute's errors; 5 also when the UAC prompt was declined
        raise OSError(f"the installer could not be started (code {result}; was the Windows prompt declined?)")


class Updater:
    def __init__(
        self,
        current: str,
        store: Any,
        data_dir: Path,
        quit_app: Callable[[], None],
        fps: int | None = None,
        feed: str | None = None,
        fetch: Callable[[str], dict[str, Any]] = fetch_json,
        launch: Callable[[Path, list[str], bool], None] = start_installer,
        has_task: Callable[[], bool] = task_exists,
        start_task: Callable[[Path, list[str], Path | None], None] = run_task,
    ):
        self.store = store
        self.data_dir = data_dir
        self.quit_app = quit_app
        self.fps = fps  # relaunch with the same frame rate as the start menu entry
        self.feed = feed or os.environ.get("LANSTREAMER_UPDATE_FEED") or FEED
        self.fetch = fetch
        self.launch = launch
        self.status = UpdateState(current=current)
        self.asset: dict[str, Any] | None = None
        self.root = paths.app_root() if sys.platform == "win32" else None
        self.has_task = has_task
        self.start_task = start_task
        self._last_result()
        if self.root is None:
            self.status.how = ("Install the update with the Linux package (.deb / .tar.gz) from the release."
                               if sys.platform != "win32" else
                               "Not an installed lan-streamer (repo checkout): update with git.")  # fmt: skip

    def _last_result(self) -> None:
        """After an update by the task: show its failure (a success shows as the new version)."""
        if self.root is None:
            return
        try:
            result = json.loads((self.root / "updates" / "result.json").read_text(encoding="utf-8-sig"))
            age = time.time() - datetime.fromisoformat(str(result["time"])).timestamp()
        except (OSError, ValueError, KeyError, TypeError):
            return
        if not result.get("ok") and age < RESULT_FRESH_S:
            self.status.error = f"the last update failed: {result.get('message', '')}"
            log.warning("%s", self.status.error)

    @property
    def auto_check(self) -> bool:
        return (self.store.get_setting(SETTING) or "on") == "on"

    def set_auto_check(self, on: bool) -> None:
        self.store.set_setting(SETTING, "on" if on else "off")

    def state(self) -> dict[str, Any]:
        return {**asdict(self.status), "auto_check": self.auto_check}

    async def run(self) -> None:
        """Checks now and then while lan-streamer runs."""
        await asyncio.sleep(FIRST_CHECK_S)
        while True:
            if self.auto_check:
                await self.check()
            await asyncio.sleep(CHECK_EVERY_S)

    async def check(self) -> dict[str, Any]:
        if self.status.state != "idle":
            return self.state()
        self.status.state, self.status.error = "checking", ""
        try:
            release = await asyncio.to_thread(self.fetch, self.feed)
            tag = str(release.get("tag_name", ""))
            if version_key(tag) == (-1,):
                raise ValueError(f"the latest release has no version tag ({tag!r})")
            self.status.latest = tag.removeprefix("v")
            self.status.notes_url = str(release.get("html_url", ""))
            self.status.available = is_newer(self.status.latest, self.status.current)
            self.asset = next((a for a in release.get("assets", []) if INSTALLER.match(str(a.get("name", "")))),
                              None)  # fmt: skip
            self.status.installable = self.root is not None and self.asset is not None
            self.status.checked_at = time.time()
            if self.status.available:
                log.info("Update available: %s (running %s)", self.status.latest, self.status.current)
        except (OSError, ValueError) as exc:
            self.status.error = f"could not check for updates: {exc}"
            log.info("%s", self.status.error)
        finally:
            self.status.state = "idle"
        return self.state()

    async def install(self) -> None:
        """Downloads, checks and starts the installer, then ends lan-streamer."""
        if self.status.state != "idle":
            raise RuntimeError(f"busy ({self.status.state})")
        if not (self.status.available and self.status.installable and self.asset and self.root):
            raise RuntimeError(self.status.how or "no update to install – check first")
        asset = self.asset
        digest = str(asset.get("digest") or "")
        if not digest.startswith("sha256:"):
            raise RuntimeError("the release gives no SHA-256 for the installer – not installed")
        relaunch = [str(self.root / "python" / "pythonw.exe"), "-m", "lanstreamer", "--tray"]
        if self.fps:
            relaunch += ["--fps", str(self.fps)]
        if await asyncio.to_thread(self.has_task):
            # install for all users: the SYSTEM task downloads, checks and installs it
            self.status.state, self.status.error = "installing", ""
            try:
                log.info("Installing %s through the update task, lan-streamer ends now", self.status.latest)
                await asyncio.to_thread(self.start_task, self.root, relaunch, self.data_dir / "logs" / "relaunch.log")
            except OSError as exc:
                self.status.state, self.status.error = "idle", f"update failed: {exc}"
                log.warning("%s", self.status.error)
                return
            await asyncio.sleep(2)
            self.quit_app()
            return
        self.status.state, self.status.progress, self.status.error = "downloading", 0, ""
        try:
            folder = self.data_dir / "updates"
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.glob("lan-streamer-setup-*.exe"):
                old.unlink(missing_ok=True)
            target = folder / str(asset["name"])

            def progress(percent: int) -> None:
                self.status.progress = percent

            got = await asyncio.to_thread(
                download, str(asset["browser_download_url"]), target, int(asset.get("size") or 0), progress
            )
            if got != digest.removeprefix("sha256:").lower():
                target.unlink(missing_ok=True)
                raise ValueError("the downloaded installer does not match the release's SHA-256")
            self.status.state = "installing"
            args = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/LAUNCH=1",
                    f'/LOG="{folder / "install.log"}"']  # what the installer did, for problems
            if self.fps:
                args.append(f"/LAUNCHFPS={self.fps}")
            log.info("Installing %s: starting the installer, lan-streamer ends now", self.status.latest)
            await asyncio.to_thread(self.launch, target, args, self._needs_admin())
        except (OSError, ValueError) as exc:
            self.status.state, self.status.error = "idle", f"update failed: {exc}"
            log.warning("%s", self.status.error)
            return
        # leave the answer to reach the admin page, then make room for the installer
        await asyncio.sleep(2)
        self.quit_app()

    def _needs_admin(self) -> bool:
        """An install in %LOCALAPPDATA% ("only for me") updates without admin rights."""
        local = os.environ.get("LOCALAPPDATA")
        return not (local and self.root and str(self.root).lower().startswith(local.lower()))
