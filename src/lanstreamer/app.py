"""M1 prototype: one output, switching between inputs, viewed in the browser.

Starts MediaMTX, picks an encoder, starts the output worker and a small web
page with a WebRTC player. Until the real scheduler exists (M2), a demo
rotation switches to the next input every few seconds.

    python -m lanstreamer [--fps 30] [--source <file or URL> ...] [--smoke-test 40]
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from lanstreamer import __version__, paths
from lanstreamer.media import encoders
from lanstreamer.web import server

OUTPUT_PATH = "out1"
RTSP_PORT = 8554  # only used to see that MediaMTX is up
SRT_PORT = 8890
WEBRTC_PORT = 8889

log = logging.getLogger("lanstreamer")


@dataclass
class Status:
    """What the web page shows; written by the app, read by the web server."""

    version: str = __version__
    state: str = "starting"
    encoder: str = ""
    resolution: str = ""
    target_fps: int = 0
    fps: float = 0.0
    current: str = ""
    sources: list[str] = field(default_factory=list)
    switches: int = 0
    failed_switches: int = 0
    last_error: str = ""
    whep_port: int = WEBRTC_PORT
    whep_path: str = OUTPUT_PATH
    started: float = field(default_factory=time.time)


def demo_sources(extra: list[str]) -> list[tuple[str, dict]]:
    """Two generated test inputs plus any file or URL given on the command line."""
    sources: list[tuple[str, dict]] = [
        ("Test input A", {"kind": "test", "pattern": "smpte", "freq": 440}),
        ("Test input B", {"kind": "test", "pattern": "ball", "freq": 660}),
    ]
    for item in extra:
        uri = item if "://" in item else Path(item).resolve().as_uri()
        sources.append((Path(item).name or item, {"kind": "uri", "uri": uri}))
    return sources


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
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.status = Status(target_fps=args.fps, resolution=f"{args.width}x{args.height}")
        self.sources = demo_sources(args.source)
        self.status.sources = [label for label, _ in self.sources]
        self.next_index = 0
        self.worker: subprocess.Popen | None = None
        self.mediamtx: subprocess.Popen | None = None
        self.mediamtx_log = subprocess.DEVNULL  # replaced by a log file in main()
        self.ready = threading.Event()
        self.failed = threading.Event()
        self.lock = threading.Lock()

    # --- start ------------------------------------------------------------------

    def start(self) -> None:
        a = self.args
        log.info("lan-streamer %s – %dx%d @ %d fps", __version__, a.width, a.height, a.fps)

        log.info("Selecting encoder (preference: %s) ...", a.encoder)
        spec, tries = encoders.select(a.encoder, a.fps)
        for line in tries:
            log.info("  %s", line)
        if spec is None:
            raise RuntimeError("no working H.264 encoder found")
        self.status.encoder = f"{spec.element} ({spec.vendor})"

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
            cwd=workdir,
            stdout=self.mediamtx_log,
            stderr=subprocess.STDOUT,
        )
        if not wait_for_port(RTSP_PORT, 15):
            raise RuntimeError(f"MediaMTX did not open port {RTSP_PORT}")

        bitrate = a.bitrate or (8000 if a.fps > 30 else 6000)
        cmd = [
            sys.executable, "-m", "lanstreamer.media.output",
            "--encoder", spec.element,
            "--publish", f"srt://127.0.0.1:{SRT_PORT}?streamid=publish:{OUTPUT_PATH}&latency=20",
            "--width", str(a.width), "--height", str(a.height),
            "--fps", str(a.fps), "--bitrate", str(bitrate),
        ]  # fmt: skip
        log.info("Starting output worker (%s, %d kbit/s)", spec.element, bitrate)
        self.worker = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1
        )
        threading.Thread(target=self._read_worker, daemon=True).start()
        if not self.ready.wait(30) or self.failed.is_set():
            raise RuntimeError(f"output worker did not start: {self.status.last_error}")

        self.status.state = "running"
        self.next_source()

    def _read_worker(self) -> None:
        assert self.worker and self.worker.stdout
        for line in self.worker.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.info("worker: %s", line.rstrip())
                continue
            self._on_event(event)
        if self.status.state != "stopping":
            self.status.state = "failed"
            self.failed.set()
            self.ready.set()

    def _on_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "ready":
            self.ready.set()
        elif kind == "playing":
            self.status.current = event["label"]
            self.status.switches += 1
            log.info("On air: %s", event["label"])
        elif kind == "switch_failed":
            self.status.failed_switches += 1
            log.warning("Switch to %s failed: %s", event["label"], event["reason"])
        elif kind == "stats":
            self.status.fps = event["fps"]
        elif kind == "error":
            self.status.last_error = event["message"]
            log.error("Output error: %s", event["message"])
            self.failed.set()
            self.ready.set()

    # --- control ----------------------------------------------------------------

    def next_source(self) -> None:
        with self.lock:
            label, source = self.sources[self.next_index % len(self.sources)]
            self.next_index += 1
            self._send({"cmd": "play", "label": label, "source": source})

    def _send(self, command: dict) -> None:
        if self.worker and self.worker.stdin and self.worker.poll() is None:
            self.worker.stdin.write(json.dumps(command) + "\n")
            self.worker.stdin.flush()

    def run(self) -> int:
        a = self.args
        deadline = time.monotonic() + a.smoke_test if a.smoke_test else None
        next_switch = time.monotonic() + a.switch_interval
        while not self.failed.is_set():
            if deadline and time.monotonic() > deadline:
                return self._smoke_result()
            if time.monotonic() >= next_switch:
                self.next_source()
                next_switch = time.monotonic() + a.switch_interval
            time.sleep(0.2)
        log.error("Output worker stopped: %s", self.status.last_error or "unknown reason")
        return 1

    def _smoke_result(self) -> int:
        s = self.status
        ok = s.switches >= 2 and s.fps >= 10 and not s.last_error
        log.info(
            "Smoke test %s: %d switches, %d failed, %.1f fps (target %d)",
            "passed" if ok else "FAILED", s.switches, s.failed_switches, s.fps, s.target_fps,
        )
        return 0 if ok else 1

    def stop(self) -> None:
        self.status.state = "stopping"
        if self.worker and self.worker.poll() is None:
            self._send({"cmd": "stop"})
            try:
                self.worker.wait(5)
            except subprocess.TimeoutExpired:
                self.worker.kill()
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
    parser.add_argument("--switch-interval", type=float, default=20.0, metavar="SECONDS")
    parser.add_argument("--port", type=int, default=8080, help="web page port")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--smoke-test", type=float, default=0, metavar="SECONDS",
                        help="run this long, then exit 0 if switching worked")  # fmt: skip
    args = parser.parse_args(argv)

    logs = paths.data_dir() / "logs"
    logs.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(logs / "lan-streamer.log", "w")],
    )

    app = App(args)
    with open(logs / "mediamtx.log", "w") as mediamtx_log:
        app.mediamtx_log = mediamtx_log
        try:
            app.start()
            server.start(args.port, app)
            url = f"http://localhost:{args.port}"
            log.info("Open %s  (Ctrl+C to stop)", url)
            if not args.no_browser and not args.smoke_test:
                webbrowser.open(url)
            return app.run()
        except KeyboardInterrupt:
            return 0
        except RuntimeError as exc:
            log.error("%s", exc)
            log.error("Logs: %s", logs)
            return 1
        finally:
            app.stop()
