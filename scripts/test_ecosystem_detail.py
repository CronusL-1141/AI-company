"""Capture detail page for fastmcp (has deep_review)."""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(r"C:/Users/TUF/Desktop/AI团队框架/ai-team-os/docs/screenshots")
FASTMCP_ID = "6d87c65c-7ebd-4d39-8f9c-2d34388fe58c"
URL_DETAIL = f"http://localhost:5174/ecosystem/{FASTMCP_ID}"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 1200})
    page = ctx.new_page()
    page.goto(URL_DETAIL, wait_until="networkidle", timeout=30000)
    time.sleep(1)
    # Capture above-the-fold view
    page.screenshot(path=str(OUT / "ecosystem-detail-fastmcp-top.png"), full_page=False)
    print("saved: ecosystem-detail-fastmcp-top.png")
    # Full page
    page.screenshot(path=str(OUT / "ecosystem-detail-fastmcp-full.png"), full_page=True)
    print("saved: ecosystem-detail-fastmcp-full.png")

    # Inspect detail content
    body = page.inner_text("body")
    print(f"body len: {len(body)}")

    # check key sections
    for key in ["fastmcp", "深度审查", "深扫", "5", "PrefectHQ", "Apache", "report_id"]:
        if key in body:
            print(f"  found '{key}': YES")
        else:
            print(f"  found '{key}': no")

    # check links
    links = page.locator("a").all()
    extlinks = []
    for lk in links[:50]:
        try:
            h = lk.get_attribute("href") or ""
            if h.startswith("http"):
                extlinks.append(h)
        except Exception:
            pass
    print(f"External links: {len(extlinks)}")
    for h in extlinks[:5]:
        print(f"  {h}")

    browser.close()
print("DONE")
