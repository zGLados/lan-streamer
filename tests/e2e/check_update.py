"""End-to-end check of the updater on Windows: a real update with the real installer.

The running, installed lan-streamer is pointed (LANSTREAMER_UPDATE_FEED) at a
local "latest release" that offers the installer CI just built as version
99.0.0. Then, as on the admin page: check, install. The updater must download
it, verify its SHA-256, start it silently and end lan-streamer; the installer
replaces the files and starts lan-streamer again.

    python tests/e2e/check_update.py http://localhost:8080 --installer <setup.exe> \\
        --dir <folder served at --feed-url> --feed-url http://127.0.0.1:8001
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import httpx

PASSWORD = "ci-password-123"
VERSION = "99.0.0"


def step(text: str) -> None:
    print(f"  ok: {text}", flush=True)


def status(base: str) -> dict | None:
    try:
        return httpx.get(f"{base}/api/status", timeout=5).json()
    except (httpx.HTTPError, ValueError):
        return None


def wait(what: str, check, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(1)
    raise AssertionError(f"FAILED: {what} within {timeout_s:.0f} s")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--feed-url", required=True)
    args = parser.parse_args()
    base, feed = args.url.rstrip("/"), args.feed_url.rstrip("/")

    name = f"lan-streamer-setup-{VERSION}.exe"
    args.dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.installer, args.dir / name)
    data = (args.dir / name).read_bytes()
    (args.dir / "latest.json").write_text(json.dumps({
        "tag_name": VERSION, "html_url": f"{feed}/notes",
        "assets": [{"name": name, "browser_download_url": f"{feed}/{name}", "size": len(data),
                    "digest": "sha256:" + hashlib.sha256(data).hexdigest()}],
    }))  # fmt: skip

    wait("lan-streamer answers", lambda: status(base) is not None, 120)
    before = status(base)
    api = httpx.Client(base_url=base, timeout=60)
    assert api.post("/api/admin/login", json={"password": PASSWORD}).status_code == 204
    state = api.post("/api/admin/update/check").json()
    assert state["latest"] == VERSION and state["available"] and state["installable"], state
    step(f"the update is found: {state['current']} → {state['latest']}")

    response = api.post("/api/admin/update/install")
    assert response.status_code == 202, response.text
    step("install started")
    wait("lan-streamer ends for the installer", lambda: status(base) is None, 180)
    step("lan-streamer ended, the installer runs")
    wait("lan-streamer is back", lambda: (s := status(base)) is not None and s["started"] > before["started"], 300)
    after = status(base)
    step(f"lan-streamer restarted by the installer ({after['version']}, state {after['state']})")
    print("OK: update installed and restarted")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(exc)
        sys.exit(1)
