"""End-to-end check: picture and sound from two separate URLs (how YouTube HD is played).

Uploads a tiny plugin whose source takes the picture from one URL and the
sound from another (both the test clip over HTTP), adds it with a high
priority and checks it goes on air, then removes it again. YouTube itself is
not used: it often blocks datacenter addresses like CI runners.

    python tests/e2e/check_split_source.py http://localhost:8080 http://127.0.0.1:8000/clip.mp4
"""

from __future__ import annotations

import io
import sys
import zipfile

import httpx
from check_api import PASSWORD, wait_for

NAME = "CI split source"


def main() -> int:
    base, clip = sys.argv[1].rstrip("/"), sys.argv[2]
    api = httpx.Client(base_url=base, timeout=30)
    assert api.post("/api/admin/login", json={"password": PASSWORD}).status_code == 204

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("plugin.toml", 'id = "split-check"\nname = "Split check"\nversion = "1.0.0"\n'
                   'api_version = 1\nentry = "split_check:Plugin"\n')
        z.writestr("split_check.py", "from lanstreamer_sdk import InputPlugin, UriSource\n"
                   "class Plugin(InputPlugin):\n    async def get_source(self):\n"
                   f"        return UriSource({clip!r}, audio_uri={clip + '?audio'!r})\n")  # fmt: skip
    r = api.put("/api/admin/plugins", content=buffer.getvalue(), headers={"Content-Type": "application/zip"})
    assert r.status_code == 201, r.text
    r = api.post("/api/admin/outputs/out1/inputs", json={"name": NAME, "plugin": "split-check", "priority": 95})
    assert r.status_code == 201, r.text
    input_id = r.json()["id"]
    try:
        wait_for("picture and sound from separate URLs go on air",
                 lambda: api.get("/api/status").json()["current"] == NAME, 30)  # fmt: skip
        wait_for("it plays without failing", lambda: next(
            i for i in api.get("/api/status").json()["inputs"] if i["name"] == NAME
        )["state"] == "on air", 5)  # fmt: skip
    finally:
        api.delete(f"/api/admin/inputs/{input_id}")
        wait_for("removed again", lambda: api.delete("/api/admin/plugins/split-check").status_code == 204)
    print("OK: separate picture and sound work")
    return 0


if __name__ == "__main__":
    sys.exit(main())
