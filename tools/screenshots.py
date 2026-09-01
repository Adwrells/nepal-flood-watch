"""Capture the README screenshots from a running console.

The first set of images was taken by hand, which meant nobody could reproduce
them after the UI moved. This script does it the same way every time.

    python launch.py serve            # in one terminal
    python tools/screenshots.py       # in another

Every URL carries #nolive=1. The console holds an open server-sent-events
stream and the charts run a frame timer; either one keeps the page from ever
reaching network- or render-idle, so a naive capture just times out. The flag
turns both off and is read once at load, before the app rewrites the hash.
"""
import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "docs" / "images"
VIEWPORT = {"width": 1600, "height": 1000}


def dismiss_notice(page):
    """The emergency-numbers modal opens over everything on first load."""
    try:
        page.get_by_role("button", name="Continue to the console").click(timeout=4000)
    except Exception:
        pass  # already acknowledged, or the notice was disabled
    page.wait_for_timeout(400)


def shoot(page, name):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path))
    print(f"  {path.relative_to(OUT.parents[1])}  ({path.stat().st_size // 1024} KB)")


def capture(base: str, theme: str = "dark"):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)

        page.goto(f"{base}/#nolive=1", wait_until="networkidle")
        page.wait_for_timeout(2500)
        dismiss_notice(page)

        if theme == "light":
            page.evaluate("document.body.setAttribute('data-theme','light')")
            page.wait_for_timeout(500)

        # --- the charts tab: one small multiple per river --------------------
        page.click("#tab-charts")
        page.wait_for_selector(".chart-card", timeout=20000)
        page.wait_for_timeout(2000)
        shoot(page, "charts-tab")

        # --- the floating window, linear time axis ---------------------------
        # Pick a gauge that publishes a danger mark, otherwise the reference
        # lines the shot is meant to show are simply absent.
        page.evaluate("""
            const card = [...document.querySelectorAll('.chart-card')]
              .find((c) => /danger \\d/.test(c.textContent));
            (card || document.querySelector('.chart-card')).click();
        """)
        page.wait_for_selector("#cw-chart svg path", timeout=20000)
        page.wait_for_timeout(1500)
        shoot(page, "chart-window")

        # --- the same gauge on the telescope axis ----------------------------
        page.click("#cw-scale")
        page.wait_for_timeout(1200)
        shoot(page, "chart-telescope")

        browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8000",
                    help="running console, e.g. http://127.0.0.1:8000")
    ap.add_argument("--theme", default="dark", choices=("dark", "light"))
    args = ap.parse_args()
    try:
        capture(args.base.rstrip("/"), args.theme)
    except Exception as exc:
        print(f"capture failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
