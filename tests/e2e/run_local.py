"""Local end-to-end run without GStreamer: everything except the video pipeline.

Starts lan-streamer in simulation mode with a fresh data folder, runs the web
API check and drives the admin page in the Windows Edge (headless, see
edge.py), then stops lan-streamer cleanly. Use it before asking for a CI build.

    python tests/e2e/run_local.py [--keep-running]

--keep-running leaves lan-streamer running afterwards, so you can look at
http://localhost:18080/admin in your own browser (Ctrl+C to stop).
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
PORT = 18080


def wait_running(base: str, timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/api/status", timeout=2).json()["state"] == "running":
                return
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(0.3)
    raise SystemExit("FAILED: lan-streamer did not start")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()
    base = f"http://localhost:{PORT}"
    data = Path(tempfile.mkdtemp(prefix="lanstreamer-e2e-"))
    env = {**os.environ, "XDG_DATA_HOME": str(data)}
    log = open(data / "app.out", "w")  # noqa: SIM115 – closed in finally, outlives the with-free flow
    app = subprocess.Popen(
        [sys.executable, "-m", "lanstreamer", "--simulate", "--no-browser", "--port", str(PORT)],
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    results: dict[str, int] = {}
    try:
        wait_running(base)
        print(f"lan-streamer (simulated) at {base}, data in {data}")
        print("== web API")
        results["web API"] = subprocess.run(
            [sys.executable, str(HERE / "check_api.py"), base], check=False
        ).returncode

        sys.path.insert(0, str(HERE))
        import edge

        print("== admin page in Edge")
        if not edge.available():
            print("  skipped: no Windows Edge reachable from here")
        else:
            with edge.windows_edge() as ws:
                results["admin page"] = subprocess.run(
                    [sys.executable, str(HERE / "check_admin.py"), base, "--cdp", ws], check=False
                ).returncode
        if args.keep_running:
            print(f"\nStill running: {base}/admin (password ci-password-123) – Ctrl+C to stop")
            app.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if app.poll() is None:
            app.send_signal(signal.SIGINT)
            try:
                app.wait(15)
            except subprocess.TimeoutExpired:
                app.kill()
        log.close()
    print()
    for name, code in results.items():
        print(f"{'OK    ' if code == 0 else 'FAILED'} {name}")
    if any(results.values()):
        print(f"app log: {data / 'app.out'}")
    return 1 if any(results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
