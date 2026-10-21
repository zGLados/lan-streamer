"""End-to-end check of the admin page in headless Microsoft Edge (Playwright).

Runs against a lan-streamer with the stored configuration (Test input A/B) whose
admin password was set by check_api.py. Uses the page like a person would: log
in, add an input through the generated form, watch it go on air, lower its
priority in the table, press "Next input", delete the input, show a log.

    python tests/e2e/check_admin.py http://localhost:8080 [--cdp <DevTools WebSocket URL>]

--cdp connects to a running browser (local runs: the Windows Edge, see edge.py)
instead of launching Edge through Playwright (CI).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import zipfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

PASSWORD = "ci-password-123"  # set by check_api.py
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "hello-plugin"


def step(text: str) -> None:
    print(f"  ok: {text}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--cdp", help="DevTools WebSocket URL of a running browser")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp) if args.cdp else p.chromium.launch(channel="msedge")
        page = browser.new_context(viewport={"width": 1280, "height": 1000}).new_page()
        page.on("console", lambda msg: print(f"  [page] {msg.text}") if msg.type == "error" else None)
        errors: list[str] = []
        page.on("pageerror", lambda exc: (errors.append(str(exc)), print(f"  [page error] {exc}")))
        page.on("dialog", lambda dialog: dialog.accept())  # confirm() on delete
        try:
            page.goto(f"{base}/admin")
            page.fill("#auth-password", PASSWORD)
            page.click("#auth-submit")
            expect(page.locator("#app")).to_be_visible(timeout=10_000)
            expect(page.locator("#inputs tr")).to_have_count(3, timeout=10_000)
            step("logged in, three inputs listed (Test input A/B, Standby)")

            page.click("#add-input")
            page.select_option("#i-plugin", "test-pattern")
            page.fill("#i-name", "UI input")
            page.fill("#i-priority", "70")
            page.locator("#settings select").first.select_option("snow")
            page.click("#input-save")
            expect(page.locator("#input-dialog")).not_to_be_visible(timeout=10_000)
            expect(page.locator("#s-current")).to_have_text("UI input", timeout=20_000)
            expect(page.locator('[data-state="ui-input"]')).to_have_text("on air", timeout=5_000)
            step("added an input through the form, it went on air")

            row = page.locator('#inputs tr[data-id="ui-input"]')
            row.locator('input[type="number"]').fill("0")
            row.locator('input[type="number"]').dispatch_event("change")
            expect(page.locator("#s-current")).not_to_have_text("UI input", timeout=20_000)
            step("lowered its priority in the table, another input took over")

            before = page.locator("#s-current").inner_text()
            page.click("#next")
            expect(page.locator("#s-current")).not_to_have_text(before, timeout=20_000)
            step(f"'Next input' switched from {before} to {page.locator('#s-current').inner_text()}")

            row.locator("button.danger").click()
            expect(page.locator("#inputs tr")).to_have_count(3, timeout=10_000)
            step("deleted the input")

            # Standby: with both test inputs blocked, the fallback goes on air
            for input_id in ("test-a", "test-b"):
                page.locator(f'#inputs tr[data-id="{input_id}"] select').select_option("block")
            expect(page.locator("#s-current")).to_have_text("Standby", timeout=20_000)
            for input_id in ("test-a", "test-b"):
                page.locator(f'#inputs tr[data-id="{input_id}"] select').select_option("none")
            expect(page.locator("#s-current")).not_to_have_text("Standby", timeout=20_000)
            step("blocked both test inputs: Standby went on air; unblocked again")

            # Upload the example plugin, use it, remove it
            archive = Path(tempfile.mkdtemp()) / "hello-plugin.zip"
            with zipfile.ZipFile(archive, "w") as z:
                for file in EXAMPLE.iterdir():
                    z.write(file, file.name)
            page.set_input_files("#plugin-file", str(archive))
            page.click("#plugin-upload")
            expect(page.locator('#plugins tr[data-plugin="hello"]')).to_be_visible(timeout=60_000)
            step("uploaded the example plugin")
            page.click("#add-input")
            page.select_option("#i-plugin", "hello")
            page.fill("#i-name", "Hello")
            page.fill("#i-priority", "90")
            page.click("#input-save")
            expect(page.locator("#s-current")).to_have_text("Hello", timeout=30_000)
            step("an input from the uploaded plugin went on air")
            page.locator('#inputs tr[data-id="hello"] button.danger').click()
            expect(page.locator('#inputs tr[data-id="hello"]')).to_have_count(0, timeout=10_000)
            page.locator('#plugins tr[data-plugin="hello"] button.danger').click()
            expect(page.locator('#plugins tr[data-plugin="hello"]')).to_have_count(0, timeout=10_000)
            step("deleted the input and removed the plugin")

            page.click("#log-refresh")
            expect(page.locator("#log")).to_contain_text("Input removed: UI input", timeout=10_000)
            step("log shown")
            if errors:
                raise AssertionError(f"JavaScript errors on the page: {errors}")
            page.screenshot(path="admin.png", full_page=True)
        except Exception:
            page.screenshot(path="admin.png", full_page=True)
            raise
        finally:
            browser.close()
    print("OK: admin page works")
    return 0


if __name__ == "__main__":
    sys.exit(main())
