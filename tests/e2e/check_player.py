"""End-to-end check of the player page: does WebRTC deliver picture and sound?

Opens the page in headless Microsoft Edge (Playwright) and waits until the
browser has decoded enough full-HD video frames and received audio.

    python tests/e2e/check_player.py http://localhost:8080 [--frames 120] [--timeout 60]
"""

from __future__ import annotations

import argparse
import sys
import time

from playwright.sync_api import sync_playwright

STATS_JS = """async () => {
  if (typeof pc === "undefined" || !pc) return null;
  const out = {};
  (await pc.getStats()).forEach((s) => {
    if (s.type === "inbound-rtp") out[s.kind] = s;
  });
  return {
    state: pc.connectionState,
    frames: out.video?.framesDecoded ?? 0,
    width: out.video?.frameWidth ?? 0,
    height: out.video?.frameHeight ?? 0,
    fps: out.video?.framesPerSecond ?? 0,
    audioPackets: out.audio?.packetsReceived ?? 0,
  };
}"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--channel", default="msedge")
    args = parser.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel=args.channel, args=["--autoplay-policy=no-user-gesture-required"]
        )
        page = browser.new_page()
        page.on("console", lambda msg: print(f"  [page] {msg.text}"))
        deadline = time.monotonic() + args.timeout
        loaded = False
        while time.monotonic() < deadline:
            try:
                if not loaded:
                    page.goto(args.url)
                    loaded = True
                stats = page.evaluate(STATS_JS)
            except Exception as exc:  # noqa: BLE001 – the app may still be starting
                print(f"  waiting: {exc.__class__.__name__}")
                page.wait_for_timeout(2000)
                continue
            print(f"  {stats}")
            if (
                stats
                and stats["frames"] >= args.frames
                and (stats["width"], stats["height"]) == (1920, 1080)
                and stats["audioPackets"] > 0
            ):
                page.screenshot(path="player.png")
                print(f"OK: {stats['frames']} frames {stats['width']}x{stats['height']} decoded")
                browser.close()
                return 0
            page.wait_for_timeout(2000)
        page.screenshot(path="player.png")
        browser.close()
    print(f"FAILED: no full-HD video with audio in the browser within {args.timeout:.0f} s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
