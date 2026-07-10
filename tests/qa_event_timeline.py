"""F. EventTimeline frontend QA - Playwright verify"""
import sys

io_setup = sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None
from playwright.sync_api import sync_playwright  # noqa: E402 — stdout 编码须在导入前设置

QA_PROJ = "acce0f17-0d4b-4a88-ae47-1940fc07c1e1"
TOPICS_REPO = "8690021d-4a58-41bb-ad86-4a19f49cd498"
STARS_REPO = "f4e97fd9-e485-4ca5-9b12-d5b87d8fdc81"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(extra_http_headers={"X-Project-Id": QA_PROJ})
        page = context.new_page()
        page.on("console", lambda msg: print(f"[console {msg.type}]: {msg.text}"))

        # Set localStorage active project before navigation
        page.goto("http://localhost:8000/", wait_until="domcontentloaded", timeout=15000)
        page.evaluate(f"""
            localStorage.setItem('activeProjectId', '{QA_PROJ}');
            localStorage.setItem('project.active', '{QA_PROJ}');
        """)

        for name, repo_id in [("topics-manip", TOPICS_REPO), ("stars-manip", STARS_REPO)]:
            url = f"http://localhost:8000/ecosystem/{repo_id}"
            print(f"\n=== Visit {name}: {url} ===")
            try:
                page.goto(url, wait_until="networkidle", timeout=25000)
            except Exception as e:
                print(f"goto error: {e}")
            page.wait_for_timeout(3000)

            screenshot = f"C:/tmp/event_timeline_{name}.png"
            page.screenshot(path=screenshot, full_page=True)
            print(f"screenshot: {screenshot}")

            print(f"  title: {page.title()}")
            print(f"  url: {page.url}")

            content = page.content()
            print(f"  HTML size: {len(content)} chars")
            for keyword in ["事件历史", "EventTimeline", "discovered", "topics_changed", "stars_jumped"]:
                marker = "OK" if keyword in content else "MISSING"
                print(f"  [{marker}] '{keyword}'")

            tabs = page.locator("[role='tab'], button[class*='tab'], .tab-button").all()
            print(f"  found {len(tabs)} tab-like elements")
            for i, t in enumerate(tabs[:15]):
                try:
                    txt = t.inner_text()[:60]
                    print(f"    tab[{i}]: {txt!r}")
                except Exception:
                    pass

            # Try clicking event-history tab
            try:
                event_tab = page.get_by_text("事件历史", exact=False).first
                if event_tab.is_visible(timeout=2000):
                    print("  found 事件历史 tab, clicking...")
                    event_tab.click()
                    page.wait_for_timeout(2000)
                    page.screenshot(path=f"C:/tmp/event_timeline_{name}_tab.png", full_page=True)
                    print(f"  screenshot after click: C:/tmp/event_timeline_{name}_tab.png")
                    # Count event nodes
                    body = page.content()
                    for et in ["discovered", "topics_changed", "stars_jumped"]:
                        cnt = body.count(et)
                        print(f"    event_type '{et}' appears {cnt} times in DOM")
            except Exception as e:
                print(f"  event tab not found: {e}")

        browser.close()

if __name__ == "__main__":
    main()
