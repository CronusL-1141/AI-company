#!/usr/bin/env python3
"""阶段 5 `/usage` 页的浏览器活体验证脚本（非单测，故用 smoke_ 前缀防 pytest 收集）。

只读：打开页面、点几下、截图。不写库、不改任何状态。
中英各跑一遍 —— 页面全量走 i18n，只验一种语言等于没验另一种。

用法: python3 scripts/smoke_usage_page.py [base_url]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as PlaywrightError
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


# 抽屉的结构化读数：选中的按钮、每个原因码的计数与样例 id。
DRAWER_JS = """() => {
  const titles = ['未归因下钻', 'Unattributed Drill-down'];
  let card = [...document.querySelectorAll('div')].find(d => titles.includes(d.textContent.trim()));
  while (card && !(card.querySelector('[role=group]') && card.querySelector('div.rounded.border button code'))) {
    card = card.parentElement;
  }
  if (!card) return null;
  const pressed = [...card.querySelectorAll('[role=group] button[aria-pressed=true]')].map(b => b.innerText);
  const blocks = [];
  for (const b of card.querySelectorAll('div.rounded.border')) {
    const code = b.querySelector('button code');
    const count = b.querySelector('button span.text-lg');
    if (!code || !count) continue;
    blocks.push({
      code: code.textContent,
      count: Number(count.textContent.replace(/,/g, '')),
      ids: [...b.querySelectorAll('li code')].map(c => c.textContent),
    });
  }
  return {pressed, blocks};
}"""


def check_drawer_follows_row(page, coverage: dict, sample_calls: list[tuple[dict, dict]]) -> list[str]:
    """逐行点矩阵的「N 未归因」，抽屉必须显示被点的那一行（4d87eba2）。

    同一条 path 按 harness 分成多行，抽屉曾按 path 取第一行：点哪一行都显示 codex
    那一桶，样例也不按 harness 过滤。判据：各原因码计数与该行逐码相等（之和即该行
    未归因数）、只有一个按钮处于选中态、样例请求带着该行的 harness 且样例全部同桶。
    """
    failures: list[str] = []
    rows = [
        r for r in coverage["rows"]
        if r["dispatches_total"] is not None and r["dispatches_total"] > (r["dispatches_attributed"] or 0)
    ]
    buttons = page.locator("table button").filter(has_text=re.compile("未归因|unattributed"))
    if buttons.count() != len(rows):
        return [f"矩阵上「N 未归因」按钮 {buttons.count()} 个，未归因行 {len(rows)} 行"]
    for i, row in enumerate(rows):
        label = f"{row['path']}/{row['harness'] or '未标注'}"
        buttons.nth(i).click()
        page.wait_for_timeout(2500)
        drawer = page.evaluate(DRAWER_JS)
        if drawer is None:
            failures.append(f"{label}: 找不到抽屉")
            continue
        shown = {b["code"]: b["count"] for b in drawer["blocks"]}
        want = row["dispatches_total"] - row["dispatches_attributed"]
        if shown != row["unattributed_reasons"] or sum(shown.values()) != want:
            failures.append(
                f"{label}: 抽屉 {shown}（和 {sum(shown.values())}），"
                f"该行 {row['unattributed_reasons']}（{want}）"
            )
        if len(drawer["pressed"]) != 1:
            failures.append(f"{label}: 选中态按钮 {drawer['pressed']}")
        ids = {aid for b in drawer["blocks"] for aid in b["ids"]}
        if row["path"] not in ("subagent", "leader_session"):
            continue
        expected_param = row["harness"] if row["harness"] is not None else "unlabelled"
        # 样例请求按 (path, harness) 缓存：默认选中的那一行在首屏就取过，点击不会再发
        calls = [
            body for params, body in sample_calls
            if params.get("population") == [row["path"]] and params.get("harness") == [expected_param]
        ]
        if not calls:
            failures.append(f"{label}: 没有发出 harness={expected_param} 的样例请求")
            continue
        returned = {s["agent_id"]: s["harness"] for rows_ in calls[-1]["samples"].values() for s in rows_}
        wrong = {aid: h for aid, h in returned.items() if h != row["harness"]}
        if wrong:
            failures.append(f"{label}: 样例混入别的 harness {wrong}")
        if not ids <= set(returned):
            failures.append(f"{label}: 抽屉显示的样例不在该行的样例响应里 {sorted(ids - set(returned))}")
        print(f"  抽屉 {label}: 计数 {shown} / 样例 {len(ids)} 行，harness 全为 {row['harness']}")
    return failures


def launch(p):
    """Playwright 自带的 chromium 没装（或随 playwright 升级失配）时退到本机 Chrome。"""
    try:
        return p.chromium.launch(headless=True)
    except PlaywrightError:
        return p.chromium.launch(channel="chrome", headless=True)


def run(lang: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    coverage: dict = {}
    sample_calls: list[tuple[dict, dict]] = []

    def on_response(resp) -> None:
        if "/api/usage/coverage" in resp.url:
            coverage.update(resp.json()["data"])
        elif "/api/usage/unattributed" in resp.url:
            sample_calls.append((parse_qs(urlparse(resp.url).query, keep_blank_values=True), resp.json()["data"]))

    with sync_playwright() as p:
        browser = launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("response", on_response)
        # 界面语言以服务端设置为准（localStorage 只是缓存）。只桩读取、不写设置：
        # 否则某一种语言的这一遍在本机设置下永远跑不到。
        page.route(
            "**/api/settings/language*",
            lambda route: route.fulfill(
                json={"mode": lang, "effective": lang, "source": "user"}
            )
            if route.request.method == "GET"
            else route.abort(),
        )

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

        drawer_failures = check_drawer_follows_row(page, coverage, sample_calls)
        for failure in drawer_failures:
            print(f"  [{lang}] *** 抽屉 *** {failure}")
        missing += drawer_failures

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
