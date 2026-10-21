"""End-to-end check of the admin page in headless Microsoft Edge (Playwright).

Runs against a lan-streamer with the stored configuration (Test input A/B) whose
admin password was set by check_api.py. Uses the page like a person would: log
in, add an input through the generated form, watch it go on air, lower its
priority in the table, press "Next input", delete the input, show a log.

    python tests/e2e/check_admin.py http://localhost:8080
"""

from __future__ import annotations

import sys

from playwright.sync_api import expect, sync_playwright

PASSWORD = "ci-password-123"  # set by check_api.py


def step(text: str) -> None:
    print(f"  ok: {text}", flush=True)


def main() -> int:
    base = sys.argv[1].rstrip("/")
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.on("console", lambda msg: print(f"  [page] {msg.text}") if msg.type == "error" else None)
        page.on("dialog", lambda dialog: dialog.accept())  # confirm() on delete
        try:
            page.goto(f"{base}/admin")
            page.fill("#auth-password", PASSWORD)
            page.click("#auth-submit")
            expect(page.locator("#app")).to_be_visible(timeout=10_000)
            expect(page.locator("#inputs tr")).to_have_count(2, timeout=10_000)
            step("logged in, two inputs listed")

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
            expect(page.locator("#inputs tr")).to_have_count(2, timeout=10_000)
            step("deleted the input")

            page.click("#log-refresh")
            expect(page.locator("#log")).to_contain_text("Input removed: UI input", timeout=10_000)
            step("log shown")
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
