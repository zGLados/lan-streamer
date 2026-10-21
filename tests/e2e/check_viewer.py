"""End-to-end check of the viewer side: submitting a video on the player page, the /tv page.

Uses the "Short-video submissions" input of a fresh setup (password from
check_api.py), submits a link through the form on the player page like a
viewer, waits until the video is on air with "submitted by …", submits one
without a name, and opens /tv.

    python tests/e2e/check_viewer.py http://localhost:8080 --link <video URL> [--cdp URL] [--webrtc]

--link     a video yt-dlp can read (CI: the test clip over HTTP; local simulated runs: anything)
--webrtc   also require decoded video frames on /tv (real pipeline only)
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx
from playwright.sync_api import expect, sync_playwright

PASSWORD = "ci-password-123"
STATS_JS = """async () => {
  if (typeof pc === "undefined" || !pc) return 0;
  let frames = 0;
  (await pc.getStats()).forEach((s) => { if (s.type === "inbound-rtp" && s.kind === "video") frames = s.framesDecoded || 0; });
  return frames;
}"""


def step(text: str) -> None:
    print(f"  ok: {text}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--link", required=True)
    parser.add_argument("--cdp")
    parser.add_argument("--webrtc", action="store_true")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    api = httpx.Client(base_url=base, timeout=60)
    assert api.post("/api/admin/login", json={"password": PASSWORD}).status_code == 204
    inputs = api.get("/api/admin/outputs").json()[0]["inputs"]
    input_id = next(i["id"] for i in inputs if i["plugin"] == "submissions")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp) if args.cdp else p.chromium.launch(
            channel="msedge", args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(viewport={"width": 1280, "height": 1000}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: (errors.append(str(exc)), print(f"  [page error] {exc}")))

        page.goto(base + "/")
        form = page.locator(f'form[data-input="{input_id}"]')
        expect(form).to_be_visible(timeout=30_000)
        expect(page.locator(f"#items-{input_id}")).to_contain_text("Nothing waiting", timeout=10_000)
        step("the submit form is on the player page")

        form.locator('input[name="link"]').fill(args.link)
        form.locator('input[name="name"]').fill("CI viewer")
        form.locator('button[type="submit"]').click()
        expect(form.locator(".msg")).to_contain_text("Added", timeout=60_000)
        expect(page.locator(f"#items-{input_id}")).to_contain_text("by CI viewer", timeout=10_000)
        step("submitted a link, it is in the queue")

        expect(page.locator("#now")).to_contain_text("submitted by CI viewer", timeout=90_000)
        expect(page.locator("#current")).to_have_text("Short-video submissions", timeout=10_000)
        step("the submitted video is on air, the page shows who submitted it")

        form.locator('input[name="link"]').fill(args.link + "?again")
        form.locator('input[name="name"]').fill("")
        form.locator('button[type="submit"]').click()
        expect(form.locator(".msg.ok")).to_contain_text("Added", timeout=60_000)
        step("submitted without a name")

        form.locator('input[name="link"]').fill("https://example.invalid/not-a-video")
        form.locator('button[type="submit"]').click()
        expect(form.locator(".msg.bad")).to_be_visible(timeout=60_000)
        step(f"a bad link is refused: {form.locator('.msg').inner_text()!r}")

        tv = browser.new_context(viewport={"width": 1280, "height": 720}).new_page()
        tv.on("pageerror", lambda exc: (errors.append(str(exc)), print(f"  [tv error] {exc}")))
        tv.goto(base + "/tv")
        expect(tv.locator("video")).to_be_visible(timeout=10_000)
        text = tv.evaluate("document.body.innerText.trim()")
        assert text == "", f"the TV page shows text: {text!r}"
        if args.webrtc:
            deadline = time.monotonic() + 40
            frames = 0
            while time.monotonic() < deadline and frames < 120:
                tv.wait_for_timeout(1000)
                frames = tv.evaluate(STATS_JS)
            assert frames >= 120, f"only {frames} frames on /tv"
            step(f"/tv shows only the video ({frames} frames decoded)")
        else:
            step("/tv shows only the video element")
        tv.screenshot(path="tv.png")
        page.screenshot(path="viewer.png", full_page=True)
        if errors:
            raise AssertionError(f"JavaScript errors: {errors}")
        browser.close()
    print("OK: viewer side works")
    return 0


if __name__ == "__main__":
    sys.exit(main())
