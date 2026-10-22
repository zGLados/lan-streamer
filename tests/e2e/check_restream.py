"""End-to-end check of the restream to RTMP against a running lan-streamer.

Sets the first output's restream URL to MediaMTX's own RTMP port (path
"restream-ci"), checks the restream goes live and that MediaMTX receives
H.264 + AAC there, then turns it off again.

    python tests/e2e/check_restream.py http://localhost:8080
"""

from __future__ import annotations

import sys

import httpx
from check_api import PASSWORD, wait_for

TARGET = "rtmp://127.0.0.1:1935/restream-ci"
MEDIAMTX_PATH = "http://127.0.0.1:9997/v3/paths/get/restream-ci"


def main() -> int:
    base = sys.argv[1].rstrip("/")
    api = httpx.Client(base_url=base, timeout=30)
    assert api.post("/api/admin/login", json={"password": PASSWORD}).status_code == 204
    out = api.get("/api/admin/outputs").json()[0]["id"]

    r = api.patch(f"/api/admin/outputs/{out}", json={"restream_url": TARGET})
    assert r.status_code == 200 and not r.json()["restart_required"], r.text
    try:
        wait_for("the restream goes live", lambda: api.get("/api/status").json()["restream"] == "live", 60)

        def received():
            path = httpx.get(MEDIAMTX_PATH, timeout=5)
            return path.status_code == 200 and path.json()["ready"] and path.json()["tracks"]

        tracks = wait_for("MediaMTX receives it over RTMP", received, 30)
        assert "H264" in tracks and any("MPEG-4 Audio" in t for t in tracks), tracks
        print(f"  ok: tracks {tracks}")
    finally:
        api.patch(f"/api/admin/outputs/{out}", json={"restream_url": ""})
    wait_for("turned off again", lambda: api.get("/api/status").json()["restream"] == "off", 15)
    wait_for("the RTMP stream has stopped", lambda: httpx.get(MEDIAMTX_PATH, timeout=5).status_code == 404, 30)
    print("OK: restream to RTMP works")
    return 0


if __name__ == "__main__":
    sys.exit(main())
