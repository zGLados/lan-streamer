"""End-to-end check of the web API against a running lan-streamer (stored configuration).

Sets the admin password, adds an input with a high priority and waits until it
is on air, lowers its priority, deletes it, and reads the live status over the
WebSocket and a log over the API.

    python tests/e2e/check_api.py http://localhost:8080
"""

from __future__ import annotations

import io
import json
import sys
import time
import zipfile

import httpx
from websockets.sync.client import connect

PASSWORD = "ci-password-123"


def wait_for(description: str, check, timeout_s: float = 30.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last:
                print(f"  ok: {description}")
                return last
        except httpx.HTTPError as exc:
            last = exc
        time.sleep(0.5)
    raise SystemExit(f"FAILED: {description} (last: {last})")


def main() -> int:
    base = sys.argv[1].rstrip("/")
    api = httpx.Client(base_url=base, timeout=30)

    wait_for("web server answers", lambda: api.get("/api/status").json()["state"] == "running", 60)
    session = api.get("/api/admin/session").json()
    if session["password_set"]:
        assert api.post("/api/admin/login", json={"password": PASSWORD}).status_code == 204
    else:
        assert api.post("/api/admin/setup", json={"password": PASSWORD}).status_code == 204
    print("  ok: logged in")

    plugins = {p["id"]: p for p in api.get("/api/admin/plugins").json()}
    assert "pattern" in plugins["test-pattern"]["schema"]["properties"], plugins
    print(f"  ok: plugins {sorted(plugins)}")

    r = api.post("/api/admin/outputs/out1/inputs", json={
        "name": "API input", "plugin": "test-pattern", "settings": {"pattern": "snow"}, "priority": 50,
    })  # fmt: skip
    assert r.status_code == 201, r.text
    input_id = r.json()["id"]
    wait_for("the new input goes on air", lambda: api.get("/api/status").json()["current"] == "API input")

    assert api.patch(f"/api/admin/inputs/{input_id}", json={"priority": 0}).status_code == 200
    wait_for("a lower priority hands over", lambda: api.get("/api/status").json()["current"] != "API input")

    assert api.delete(f"/api/admin/inputs/{input_id}").status_code == 204
    wait_for("the input is gone", lambda: all(
        i["id"] != input_id for i in api.get("/api/status").json()["inputs"]
    ))  # fmt: skip

    with connect(base.replace("http", "ws", 1) + "/api/ws") as ws:
        status = json.loads(ws.recv(timeout=10))
        assert status["state"] == "running", status
    print("  ok: live status over the WebSocket")

    # A plugin with a real requirement: pip of the installed app downloads it into the plugin
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("plugin.toml", 'id = "deps-check"\nname = "Deps check"\nversion = "1.0.0"\n'
                   'api_version = 1\nentry = "deps_check:Plugin"\nrequirements = ["six==1.16.0"]\n')
        z.writestr("deps_check.py", "import six\nfrom lanstreamer_sdk import InputPlugin, TestSource\n"
                   "class Plugin(InputPlugin):\n    async def get_source(self):\n"
                   "        return TestSource(pattern=six.text_type('ball'))\n")  # fmt: skip
    r = api.put("/api/admin/plugins", content=buffer.getvalue(), headers={"Content-Type": "application/zip"},
                timeout=300)  # fmt: skip
    assert r.status_code == 201, r.text
    assert api.delete("/api/admin/plugins/deps-check").status_code == 204
    print("  ok: plugin with a requirement installed (pip) and removed")

    log = api.get("/api/admin/logs/lan-streamer.log").text
    assert "Input added: API input" in log and "Input removed: API input" in log, log[-2000:]
    print("  ok: log over the API")
    print("OK: web API works")
    return 0


if __name__ == "__main__":
    sys.exit(main())
