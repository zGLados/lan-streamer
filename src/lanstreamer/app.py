"""lan-streamer: the outputs, their inputs and schedulers, the web pages.

Starts MediaMTX (shared), and per stored output: picks an encoder, starts the
output worker, the input plugins and the scheduler (core.runtime). Serves the
player page, the TV view and the admin page. Outputs added or removed on the
admin page start or stop while running. --source starts a test run with one
output: two test patterns plus that file or URL.

    python -m lanstreamer [--fps 30] [--source <file or URL> ...] [--smoke-test 40]
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn

from lanstreamer import BUILD, paths, tray
from lanstreamer.core.auth import Auth
from lanstreamer.core.config import OutputConfig, demo_output
from lanstreamer.core.restreamer import Restreamer
from lanstreamer.core.runtime import OutputRuntime
from lanstreamer.core.simworker import SimulatedWorker
from lanstreamer.core.store import NotFound, Store
from lanstreamer.core.worker import OutputWorker, WorkerError
from lanstreamer.localpage import LocalPage
from lanstreamer.media import encoders
from lanstreamer.plugins import choice
from lanstreamer.plugins.registry import discover
from lanstreamer.updater import Updater
from lanstreamer.web.api import Backend, Changes, create_app

RTSP_PORT = 8554  # only used to see that MediaMTX is up
SRT_PORT = 8890
WEBRTC_PORT = 8889
# MediaMTX's control API, only on this machine: the ingest input asks it whether
# someone publishes (the shipped default config has it off, env vars override it).
MEDIAMTX_API = "127.0.0.1:9997"
SHUTDOWN_S = 5  # open web connections get this long to finish when lan-streamer stops

log = logging.getLogger("lanstreamer")

# Console programs (MediaMTX) must not open a console window when the app runs
# windowless from the tray.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass
class Status:
    """What the web page shows for one output; written by the app, read by the web server."""

    version: str = BUILD
    state: str = "starting"
    output_id: str = "out1"
    output_name: str = ""
    encoder: str = ""
    stream: str = ""  # negotiated H.264 caps (profile, level)
    ts_table: str = ""  # tracks in the MPEG-TS sent to MediaMTX
    resolution: str = ""
    target_fps: int = 0
    fps: float = 0.0
    current: str = ""
    now: dict[str, Any] = field(default_factory=dict)  # title, channel, submitter of what is on air
    viewer_version: int = 0  # changes when a viewer form or its items change (pages reload them)
    sources: list[str] = field(default_factory=list)
    inputs: list[dict[str, Any]] = field(default_factory=list)
    switches: int = 0
    failed_switches: int = 0
    last_error: str = ""
    restream: str = "off"  # off, connecting, live, retrying in … s: <error>
    debug_clock: bool = False  # the player page shows its local clock to compare
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


class Output:
    """One running output: its worker (GStreamer process), its inputs and scheduler, its status."""

    def __init__(self, app: App, cfg: OutputConfig):
        self.app = app
        self.cfg = cfg
        self.status = Status(
            output_id=cfg.id, output_name=cfg.name,
            target_fps=cfg.fps, resolution=f"{cfg.width}x{cfg.height}",
            whep_path=cfg.id, sources=[i.name for i in cfg.inputs],
        )  # fmt: skip
        self.worker: OutputWorker | SimulatedWorker | None = None
        self.runtime: OutputRuntime | None = None
        self.scheduler: asyncio.Task | None = None
        self.restreamer: Restreamer | None = None
        self.log_file: Any = subprocess.DEVNULL

    @property
    def log_name(self) -> str:
        """The first output keeps the old name output.log, the others output-<id>.log."""
        first = next(iter(self.app.outputs), self.cfg.id) == self.cfg.id
        return "output.log" if first else f"output-{self.cfg.id}.log"

    async def start(self) -> None:
        out = self.cfg
        log.info("Output %s: %dx%d @ %d fps", out.name, out.width, out.height, out.fps)
        if self.app.args.simulate:
            self.status.encoder = "simulated"
            self.worker = SimulatedWorker(self._on_worker_event, out.fps)
            await self.worker.start()
        else:
            await self._start_worker(out)
        await self._debug_clock(out.debug_clock)
        self.runtime = OutputRuntime(out, self.app.plugins(), paths.data_dir(), self.worker,
                                     on_change=self._on_change)  # fmt: skip
        log.info("Starting %d inputs of %s", len(out.inputs), out.name)
        await self.runtime.start()
        self.scheduler = asyncio.create_task(self.runtime.run())
        self.status.state = "running"
        await self._restream(out.restream_url)
        self.refresh_status()

    async def _debug_clock(self, on: bool) -> None:
        assert self.worker is not None
        await self.worker.debug_clock(on)
        self.status.debug_clock = on

    async def _restream(self, url: str) -> None:
        """Starts, changes or stops sending this output to an RTMP server."""
        if self.restreamer is not None and self.restreamer.target == url:
            return
        if self.restreamer is not None:
            await self.restreamer.stop()
            self.restreamer = None
        if not url:
            self.status.restream = "off"
            return
        if self.app.args.simulate:
            self.status.restream = "simulated (not sent)"
            return
        self.restreamer = Restreamer(
            self.cfg.name, f"srt://127.0.0.1:{SRT_PORT}?streamid=read:{self.cfg.id}", url,
            on_change=self._on_change, stderr=self.log_file,
        )  # fmt: skip
        self.restreamer.start()

    async def _start_worker(self, out: OutputConfig) -> None:
        spec = await self.app.encoder(out.encoder, out.fps)
        self.status.encoder = f"{spec.element} ({spec.vendor})"
        bitrate = out.bitrate_kbps or (8000 if out.fps > 30 else 6000)
        log.info("Starting output worker for %s (%s, %d kbit/s)", out.name, spec.element, bitrate)
        if self.app.logs is not None:
            self.log_file = open(self.app.logs / self.log_name, "w")  # noqa: SIM115, ASYNC230 – closed in stop()
        self.worker = OutputWorker(
            [
                "--encoder", spec.element,
                "--publish", f"srt://127.0.0.1:{SRT_PORT}?streamid=publish:{out.id}&latency=20",
                "--width", str(out.width), "--height", str(out.height),
                "--fps", str(out.fps), "--bitrate", str(bitrate),
            ],
            self._on_worker_event, stderr=self.log_file,
        )  # fmt: skip
        try:
            await self.worker.start()
        except WorkerError as exc:
            raise RuntimeError(str(exc)) from exc

    def _on_worker_event(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "stream":
            self.status.stream = event["caps"]
            log.info("Encoded stream (%s): %s", self.cfg.name, event["caps"])
        elif kind == "ts_table":
            if event["sent"]:
                self.status.ts_table = event["pmt"]
            log.info("MPEG-TS %s: %s", "sent" if event["sent"] else "held back", event["pmt"])
        elif kind == "stats":
            self.status.fps = event["fps"]
        elif kind == "error":
            self.status.last_error = event["message"]
            log.error("Output error (%s): %s", self.cfg.name, event["message"])
        elif kind == "exited" and self.status.state not in ("stopping", "removed"):
            self.status.state = "failed"
            self.app.failed.set()
        elif self.runtime is not None:
            self.runtime.on_worker_event(event)

    def _on_change(self) -> None:
        self.refresh_status()
        self.app.changes.notify()

    def refresh_status(self) -> None:
        if self.runtime is None:
            return
        snapshot = self.runtime.status()
        self.status.current = snapshot["current"]
        self.status.now = snapshot["now"]
        self.status.viewer_version = snapshot["viewer_version"]
        self.status.switches = snapshot["switches"]
        self.status.failed_switches = snapshot["failed_switches"]
        self.status.inputs = snapshot["inputs"]
        if self.restreamer is not None:
            self.status.restream = self.restreamer.state

    async def apply(self, stored: OutputConfig) -> None:
        """An admin change: take over the stored configuration (format/encoder need a restart)."""
        self.cfg.name, self.status.output_name = stored.name, stored.name
        self.cfg.inputs = stored.inputs
        self.cfg.overlay, self.cfg.overlay_s = stored.overlay, stored.overlay_s
        self.cfg.restream_url = stored.restream_url
        if self.worker is not None and stored.debug_clock != self.cfg.debug_clock:
            await self._debug_clock(stored.debug_clock)
        self.cfg.debug_clock = stored.debug_clock
        if self.runtime is not None:
            await self.runtime.apply(self.cfg, self.app.plugins())
            await self._restream(stored.restream_url)
        self.status.sources = [i.name for i in self.cfg.inputs]

    def smoke_ok(self) -> bool:
        s = self.status
        ok = s.switches >= 1 and s.failed_switches == 0 and s.fps >= 10 and not s.last_error
        log.info(
            "Smoke test %s (%s): %d switches, %d failed, %.1f fps (target %d)",
            "passed" if ok else "FAILED", self.cfg.name, s.switches, s.failed_switches, s.fps, s.target_fps,
        )  # fmt: skip
        return ok

    async def stop(self, state: str = "stopping") -> None:
        self.status.state = state
        if self.restreamer:
            await self.restreamer.stop()
        if self.scheduler:
            self.scheduler.cancel()
        if self.runtime:
            await self.runtime.stop()
        if self.worker:
            await self.worker.stop()
        if self.log_file is not subprocess.DEVNULL:
            self.log_file.close()


class App:
    """MediaMTX (shared) and the outputs; outputs are added and removed while running."""

    def __init__(self, args: argparse.Namespace, outputs: list[OutputConfig], store: Store | None = None):
        self.args = args
        self.store = store  # None: test run (--source), the configuration is not stored
        self.changes = Changes()
        self.outputs: dict[str, Output] = {}
        for cfg in outputs:
            self.outputs[cfg.id] = Output(self, cfg)
        self.logs: Path | None = None  # set in run(): output workers log there
        self.mediamtx: subprocess.Popen | None = None
        self.mediamtx_log: Any = subprocess.DEVNULL  # replaced by a log file in run()
        self._encoders: dict[tuple[str, int], Any] = {}
        self.quit = threading.Event()  # set by the tray menu
        self.failed = threading.Event()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self.updater: Updater | None = None  # made with the web backend
        self.local_page: LocalPage | None = None  # likewise

    @property
    def first(self) -> Output:
        return next(iter(self.outputs.values()))

    def output(self, output_id: str | None) -> Output:
        """The output with this id (KeyError if none); None: the first one."""
        return self.first if not output_id else self.outputs[output_id]

    def plugins(self) -> dict[str, Path]:
        return {pid: m.path for pid, m in discover(paths.plugins_dir()).items()}

    # --- start ------------------------------------------------------------------

    async def start(self) -> None:
        log.info("lan-streamer %s – %d output(s)", BUILD, len(self.outputs))
        if self.args.simulate:
            log.info("Simulation: no video pipeline, no MediaMTX (local tests)")
        else:
            await asyncio.to_thread(self._start_mediamtx)
            if not await asyncio.to_thread(wait_for_port, RTSP_PORT, 15):
                raise RuntimeError(f"MediaMTX did not open port {RTSP_PORT}")
        for output in list(self.outputs.values()):
            await output.start()

    async def encoder(self, preference: str, fps: int) -> Any:
        """The encoder for an output; probed once per preference and frame rate."""
        key = (preference, fps)
        if key not in self._encoders:
            log.info("Selecting encoder (preference: %s, %d fps) ...", preference, fps)
            spec, tries = await asyncio.to_thread(encoders.select, preference, fps)
            for line in tries:
                log.info("  %s", line)
            if spec is None:
                raise RuntimeError("no working H.264 encoder found")
            self._encoders[key] = spec
        return self._encoders[key]

    def _start_mediamtx(self) -> None:
        exe = paths.mediamtx()
        if exe is None:
            raise RuntimeError("MediaMTX not found")
        log.info("Starting MediaMTX (%s)", exe)
        # Runs in the user's data folder: MediaMTX writes files (e.g. certificates)
        # next to itself, and C:\Program Files is read-only for users.
        workdir = paths.data_dir() / "mediamtx"
        workdir.mkdir(exist_ok=True)
        env = {**os.environ, "MTX_API": "yes", "MTX_APIADDRESS": MEDIAMTX_API}
        self.mediamtx = subprocess.Popen(
            [str(exe), str(exe.with_name("mediamtx.yml"))],
            cwd=workdir, creationflags=NO_WINDOW, env=env,
            stdout=self.mediamtx_log, stderr=subprocess.STDOUT,
        )  # fmt: skip

    # --- control ----------------------------------------------------------------

    def status_dict(self, output_id: str | None = None) -> dict[str, Any]:
        """One output's status (KeyError: no such output) and the list of all outputs."""
        output = self.output(output_id)
        data = dataclasses.asdict(output.status)
        data["uptime_s"] = round(time.time() - output.status.started)
        data["outputs"] = [{"id": o.cfg.id, "name": o.cfg.name, "current": o.status.current}
                           for o in self.outputs.values()]  # fmt: skip
        return data

    def skip(self, output_id: str | None = None) -> None:
        """The "Next input" button."""
        runtime = self.output(output_id).runtime
        if runtime:
            runtime.skip()

    async def apply_config(self, output_id: str) -> None:
        """An admin change: the output takes over its stored configuration, a new
        output starts, a deleted one stops."""
        if self.store is None:
            return
        async with self._lock:
            try:
                stored = self.store.output(output_id)
            except NotFound:
                stored = None
            running = self.outputs.get(output_id)
            if stored is None and running is not None:
                log.info("Output removed: %s", running.cfg.name)
                del self.outputs[output_id]
                await running.stop("removed")
            elif stored is not None and running is None:
                log.info("Output added: %s", stored.name)
                output = self.outputs[output_id] = Output(self, stored)
                task = asyncio.create_task(self._start_added(output))  # choosing an encoder takes a while
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            elif stored is not None and running is not None:
                await running.apply(stored)
        self.changes.notify()

    async def _start_added(self, output: Output) -> None:
        try:
            await output.start()
        except Exception as exc:  # noqa: BLE001 – the other outputs keep running
            output.status.state, output.status.last_error = "failed", str(exc)
            log.error("Output %s did not start: %s", output.cfg.name, exc)
        if self.outputs.get(output.cfg.id) is not output:  # removed while it was starting
            await output.stop("removed")
        self.changes.notify()

    def backend(self) -> Backend:
        store = self.store or Store(paths.data_dir() / "lanstreamer.db")
        self.updater = Updater(BUILD, store, paths.data_dir(), self.quit.set, fps=self.args.fps)
        if LocalPage.available():
            self.local_page = LocalPage(store, f"http://localhost:{self.args.port}", paths.data_dir() / "kiosk-browser")
        return Backend(
            status=self.status_dict, skip=self.skip, store=store, auth=Auth(store),
            plugins=lambda: discover(paths.plugins_dir()), apply=self.apply_config,
            logs=paths.data_dir() / "logs", changes=self.changes,
            plugins_dir=paths.plugins_dir(), plugins_changed=self.plugin_changed,
            viewer=_Viewers(self), updater=self.updater, local_page=self.local_page,
        )  # fmt: skip

    async def plugin_changed(self, plugin_id: str) -> None:
        """A plugin was uploaded or removed: inputs using it restart with the new code."""
        plugins = self.plugins()
        for output in list(self.outputs.values()):
            if output.runtime is not None:
                await output.runtime.reload_plugin(plugin_id, plugins)

    async def run(self, web: asyncio.Task | None = None) -> int:
        deadline = time.monotonic() + self.args.smoke_test if self.args.smoke_test else None
        stop_file = Path(self.args.smoke_stop_file) if getattr(self.args, "smoke_stop_file", None) else None
        while not self.failed.is_set():
            if self.quit.is_set() or (web is not None and web.done()):
                return 0  # tray "Quit" or Ctrl+C (the web server stops on it)
            if deadline and (time.monotonic() > deadline or (stop_file and stop_file.exists())):
                results = [o.smoke_ok() for o in self.outputs.values()]  # each one logs its result
                return 0 if all(results) else 1
            for output in list(self.outputs.values()):
                output.refresh_status()
            await asyncio.sleep(0.2)
        failed = next((o for o in self.outputs.values() if o.status.state == "failed"), self.first)
        log.error("Output worker of %s stopped: %s", failed.cfg.name, failed.status.last_error or "unknown reason")
        return 1

    async def stop(self) -> None:
        for output in list(self.outputs.values()):
            await output.stop()
        if self.mediamtx and self.mediamtx.poll() is None:
            self.mediamtx.terminate()
            try:
                self.mediamtx.wait(5)
            except subprocess.TimeoutExpired:
                self.mediamtx.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lanstreamer", description=__doc__.splitlines()[0])
    # Output settings: override the stored configuration for this run only
    parser.add_argument("--fps", type=int, choices=(30, 60))
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--bitrate", type=int, help="kbit/s (default: 8000 at 60 fps)")
    parser.add_argument("--encoder", choices=("auto", *encoders.AUTO_ORDER))
    parser.add_argument("--source", action="append", default=[],
                        help="test run: two test patterns plus this file or URL, "
                             "instead of the stored configuration")  # fmt: skip
    parser.add_argument("--switch-interval", type=float, default=20.0, metavar="SECONDS",
                        help="how long each test input stays on air at least")  # fmt: skip
    parser.add_argument("--port", type=int, default=8080, help="web page port")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--simulate", action="store_true",
                        help="no video pipeline (no GStreamer needed): for local tests")  # fmt: skip
    parser.add_argument("--smoke-stop-file", metavar="PATH",
                        help="with --smoke-test: end early once this file exists (CI: checks done)")  # fmt: skip
    parser.add_argument("--smoke-test", type=float, default=0, metavar="SECONDS",
                        help="run this long, then exit 0 if switching worked")  # fmt: skip
    parser.add_argument("--pause-on-error", action="store_true",
                        help="keep the console window open after an error")  # fmt: skip
    parser.add_argument("--tray", action="store_true",
                        help="run without console, with a tray icon (Windows start menu)")  # fmt: skip
    args = parser.parse_args(argv)
    tray.set_app_id()
    tray.hold_running_mutex()
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

    store = Store(paths.data_dir() / "lanstreamer.db")
    choice.on_start(store, paths.setup_file())
    if args.source:  # test run, nothing is stored
        outputs = [demo_output(60, args.switch_interval, args.source)]
    else:
        outputs = store.outputs()
    for output in outputs:  # command line options apply to every output, for this run only
        for name, value in (("fps", args.fps), ("width", args.width), ("height", args.height),
                            ("bitrate_kbps", args.bitrate), ("encoder", args.encoder)):  # fmt: skip
            if value is not None:
                setattr(output, name, value)
    app = App(args, outputs, None if args.source else store)
    app.logs = logs
    url = f"http://localhost:{args.port}"
    icon = None
    if args.tray:
        icon = tray.Tray(url, logs, lambda: f"On air: {app.first.status.current or '–'}", app.quit.set)
        icon.start()
    with open(logs / "mediamtx.log", "w") as mediamtx_log:
        app.mediamtx_log = mediamtx_log
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
            store.close()


