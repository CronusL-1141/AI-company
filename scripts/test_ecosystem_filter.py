"""Probe ecosystem filter UI components."""
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(r"C:/Users/TUF/Desktop/AI团队框架/ai-team-os/docs/screenshots")
URL = "http://localhost:5174/ecosystem"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.goto(URL, wait_until="networkidle", timeout=30000)

        # capture top viewport (filters area)
        page.screenshot(path=str(OUT / "ecosystem-list-top.png"), full_page=False)
        print("saved: ecosystem-list-top.png")

        # Find any visible buttons / select dropdowns / inputs in top region
        all_buttons = page.locator("button:visible").all()
        print(f"Visible buttons: {len(all_buttons)}")
        for i, b in enumerate(all_buttons[:15]):
            try:
                t = b.inner_text() or b.get_attribute("aria-label") or "(empty)"
                print(f"  btn[{i}]: '{t.strip()[:40]}'")
            except Exception:
                pass

        # Look for category / sort / filter selects (might be MUI/AntDesign style)
        selects_or_combos = page.locator("[role='combobox'], [role='listbox'], select").all()
        print(f"Combobox/select elements: {len(selects_or_combos)}")

        # Find tag chips / pill style
        chips = page.locator(".chip, .tag, .pill, [class*='Chip'], [class*='Tag']").all()
        print(f"Chip/tag elements: {len(chips)}")
        for c in chips[:8]:
            try:
                print(f"  chip: '{c.inner_text()[:30]}'")
            except Exception:
                pass

        # Try the search input  (placeholder='搜索仓库名 / owner / ...')
        search_input = page.locator("input[placeholder*='搜索']").first
        if search_input.count() > 0:
            print("Search input found, typing 'memory'...")
            search_input.fill("memory")
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT / "ecosystem-list-search-memory.png"), full_page=True)
            print("saved: ecosystem-list-search-memory.png")
            # count visible repo cards
            cards = page.locator("article, [class*='Card'], [class*='card']").all()
            print(f"Visible cards after search: {len(cards)}")

        # Try finding selector for category/tag filter by inspecting stable selectors
        # Look for divs with text "分类" or "Category" or "标签"
        for txt in ["分类", "类别", "标签", "Tags", "Stars", "排序", "min_stars"]:
            els = page.get_by_text(txt, exact=False).all()
            if els:
                print(f"  text '{txt}': {len(els)} elements")

        browser.close()
    print("DONE")

if __name__ == "__main__":
    main()
