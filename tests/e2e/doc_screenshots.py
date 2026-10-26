"""Screenshots for the documentation (docs/images/), taken from a real installation.

Builds a small scene on a fresh setup – three viewers submit videos, a playlist
and an RTMP/SRT input wait with lower priorities, the lower third stays on –
and photographs the player page, the TV view, the admin page and the "Add
input" dialog. CI runs it on Windows with the installed app and real video;
the pictures are an artifact to look at and copy into docs/images by hand.

    python tests/e2e/doc_screenshots.py http://localhost:8080 --media http://127.0.0.1:8000 \\
        --playlist <folder with the clips> [--out docs-images]

--media      where the demo clips Highlights.mp4, Intro.mp4 and Finale.mp4 are served
--cdp        drive a running browser (local runs, see run_local.py / edge.py)
--simulated  the app runs with --simulate: no video, do not wait for frames
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

PASSWORD = "docs-password-123"
VIEWERS = [("Highlights", "Anna"), ("Intro", "Ben"), ("Finale", "Chris")]
FRAMES_JS = """async () => {
  if (typeof pc === "undefined" || !pc) return 0;
  let frames = 0;
  (await pc.getStats()).forEach((s) => { if (s.type === "inbound-rtp" && s.kind === "video") frames = s.framesDecoded || 0; });
  return frames;
}"""


def step(text: str) -> None:
    print(f"  {text}", flush=True)


def wait_for(what: str, check, timeout_s: float = 120) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(1)
    raise TimeoutError(f"waited {timeout_s:.0f} s for {what}")


def build_scene(base: str, media: str, playlist: str) -> None:
    admin = httpx.Client(base_url=base, timeout=60)
    session = admin.get("/api/admin/session").json()
    if not session["password_set"]:
        assert admin.post("/api/admin/setup", json={"password": PASSWORD}).status_code == 204
    else:
        assert admin.post("/api/admin/login", json={"password": PASSWORD}).status_code == 204
    out = admin.get("/api/admin/outputs").json()[0]
    assert admin.patch(f"/api/admin/outputs/{out['id']}", json={"overlay": "always"}).status_code == 200
    for body in (
        {"name": "Lobby loop", "plugin": "playlist", "priority": 10, "settings": {"items": [playlist]}},
        {"name": "Stage camera", "plugin": "ingest", "priority": 80, "settings": {}},
    ):
        response = admin.post(f"/api/admin/outputs/{out['id']}/inputs", json=body)
        assert response.status_code == 201, response.text
    step("inputs: Lobby loop (playlist), Stage camera (RTMP/SRT)")

    submissions = next(i["id"] for i in out["inputs"] if i["plugin"] == "submissions")
    for title, name in VIEWERS:  # one client each: every viewer has their own device cookie
        response = httpx.post(f"{base}/api/viewer/{submissions}", timeout=120,
                              json={"link": f"{media}/{title}.mp4", "name": name})  # fmt: skip
        assert response.status_code == 200, response.text
        step(f"{name} submitted {title}: {response.json()['message']}")

    def first_video_on_air() -> bool:
        status = httpx.get(f"{base}/api/status", timeout=10).json()
        return status["current"] == "Short-video submissions" and status["now"].get("submitter") == "Anna"

    wait_for("the first submitted video on air", first_video_on_air)
    time.sleep(8)  # past the "Up next" card, into the video
    step("Highlights by Anna is on air")


def take(base: str, out: Path, cdp: str | None, simulated: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp) if cdp else p.chromium.launch(
            channel=os.environ.get("LANSTREAMER_BROWSER", "msedge"),
            args=["--autoplay-policy=no-user-gesture-required"])  # fmt: skip

        def page(width: int, height: int):
            return browser.new_context(viewport={"width": width, "height": height}).new_page()

        def playing(view) -> None:
            if simulated:
                view.wait_for_timeout(3000)
                return
            wait_for("video frames", lambda: view.evaluate(FRAMES_JS) > 30, 60)
            view.wait_for_timeout(1500)

        player = page(1280, 900)
        player.goto(base + "/")
        playing(player)
        player.screenshot(path=out / "player.png")
        step("player.png")

        tv = page(1280, 720)
        tv.goto(base + "/tv")
        tv.mouse.click(640, 360)  # sound on and full window, as a viewer would
        playing(tv)
        tv.wait_for_timeout(2500)  # the pointer hides
        tv.screenshot(path=out / "tv.png")
        step("tv.png")

        admin = page(1280, 900)
        assert admin.request.post(base + "/api/admin/login", data={"password": PASSWORD}).status == 204
        admin.goto(base + "/admin")
        admin.locator("#plugins tr").first.wait_for()
        admin.wait_for_timeout(3000)  # live states settle
        plugins = admin.locator("section").filter(has=admin.locator("#plugins")).bounding_box()
        assert plugins is not None
        admin.screenshot(path=out / "admin.png", full_page=True,
                         clip={"x": 0, "y": 0, "width": 1280, "height": plugins["y"] + plugins["height"] + 16})  # fmt: skip
        step("admin.png")

        admin.click("#add-input")
        admin.select_option("#i-plugin", "youtube")
        admin.fill("#i-name", "Team channel")
        admin.wait_for_timeout(1000)
        admin.locator("#input-dialog").screenshot(path=out / "add-input.png")
        step("add-input.png")
        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--media", required=True)
    parser.add_argument("--playlist", required=True)
    parser.add_argument("--out", type=Path, default=Path("docs-images"))
    parser.add_argument("--cdp")
    parser.add_argument("--simulated", action="store_true")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    build_scene(base, args.media.rstrip("/"), args.playlist)
    take(base, args.out, args.cdp, args.simulated)
    print(f"OK: screenshots in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