async def _serve(app: App, args: argparse.Namespace, url: str, icon: tray.Tray | None) -> int:
    server = uvicorn.Server(uvicorn.Config(
        create_app(app.backend()), log_config=None, access_log=False,  # our logging (pythonw: no stderr)
        timeout_graceful_shutdown=SHUTDOWN_S,
    ))  # fmt: skip
    web: asyncio.Task | None = None
    sock: socket.socket | None = None
    try:
        # Bind the port ourselves: uvicorn calls sys.exit() when it cannot, which
        # would end the whole app without a message (e.g. lan-streamer already running).
        sock = open_port(args.port)
        await app.start()
        web = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            if web.done():
                raise RuntimeError("the web server did not start")
            await asyncio.sleep(0.05)
        log.info("Open %s  (%s to stop)", url, "tray menu" if icon else "Ctrl+C")
        if app.local_page is not None and not args.no_browser and not args.smoke_test:
            app.local_page.open()  # as set on the admin page: player page, full screen or nothing
        if icon:
            threading.Thread(target=_refresh_tray, args=(icon, app), daemon=True).start()
        if app.updater is not None and not args.smoke_test and not args.source:
            app._tasks.add(asyncio.create_task(app.updater.run()))  # cancelled when the loop ends
        return await app.run(web)
    finally:
        server.should_exit = True
        if web is not None:
            # On Windows a connection reset by the client can stay registered with the
            # asyncio server for ever, and the web server then waits for it without end
            # (CI 2026-10-26: "Shutting down", then nothing). Wait a while, then stop it.
            done, _ = await asyncio.wait({web}, timeout=SHUTDOWN_S + 2)
            if not done:
                log.warning("The web server did not stop within %d s – stopping it anyway", SHUTDOWN_S + 2)
                web.cancel()
            await asyncio.gather(web, return_exceptions=True)
        elif sock is not None:
            sock.close()
        await app.stop()


