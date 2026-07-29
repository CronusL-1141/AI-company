#!/usr/bin/env python3
"""阶段 5 `/usage` 页的浏览器活体验证脚本（非单测，故用 smoke_ 前缀防 pytest 收集）。

只读：打开页面、点几下、截图。不写库、不改任何状态。
中英各跑一遍 —— 页面全量走 i18n，只验一种语言等于没验另一种。

用法: python3 scripts/smoke_usage_page.py [base_url]
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
SHOTS = Path(__file__).resolve().parent.parent / "test-screenshots"
SHOTS.mkdir(exist_ok=True)

MUST_APPEAR = {
    "zh": [
        "覆盖率矩阵",
        "未归因下钻",
        "已归因明细",
        "单次实测",
        "口径说明",
        "usage_sum",
        "ctx_last",
        "设计上不采集",
        "无合计行",
        "四层分列，无合计字段",
        "单次实测，非全量台账",
        "Leader 主会话",
        "no_transcript_path",
        "not_yet_measured",
        "by_design",
    ],
    "en": [
        "Coverage Matrix",
        "Unattributed Drill-down",
        "Attributed Detail",
        "Single Measurement",
        "Metric Definitions",
        "usage_sum",
        "ctx_last",
        "No total row",
        "Four layers, split; no total field",
        "Single measurement, not a full ledger",
        "Leader main session",
    ],
}


def run(lang: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(f"{BASE}/usage")
        page.evaluate(f"localStorage.setItem('lang', '{lang}')")
        page.goto(f"{BASE}/usage", wait_until="networkidle")
        page.wait_for_timeout(7000)

        page.screenshot(path=str(SHOTS / f"usage-{lang}-01-firstscreen.png"))
        page.screenshot(path=str(SHOTS / f"usage-{lang}-02-full.png"), full_page=True)

        body = page.locator("body").inner_text()
        missing = [probe for probe in MUST_APPEAR[lang] if probe not in body]
        for probe in MUST_APPEAR[lang]:
            print(f"  [{lang}] {probe}: {'YES' if probe in body else '*** MISSING ***'}")

        # 移动端断点（375px）—— 表格与并排面板必须不横向溢出
        if lang == "zh":
            page.set_viewport_size({"width": 375, "height": 900})
            page.wait_for_timeout(1500)
            page.screenshot(path=str(SHOTS / "usage-zh-03-mobile375.png"), full_page=True)
            overflow = page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            print(f"  [zh] 375px 横向溢出: {overflow}px")

        browser.close()
    return missing, errors


def main() -> int:
    bad = False
    for lang in ("zh", "en"):
        missing, errors = run(lang)
        print(f"[{lang}] CONSOLE ERRORS:", errors[:8] if errors else "none")
        if missing or errors:
            bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
