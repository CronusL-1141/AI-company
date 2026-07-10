"""Frontend validation for ecosystem dashboard. Saves 5+ screenshots."""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(r"C:/Users/TUF/Desktop/AI团队框架/ai-team-os/docs/screenshots")
URL = "http://localhost:5174/ecosystem"
BUGS = []

def log(msg):
    print(msg, flush=True)

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Desktop
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        console_msgs = []
        page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: BUGS.append(f"PAGE ERROR: {e}"))

        log("Loading ecosystem list page...")
        t0 = time.time()
        page.goto(URL, wait_until="networkidle", timeout=30000)
        load_time = time.time() - t0
        log(f"  loaded in {load_time:.2f}s")

        page.screenshot(path=str(OUT / "ecosystem-list-desktop.png"), full_page=True)
        log("  screenshot: ecosystem-list-desktop.png")

        # find repo cards
        body_text = page.inner_text("body")
        log(f"  body length: {len(body_text)} chars")

        # Try finding repo count indicator
        count_indicators = []
        for sel in ["[data-testid='repo-count']", ".repo-count", "h1", "h2", ".total"]:
            els = page.locator(sel).all()
            for e in els[:3]:
                try:
                    t = e.inner_text()
                    if t and len(t) < 100:
                        count_indicators.append(f"{sel}: '{t}'")
                except Exception:
                    pass
        log("  Headers/counts found:")
        for c in count_indicators[:5]:
            log(f"    {c}")

        # Try filter
        log("Looking for filter inputs...")
        inputs = page.locator("input").all()
        selects = page.locator("select").all()
        buttons = page.locator("button").all()
        log(f"  inputs={len(inputs)} selects={len(selects)} buttons={len(buttons)}")
        for i, inp in enumerate(inputs[:5]):
            try:
                ph = inp.get_attribute("placeholder") or ""
                nm = inp.get_attribute("name") or ""
                log(f"    input[{i}]: placeholder='{ph}' name='{nm}'")
            except Exception:
                pass

        # Look for tag filters / pill buttons
        tag_btns = []
        for txt in ["memory_system", "memory", "Memory", "mcp_server", "mcp_framework"]:
            try:
                els = page.get_by_text(txt, exact=False).all()
                if els:
                    tag_btns.append((txt, len(els)))
            except Exception:
                pass
        log(f"  tag-related elements: {tag_btns}")

        # Try clicking filter for memory_system if found
        try:
            mem_btn = page.get_by_text("memory_system", exact=False).first
            if mem_btn:
                log("  clicking memory_system filter...")
                mem_btn.click()
                page.wait_for_timeout(2000)
                page.screenshot(path=str(OUT / "ecosystem-list-filtered-memory.png"), full_page=True)
                log("  screenshot: ecosystem-list-filtered-memory.png")
        except Exception as e:
            log(f"  filter click failed: {e}")
            BUGS.append(f"Filter click failure: {e}")

        # navigate back to base
        page.goto(URL, wait_until="networkidle")

        # Try finding a repo link/card and clicking it
        log("Looking for repo links to click for detail page...")
        # likely links to /ecosystem/<id> or /ecosystem/<full_name>
        links = page.locator("a").all()
        log(f"  total links: {len(links)}")
        for i, lnk in enumerate(links[:30]):
            try:
                href = lnk.get_attribute("href") or ""
                if "/ecosystem/" in href and href != "/ecosystem" and href != "/ecosystem/":
                    log(f"  link[{i}] -> {href}")
                    lnk.click()
                    page.wait_for_load_state("networkidle", timeout=15000)
                    page.screenshot(path=str(OUT / "ecosystem-detail-desktop.png"), full_page=True)
                    log("  screenshot: ecosystem-detail-desktop.png")
                    break
            except Exception as e:
                log(f"  link[{i}] failed: {e}")

        # Mobile viewport
        log("Switching to mobile viewport (375x667)...")
        ctx.close()
        ctx = browser.new_context(viewport={"width": 375, "height": 667})
        page = ctx.new_page()
        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.screenshot(path=str(OUT / "ecosystem-list-mobile.png"), full_page=True)
        log("  screenshot: ecosystem-list-mobile.png")

        log("\nConsole messages captured:")
        for m in console_msgs[:20]:
            log(f"  {m}")
        log(f"  (total {len(console_msgs)} messages)")

        log("\nBugs collected:")
        for b in BUGS:
            log(f"  - {b}")

        browser.close()
    log("DONE")

if __name__ == "__main__":
    main()