class _Viewers:
    """The web layer's view of the running outputs' viewer forms and queues.

    Input ids are unique across outputs, so a submission finds its output by
    the input id; the form list is per output.
    """

    def __init__(self, app: App):
        self._app = app

    def _runtime(self, output_id: str | None) -> OutputRuntime:
        runtime = self._app.output(output_id).runtime
        if runtime is None:
            raise KeyError("not running")
        return runtime

    def _owner(self, input_id: str) -> OutputRuntime:
        for output in self._app.outputs.values():
            if output.runtime is not None and input_id in output.runtime.inputs:
                return output.runtime
        raise KeyError(input_id)

    async def viewer_forms(self, output_id: str | None = None) -> list[dict[str, Any]]:
        return await self._runtime(output_id).viewer_forms()

    async def viewer_submit(self, input_id: str, form: dict[str, Any], device: str, ip: str) -> str:
        return await self._owner(input_id).viewer_submit(input_id, form, device, ip)

    async def items(self, input_id: str) -> list[dict[str, Any]]:
        return await self._owner(input_id).items(input_id)

    async def remove_item(self, input_id: str, item_id: str) -> None:
        await self._owner(input_id).remove_item(input_id, item_id)


def open_port(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":  # on Windows SO_REUSEADDR would allow two servers on one port
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as exc:
        sock.close()
        raise RuntimeError(
            f"port {port} is in use – is lan-streamer already running? ({exc.strerror})"
        ) from exc
    sock.listen(128)
    sock.setblocking(False)
    return sock


def _refresh_tray(icon: tray.Tray, app: App) -> None:
    while not app.quit.is_set() and app.first.status.state != "stopping":
        icon.refresh()
        time.sleep(2)
