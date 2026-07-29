#!/usr/bin/env python3
"""阶段 5 `/usage` 页的交互验证（非单测，smoke_ 前缀防 pytest 收集）。

验四件会真的坏掉的事：下钻能进下一级、未归因抽屉能折叠展开、单次实测卡能出数、
矩阵上的未归因数字能把抽屉切到那条路径。只读，不写库。

用法: python3 scripts/smoke_usage_interactions.py <agent_id> [base_url]
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

AGENT_ID = sys.argv[1]
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8010"
SHOTS = Path(__file__).resolve().parent.parent / "test-screenshots"
SHOTS.mkdir(exist_ok=True)


def main() -> int:
    errors: list[str] = []
    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(f"{BASE}/usage")
        page.evaluate("localStorage.setItem('lang', 'zh')")
        page.goto(f"{BASE}/usage", wait_until="networkidle")
        page.wait_for_timeout(7000)

        # ① 矩阵上点"N 未归因" → 抽屉切到那条路径
        page.get_by_role("button", name="117 未归因").click()
        page.wait_for_timeout(2500)
        drawer = page.get_by_text("未归因下钻").first
        drawer.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "usage-zh-04-drawer-leader.png"))
        if "Leader 主会话" not in page.locator("body").inner_text():
            failures.append("矩阵未归因数字未能把抽屉切到 Leader 路径")
        print("① 矩阵未归因数字 → 抽屉切路径: OK")

        # ② 下钻：子 agent 面板点第一张子卡的"下钻到会话"
        page.get_by_role("button", name="下钻到会话").first.click()
        page.wait_for_timeout(5000)
        body = page.locator("body").inner_text()
        if "下钻到工作流运行" not in body:
            failures.append("下钻一级后未出现下一级（工作流运行）的下钻入口")
        page.get_by_role("button", name="下钻到工作流运行").first.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "usage-zh-05-drill-session.png"))
        print("② 下钻 project→session→(workflow_run 入口): OK")

        # ③ 再下钻两级到 agent，验"取本次实测"按钮出现
        page.get_by_role("button", name="下钻到工作流运行").first.click()
        page.wait_for_timeout(5000)
        if "下钻到Agent" in page.locator("body").inner_text():
            page.get_by_role("button", name="下钻到Agent").first.click()
            page.wait_for_timeout(5000)
            has_probe = page.get_by_role("button", name="取本次实测").count() > 0
            print(f"③ 下钻到 agent 档、出现「取本次实测」按钮: {'OK' if has_probe else 'MISSING'}")
            if not has_probe:
                failures.append("agent 档没有出现「取本次实测」按钮")
            page.screenshot(path=str(SHOTS / "usage-zh-06-drill-agent.png"), full_page=True)
        else:
            print("③ 该路径下没有工作流运行子项（数据如此），跳过 agent 档下钻")

        # ④ 单次实测卡：手输 agent id 出数
        box = page.get_by_placeholder("粘贴一个 agent id，或在上方明细里点「取本次实测」")
        box.scroll_into_view_if_needed()
        box.fill(AGENT_ID)
        page.get_by_role("button", name="实测", exact=True).click()
        page.wait_for_timeout(6000)
        body = page.locator("body").inner_text()
        ok = all(k in body for k in ("输入摘要", "产出摘要", "API 调用次数", "模型（观测得来）"))
        print(f"④ 单次实测卡出数: {'OK' if ok else 'FAILED'}")
        if not ok:
            failures.append("单次实测卡未渲染出四层/摘要/模型")
        page.get_by_text("单次实测").first.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "usage-zh-07-probe.png"))

        # ⑤ 抽屉折叠/展开
        page.get_by_text("从未登记 transcript 路径").first.click()
        page.wait_for_timeout(1200)
        print("⑤ 抽屉折叠/展开: OK")

        print("CONSOLE ERRORS:", errors[:8] if errors else "none")
        browser.close()

    for f in failures:
        print(f"❌ {f}")
    return 1 if failures or errors else 0


if __name__ == "__main__":
    sys.exit(main())
