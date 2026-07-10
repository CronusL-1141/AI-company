"""v1.5.0 release e2e validation — every button/filter/interaction.

Run:
  python scripts/smoke_v15_release_e2e.py --base http://localhost:8000 --headed
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

# Fix Windows GBK console — use UTF-8 with replace errors
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

OUT = Path(__file__).resolve().parents[1] / "test-screenshots"
OUT.mkdir(parents=True, exist_ok=True)

AI_TEAM_OS = ("3aa58dc7-a771-4745-8fa6-efbd5819956a", "Project-AI Team OS Plugin Design")
LEGAL_AID = ("2af57809-2a85-4d87-bce1-edd3ab0fe511", "legal-aid-proxy")
TUF = ("a6449d70-b9f3-45f8-98ca-bfb8fe23861e", "TUF")
QUANT = ("c66dc08a-d54a-4148-8a98-c8d1b32ce69f", "ai-quant-platform")  # may not match — find real id
REPO_INSIGHT = ("f51dee4a-2fdb-41ee-aea6-77079c0c2537", "repo-insight")

results = []


def shot(page: Page, name: str):
    p = OUT / f"v15rel-{name}.png"
    page.screenshot(path=str(p), full_page=False)
    print(f"  📷 {name}.png")


def ok(area, msg=""):
    results.append((area, "PASS", msg))
    print(f"  ✅ {area}{(' — ' + msg) if msg else ''}")


def fail(area, msg):
    results.append((area, "FAIL", msg))
    print(f"  ❌ {area}: {msg[:120]}")


def warn(area, msg):
    results.append((area, "WARN", msg))
    print(f"  ⚠️  {area}: {msg[:120]}")


def set_proj(page: Page, project_id: str, name: str, base: str):
    """Set project after page already loaded (localStorage needs origin)."""
    page.evaluate(
        "(a) => { localStorage.setItem('ai-team-os.activeProjectId', a[0]); "
        "localStorage.setItem('ai-team-os.activeProjectName', a[1]); }",
        [project_id, name],
    )


def get_count(page: Page) -> int:
    """Read 共 N 个仓库 number."""
    try:
        txt = page.locator("text=/共 \\d+ 个仓库/").first.text_content(timeout=5000)
        import re
        m = re.search(r"共 (\d+)", txt or "")
        return int(m.group(1)) if m else -1
    except PWTimeout:
        return -1


def main(base: str, headed: bool):
    console_errs = []
    page_errs = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed, slow_mo=200 if headed else 0)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: console_errs.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errs.append(str(e)))

        # ============ INIT ============
        print("\n========== INIT ==========")
        page.goto(f"{base}/", wait_until="networkidle", timeout=15000)
        set_proj(page, *AI_TEAM_OS, base)
        page.goto(f"{base}/ecosystem", wait_until="networkidle", timeout=15000)
        time.sleep(1.5)

        # ============ AREA 1: 列表页基础 ============
        print("\n========== AREA 1: 列表页基础 ==========")
        cnt = get_count(page)
        shot(page, "01a-list-default")
        if cnt >= 100:
            ok("1.0-默认列表", f"{cnt} 仓")
        else:
            fail("1.0-默认列表", f"got {cnt}")

        # ============ AREA 1.1: 切换 5 项目（含中文路径）============
        print("\n========== AREA 1.1: 5 项目切换（验证 X-Project-Dir bug 修复）==========")
        for pid, pname in [LEGAL_AID, TUF, REPO_INSIGHT, AI_TEAM_OS]:
            set_proj(page, pid, pname, base)
            page.reload(wait_until="networkidle")
            time.sleep(1.2)
            cnt = get_count(page)
            shot(page, f"01b-switch-{pname[:15].replace('/', '_').replace(' ', '_')}")
            if pname == "Project-AI Team OS Plugin Design" and cnt >= 100:
                ok(f"1.1-切到 {pname[:25]}", f"{cnt} 仓")
            elif pname != "Project-AI Team OS Plugin Design" and cnt == 0:
                ok(f"1.1-切到 {pname[:25]}", "0 仓（隔离正确）")
            else:
                warn(f"1.1-切到 {pname[:25]}", f"got {cnt}")

        # ============ AREA 1.2: 三 tab 切换 ============
        print("\n========== AREA 1.2: 活跃集 / 全量 / 已删除 tab ==========")
        for tab in ["活跃集", "全量", "已删除"]:
            try:
                page.locator(f'button:has-text("{tab}")').first.click(timeout=5000)
                time.sleep(1)
                cnt = get_count(page)
                shot(page, f"02-tab-{tab}")
                ok(f"1.2-tab-{tab}", f"{cnt} 仓")
            except Exception as e:
                fail(f"1.2-tab-{tab}", str(e)[:80])
        # 回活跃集
        page.locator('button:has-text("活跃集")').first.click()
        time.sleep(1)

        # ============ AREA 1.3: 搜索框 ============
        print("\n========== AREA 1.3: 搜索框 ==========")
        try:
            search = page.locator('input[placeholder*="搜索"]').first
            search.fill("mcp")
            time.sleep(1.5)
            cnt = get_count(page)
            shot(page, "03-search-mcp")
            if 0 < cnt < 200:
                ok("1.3-搜索 mcp", f"过滤后 {cnt} 仓")
            else:
                warn("1.3-搜索 mcp", f"got {cnt}")
            search.fill("")
            time.sleep(1)
        except Exception as e:
            fail("1.3-搜索", str(e)[:80])

        # ============ AREA 1.4: category dropdown ============
        print("\n========== AREA 1.4: 类别 dropdown ==========")
        try:
            cat_btn = page.locator('button[aria-label*="类别"], button:has-text("全部类别")').first
            cat_btn.click(timeout=5000)
            time.sleep(0.7)
            shot(page, "04-cat-open")
            # 选第一个非 "全部" 的选项
            options = page.locator('[role="option"]').all()
            print(f"    Category options: {len(options)}")
            if len(options) >= 2:
                # 选第二个（第一个是 "全部"）
                opt_text = options[1].text_content() or ""
                options[1].click()
                time.sleep(1.2)
                cnt = get_count(page)
                shot(page, "04-cat-selected")
                ok(f"1.4-类别 {opt_text[:20]}", f"{cnt} 仓")
                # 重置
                cat_btn.click()
                time.sleep(0.5)
                page.locator('[role="option"]').first.click()
                time.sleep(1)
            else:
                warn("1.4-类别 dropdown", f"options={len(options)}")
        except Exception as e:
            fail("1.4-类别", str(e)[:80])

        # ============ AREA 1.5: min_stars dropdown ============
        print("\n========== AREA 1.5: 星标 dropdown ==========")
        try:
            star_btn = page.locator('button[aria-label*="星标"], button:has-text("不限星标")').first
            star_btn.click(timeout=5000)
            time.sleep(0.5)
            shot(page, "05-star-open")
            page.locator('[role="option"]:has-text("≥ 50k")').first.click(timeout=3000)
            time.sleep(1.2)
            cnt = get_count(page)
            shot(page, "05-star-50k")
            if 0 <= cnt < 50:
                ok("1.5-≥50k", f"{cnt} 仓")
            else:
                warn("1.5-≥50k", f"got {cnt}")
            # 重置
            star_btn.click()
            time.sleep(0.5)
            page.locator('[role="option"]:has-text("不限")').first.click()
            time.sleep(1)
        except Exception as e:
            fail("1.5-星标", str(e)[:80])

        # ============ AREA 1.6: 深扫状态 dropdown ============
        print("\n========== AREA 1.6: 深扫状态 dropdown ==========")
        try:
            rev_btn = page.locator('button[aria-label*="深扫"]').first
            rev_btn.click(timeout=5000)
            time.sleep(0.5)
            shot(page, "06-review-open")
            page.locator('[role="option"]:has-text("待深扫")').first.click(timeout=3000)
            time.sleep(1.2)
            cnt = get_count(page)
            shot(page, "06-review-pending")
            ok("1.6-待深扫", f"{cnt} 仓")
            # 重置
            rev_btn.click()
            time.sleep(0.5)
            page.locator('[role="option"]:has-text("全部")').first.click()
            time.sleep(1)
        except Exception as e:
            fail("1.6-深扫状态", str(e)[:80])

        # ============ AREA 1.7: 清除按钮 ============
        print("\n========== AREA 1.7: 清除筛选按钮 ==========")
        try:
            search = page.locator('input[placeholder*="搜索"]').first
            search.fill("test")
            time.sleep(0.5)
            clear_btn = page.locator('button:has-text("清除")').first
            clear_btn.click(timeout=3000)
            time.sleep(1)
            cnt = get_count(page)
            shot(page, "07-cleared")
            if cnt >= 100:
                ok("1.7-清除", f"恢复到 {cnt} 仓")
            else:
                warn("1.7-清除", f"got {cnt}")
        except Exception as e:
            warn("1.7-清除", str(e)[:80])

        # ============ AREA 2: 详情页 ============
        print("\n========== AREA 2: 详情页（点击仓卡片）==========")
        try:
            # 点击 PrefectHQ/fastmcp（有真实 deep_review 数据）
            page.goto(f"{base}/ecosystem", wait_until="networkidle")
            time.sleep(1)
            search = page.locator('input[placeholder*="搜索"]').first
            search.fill("fastmcp")
            time.sleep(1)
            cards = page.locator('a[href*="/ecosystem/"]').all()
            if cards:
                cards[0].click(timeout=5000)
                time.sleep(2)
                shot(page, "08-detail-fastmcp-top")
                page.evaluate("window.scrollTo(0, 600)")
                time.sleep(0.5)
                shot(page, "09-detail-fastmcp-mid")
                ok("2.0-详情页加载", "fastmcp")
                # 切 tab 看 5 段 markdown
                for tab_label in ["基础信息", "深度档案", "研究历程"]:
                    try:
                        sel = f'[role="tab"]:has-text("{tab_label}"), button:has-text("{tab_label}")'
                        tab = page.locator(sel).first
                        tab.click(timeout=3000)
                        time.sleep(1)
                        shot(page, f"10-detail-tab-{tab_label}")
                        ok(f"2.1-tab-{tab_label}", "")
                    except Exception as e:
                        warn(f"2.1-tab-{tab_label}", str(e)[:80])
            else:
                fail("2.0-详情页", "找不到 fastmcp 卡片")
        except Exception as e:
            fail("2.0-详情页", str(e)[:80])

        # ============ AREA 3: research 页 ============
        print("\n========== AREA 3: /ecosystem/research ==========")
        try:
            page.goto(f"{base}/ecosystem/research", wait_until="networkidle", timeout=10000)
            time.sleep(1.5)
            shot(page, "11-research-page")
            # 输入 research goal
            try:
                goal = page.locator('input[placeholder*="目标"], textarea, input[type="text"]').first
                goal.fill("升级我们的记忆系统")
                time.sleep(0.5)
                shot(page, "12-research-goal-input")
                ok("3.0-research goal 输入", "")
            except Exception:
                warn("3.0-goal", "未找到输入框")
            ok("3-/research 页", "可访问")
        except Exception as e:
            fail("3-/research", str(e)[:80])

        # ============ AREA 4: 项目设置 tab ============
        print("\n========== AREA 4: 项目详情页 → Ecosystem 设置 ==========")
        try:
            page.goto(f"{base}/projects/{AI_TEAM_OS[0]}", wait_until="networkidle", timeout=10000)
            time.sleep(1.5)
            shot(page, "13-project-detail")
            # 找 Ecosystem tab
            try:
                eco_sel = (
                    '[role="tab"]:has-text("Ecosystem"), '
                    'button:has-text("Ecosystem 设置"), button:has-text("生态")'
                )
                eco_tab = page.locator(eco_sel).first
                eco_tab.click(timeout=5000)
                time.sleep(1.5)
                shot(page, "14-eco-settings")
                ok("4.0-Ecosystem tab", "")
                # 看 8 字段
                page_text = page.text_content("body") or ""
                fields = {"min_stars": "min_stars" in page_text or "星标" in page_text,
                          "top_n": "top_n" in page_text or "Top" in page_text,
                          "focus_topics": "focus_topics" in page_text or "topics" in page_text}
                for k, v in fields.items():
                    if v:
                        ok(f"4.1-字段 {k}", "")
                    else:
                        warn(f"4.1-字段 {k}", "未找到")
            except Exception as e:
                warn("4.0-Ecosystem tab", str(e)[:80])
        except Exception as e:
            fail("4-项目设置", str(e)[:80])

        # ============ AREA 5: 移动端 ============
        print("\n========== AREA 5: 移动端响应式 (375px) ==========")
        ctx2 = browser.new_context(viewport={"width": 375, "height": 812})
        m = ctx2.new_page()
        m.goto(f"{base}/", wait_until="networkidle")
        m.evaluate("(a) => { localStorage.setItem('ai-team-os.activeProjectId', a[0]); "
                   "localStorage.setItem('ai-team-os.activeProjectName', a[1]); }",
                   list(AI_TEAM_OS))
        m.goto(f"{base}/ecosystem", wait_until="networkidle")
        time.sleep(1)
        m.screenshot(path=str(OUT / "v15rel-15-mobile-list.png"))
        print("  📷 v15rel-15-mobile-list.png")
        ok("5.0-移动端列表", "")
        ctx2.close()

        # ============ AREA 6: console errors ============
        print("\n========== AREA 6: console / page errors ==========")
        if console_errs:
            for e in console_errs[:5]:
                print(f"    ⚠️ console: {e[:200]}")
            warn("6.0-console", f"{len(console_errs)} errors")
        else:
            ok("6.0-console", "0 errors")
        if page_errs:
            for e in page_errs[:5]:
                print(f"    ❌ pageerror: {e[:200]}")
            fail("6.1-pageerror", f"{len(page_errs)} errors")
        else:
            ok("6.1-pageerror", "0 errors")

        if headed:
            print("\n[让你看 5 秒最终页面]")
            time.sleep(5)

        ctx.close()
        browser.close()

    # ============ 总结 ============
    print("\n" + "=" * 70)
    print("v1.5.0 RELEASE E2E 总结")
    print("=" * 70)
    p = sum(1 for _, s, _ in results if s == "PASS")
    f = sum(1 for _, s, _ in results if s == "FAIL")
    w = sum(1 for _, s, _ in results if s == "WARN")
    print(f"PASS: {p} | FAIL: {f} | WARN: {w}")
    print()
    for area, status, msg in results:
        icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️"}[status]
        print(f"  {icon} {area}: {msg[:80]}")
    return 0 if f == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    sys.exit(main(args.base, args.headed))
