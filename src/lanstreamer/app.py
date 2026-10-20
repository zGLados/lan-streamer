"""lan-streamer: one output, its inputs, the scheduler and the player page.

Starts MediaMTX, picks an encoder, starts the output worker, the input plugins
and the scheduler (core.runtime), and serves the player page. Until the
configuration lives in SQLite (M2 step 2), the inputs are two test patterns
plus one playlist per --source, all of equal priority.

    python -m lanstreamer [--fps 30] [--source <file or URL> ...] [--smoke-test 40]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any

from lanstreamer import BUILD, paths, tray
from lanstreamer.core.config import OutputConfig, demo_output
from lanstreamer.core.runtime import OutputRuntime
from lanstreamer.core.worker import OutputWorker, WorkerError
from lanstreamer.media import encoders
from lanstreamer.plugins.registry import discover
from lanstreamer.web import server

RTSP_PORT = 8554  # only used to see that MediaMTX is up
SRT_PORT = 8890
WEBRTC_PORT = 8889

log = logging.getLogger("lanstreamer")

# Console programs (MediaMTX) must not open a console window when the app runs
# windowless from the tray.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass
class Status:
    """What the web page shows; written by the app, read by the web server."""

    version: str = BUILD
    state: str = "starting"
    encoder: str = ""
    stream: str = ""  # negotiated H.264 caps (profile, level)
    ts_table: str = ""  # tracks in the MPEG-TS sent to MediaMTX
    resolution: str = ""
    target_fps: int = 0
    fps: float = 0.0
    current: str = ""
    sources: list[str] = field(default_factory=list)
    inputs: list[dict[str, Any]] = field(default_factory=list)
    switches: int = 0
    failed_switches: int = 0
    last_error: str = ""
    whep_port: int = WEBRTC_PORT
    whep_path: str = "out1"
    started: float = field(default_factory=time.time)


def wait_for_port(port: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


class App:
    def __init__(self, args: argparse.Namespace, output: OutputConfig):
        self.args = args
        self.output = output
        self.status = Status(
            target_fps=output.fps, resolution=f"{output.width}x{output.height}",
            whep_path=output.id, sources=[i.name for i in output.inputs],
        )  # fmt: skip
        self.mediamtx: subprocess.Popen | None = None
        self.mediamtx_log: Any = subprocess.DEVNULL  # replaced by log files in run()
        self.worker_log: Any = subprocess.DEVNULL
        self.worker: OutputWorker | None = None
        self.runtime: OutputRuntime | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.quit = threading.Event()  # set by the tray menu
        self.failed = threading.Event()

    # --- start ------------------------------------------------------------------

    async def start(self) -> None:
        out = self.output
        self.loop = asyncio.get_running_loop()
        log.info("lan-streamer %s – %dx%d @ %d fps", BUILD, out.width, out.height, out.fps)

        log.info("Selecting encoder (preference: %s) ...", out.encoder)
        spec, tries = await asyncio.to_thread(encoders.select, out.encoder, out.fps)
        for line in tries:
            log.info("  %s", line)
        if spec is None:
            raise RuntimeError("no working H.264 encoder found")
        self.status.encoder = f"{spec.element} ({spec.vendor})"

        await asyncio.to_thread(self._start_mediamtx)
        if not await asyncio.to_thread(wait_for_port, RTSP_PORT, 15):
            raise RuntimeError(f"MediaMTX did not open port {RTSP_PORT}")

        bitrate = out.bitrate_kbps or (8000 if out.fps > 30 else 6000)
        log.info("Starting output worker (%s, %d kbit/s)", spec.element, bitrate)
        self.worker = OutputWorker(
            [
                "--encoder", spec.element,
                "--publish", f"srt://127.0.0.1:{SRT_PORT}?streamid=publish:{out.id}&latency=20",
                "--width", str(out.width), "--height", str(out.height),
                "--fps", str(out.fps), "--bitrate", str(bitrate),
            ],
            self._on_worker_event, stderr=self.worker_log,
        )  # fmt: skip
        try:
            await self.worker.start()
        except WorkerError as exc:
            raise RuntimeError(str(exc)) from exc

        plugins = {pid: m.path for pid, m in discover(paths.data_dir() / "plugins").items()}
        self.runtime = OutputRuntime(
            out, plugins, paths.data_dir(), self.worker, on_change=self._refresh_status
        )
        log.info("Starting %d inputs", len(out.inputs))
        await self.runtime.start()
        self.status.state = "running"
        self._refresh_status()

    def _start_mediamtx(self) -> None:
        exe = paths.mediamtx()
        if exe is None:
            raise RuntimeError("MediaMTX not found")
        log.info("Starting MediaMTX (%s)", exe)
        # Runs in the user's data folder: MediaMTX writes files (e.g. certificates)
        # next to itself, and C:\Program Files is read-only for users.
        workdir = paths.data_dir() / "mediamtx"
        workdir.mkdir(exist_ok=True)
        self.mediamtx = subprocess.Popen(
            [str(exe), str(exe.with_name("mediamtx.yml"))],
            cwd=workdir, creationflags=NO_WINDOW,
            stdout=self.mediamtx_log, stderr=subprocess.STDOUT,
        )  # fmt: skip

    def _on_worker_event(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "stream":
            self.status.stream = event["caps"]
            log.info("Encoded stream: %s", event["caps"])
        elif kind == "ts_table":
            if event["sent"]:
                self.status.ts_table = event["pmt"]
            log.info("MPEG-TS %s: %s", "sent" if event["sent"] else "held back", event["pmt"])
        elif kind == "stats":
            self.status.fps = event["fps"]
        elif kind == "error":
            self.status.last_error = event["message"]
            log.error("Output error: %s", event["message"])
        elif kind == "exited" and self.status.state != "stopping":
            self.status.state = "failed"
            self.failed.set()
        elif self.runtime is not None:
            self.runtime.on_worker_event(event)

    def _refresh_status(self) -> None:
        if self.runtime is None:
            return
        snapshot = self.runtime.status()
        self.status.current = snapshot["current"]
        self.status.switches = snapshot["switches"]
        self.status.failed_switches = snapshot["failed_switches"]
        self.status.inputs = snapshot["inputs"]

    # --- control ----------------------------------------------------------------

    def next_source(self) -> None:
        """The "Next input" button on the page (called from the web server thread)."""
        if self.loop and self.runtime:
            self.loop.call_soon_threadsafe(self.runtime.skip)

    async def run(self) -> int:
        assert self.runtime is not None
        scheduler = asyncio.create_task(self.runtime.run())
        deadline = time.monotonic() + self.args.smoke_test if self.args.smoke_test else None
        try:
            while not self.failed.is_set():
                if self.quit.is_set():
                    return 0
                if deadline and time.monotonic() > deadline:
                    return self._smoke_result()
                self._refresh_status()
                await asyncio.sleep(0.2)
            log.error("Output worker stopped: %s", self.status.last_error or "unknown reason")
            return 1
        finally:
            scheduler.cancel()

    def _smoke_result(self) -> int:
        s = self.status
        ok = s.switches >= 2 and s.failed_switches == 0 and s.fps >= 10 and not s.last_error
        log.info(
            "Smoke test %s: %d switches, %d failed, %.1f fps (target %d)",
            "passed" if ok else "FAILED", s.switches, s.failed_switches, s.fps, s.target_fps,
        )
        return 0 if ok else 1

    async def stop(self) -> None:
        self.status.state = "stopping"
        if self.runtime:
            await self.runtime.stop()
        if self.worker:
            await self.worker.stop()
        if self.mediamtx and self.mediamtx.poll() is None:
            self.mediamtx.terminate()
            try:
                self.mediamtx.wait(5)
            except subprocess.TimeoutExpired:
                self.mediamtx.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lanstreamer", description=__doc__.splitlines()[0])
    parser.add_argument("--fps", type=int, default=60, choices=(30, 60))
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--bitrate", type=int, default=0, help="kbit/s (default: 8000 at 60 fps)")
    parser.add_argument("--encoder", default="auto", choices=("auto", *encoders.AUTO_ORDER))
    parser.add_argument("--source", action="append", default=[], help="extra input: file or URL")
    parser.add_argument("--switch-interval", type=float, default=20.0, metavar="SECONDS",
                        help="how long each test input stays on air at least")  # fmt: skip
    parser.add_argument("--port", type=int, default=8080, help="web page port")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--smoke-test", type=float, default=0, metavar="SECONDS",
                        help="run this long, then exit 0 if switching worked")  # fmt: skip
    parser.add_argument("--pause-on-error", action="store_true",
                        help="keep the console window open after an error")  # fmt: skip
    parser.add_argument("--tray", action="store_true",
                        help="run without console, with a tray icon (Windows start menu)")  # fmt: skip
    args = parser.parse_args(argv)
    tray.set_app_id()
    code = run(args)
    if code and args.pause_on_error and sys.stdin is not None:
        input("\nPress Enter to close this window ...")
    return code


def run(args: argparse.Namespace) -> int:
    logs = paths.data_dir() / "logs"
    logs.mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(logs / "lan-streamer.log", "w")]
    if sys.stderr is not None:  # None under pythonw.exe (tray mode)
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", handlers=handlers
    )

    output = demo_output(args.fps, args.switch_interval, args.source)
    output.width, output.height = args.width, args.height
    output.bitrate_kbps, output.encoder = args.bitrate, args.encoder
    app = App(args, output)
    url = f"http://localhost:{args.port}"
    icon = None
    if args.tray:
        icon = tray.Tray(url, logs, lambda: f"On air: {app.status.current or '–'}", app.quit.set)
        icon.start()
    with (
        open(logs / "mediamtx.log", "w") as mediamtx_log,
        open(logs / "output.log", "w") as worker_log,
    ):
        app.mediamtx_log, app.worker_log = mediamtx_log, worker_log
        try:
            return asyncio.run(_serve(app, args, url, icon))
        except KeyboardInterrupt:
            return 0
        except RuntimeError as exc:
            log.error("%s", exc)
            log.error("Logs: %s", logs)
            if args.tray:
                tray.message_box("lan-streamer", f"{exc}\n\nLogs: {logs}")
            return 1
        finally:
            if icon:
                icon.stop()


async def _serve(app: App, args: argparse.Namespace, url: str, icon: tray.Tray | None) -> int:
    try:
        await app.start()
        server.start(args.port, app)
        log.info("Open %s  (%s to stop)", url, "tray menu" if icon else "Ctrl+C")
        if not args.no_browser and not args.smoke_test:
            webbrowser.open(url)
        if icon:
            threading.Thread(target=_refresh_tray, args=(icon, app), daemon=True).start()
        return await app.run()
    finally:
        await app.stop()


def _refresh_tray(icon: tray.Tray, app: App) -> None:
    while not app.quit.is_set() and app.status.state != "stopping":
        icon.refresh()
        time.sleep(2)
