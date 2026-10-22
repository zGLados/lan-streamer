"""End-to-end check of the RTMP/SRT ingest input against a running lan-streamer.

Adds an ingest input with the stream key "ci", sends a live test stream to it
over SRT (publish_srt.py with lan-streamer's Python and GStreamer), checks
that the input goes on air with the received tracks, and that another input
takes over once the stream stops. Removes the input at the end.

    python tests/e2e/check_ingest.py http://localhost:8080 <lan-streamer's python.exe>
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
from check_api import PASSWORD, wait_for

NAME = "CI ingest"
PUBLISH = "srt://127.0.0.1:8890?streamid=publish:ingest/ci"


def main() -> int:
    base, python = sys.argv[1].rstrip("/"), sys.argv[2]
    api = httpx.Client(base_url=base, timeout=30)
    wait_for("web server answers", lambda: api.get("/api/status").json()["state"] == "running", 60)
    assert api.post("/api/admin/login", json={"password": PASSWORD}).status_code == 204

    def ingest():
        return next((i for i in api.get("/api/status").json()["inputs"] if i["name"] == NAME), None)

    r = api.post("/api/admin/outputs/out1/inputs", json={
        "name": NAME, "plugin": "ingest", "settings": {"key": "ci"}, "priority": 90,
    })  # fmt: skip
    assert r.status_code == 201, r.text
    input_id = r.json()["id"]
    wait_for("the input waits and shows the addresses", lambda: (i := ingest()) and
             i["info"].startswith("waiting for a stream") and "key ci" in i["info"])  # fmt: skip

    publisher = subprocess.Popen([python, str(Path(__file__).with_name("publish_srt.py")), PUBLISH, "25"])
    try:
        wait_for("a stream sent over SRT goes on air", lambda: api.get("/api/status").json()["current"] == NAME, 30)
        info = ingest()["info"]
        assert info.startswith("receiving") and "H264" in info, info
        print(f"  ok: {info.split(' · ')[0]}")
        publisher.wait(40)
        assert publisher.returncode == 0, f"publisher failed ({publisher.returncode})"
        wait_for("when the stream stops, another input takes over",
                 lambda: api.get("/api/status").json()["current"] != NAME, 30)  # fmt: skip
    finally:
        if publisher.poll() is None:
            publisher.kill()
        api.delete(f"/api/admin/inputs/{input_id}")
    print("OK: RTMP/SRT ingest works")
    return 0


if __name__ == "__main__":
    sys.exit(main())
