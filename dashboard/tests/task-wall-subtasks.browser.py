"""Read-only browser regression against dashboard_snapshot_preview.py.

Pass --url, --database, --project-id and --parent-id for a private snapshot
containing an open parent with running and completed children. Task responses
come from production HTTP routes; only the language preference is overridden.
Run with testlock run --kind playwright --lane targeted -- python3.12 ...
"""

import argparse
import json
import re
import sqlite3
import urllib.request
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--parent-id", required=True)
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[2] / "test-screenshots"
    output.mkdir(exist_ok=True)

    def get(path):
        with urllib.request.urlopen(args.url + path, timeout=30) as response:
            assert response.headers.get("X-Aiteam-Preview") == "isolated-snapshot"
            return json.load(response)

    prefix = f"/api/projects/{args.project_id}/task-wall"
    wall = get(prefix + "?limit=500&fields=card")
    digest = wall["digest"]
    now = datetime.fromisoformat(digest["as_of"].replace("Z", "+00:00"))
    with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 20260728
        db.row_factory = sqlite3.Row
        tasks = [dict(row) for row in db.execute(
            "SELECT id, title, parent_id, status, horizon, created_at, completed_at FROM tasks WHERE project_id=?",
            (args.project_id,),
        )]
    counts = Counter(task["status"] for task in tasks)

    def recent(value):
        if not value:
            return False
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return at.replace(tzinfo=at.tzinfo or UTC) >= now - timedelta(days=7)

    assert digest["open_total"] == sum(n for status, n in counts.items() if status != "completed")
    assert digest["completed_total"] == counts["completed"]
    assert digest["created_7d"] == sum(recent(task["created_at"]) for task in tasks)
    assert digest["closed_7d"] == sum(
        task["status"] == "completed" and recent(task["completed_at"]) for task in tasks
    )
    assert wall["stats"]["total"] == digest["open_total"]
    for status in ("pending", "running", "blocked", "failed"):
        assert digest["by_status"][status] == counts[status]
    cards = [row for bucket in wall["wall"].values() for row in bucket]
    assert len({row["id"] for row in cards}) == len(cards)
    running = [row for row in cards if row["parent_id"] == args.parent_id and row["status"] == "running"]
    assert running, "Snapshot must have a running child"
    parent = next(task for task in tasks if task["id"] == args.parent_id)
    project = get(f"/api/projects/{args.project_id}")["data"]
    completed = []
    while True:
        batch = get(prefix + "?status=completed&include_completed=true&completed_limit=50"
                    + f"&completed_offset={len(completed)}")
        completed.extend(batch["completed"])
        if not batch["completed_has_more"]:
            break
    assert len(completed) == counts["completed"] == len({row["id"] for row in completed})
    child_done = next(row for row in completed if row["parent_id"] == args.parent_id)
    failures = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel="chrome")
        for language, width in (("zh", 1440), ("en", 1440), ("zh", 375)):
            zh = language == "zh"
            back = "返回父任务" if zh else "Back to parent task"
            context = browser.new_context(viewport={"width": width, "height": 1000}, locale=f"{language}-CN")
            context.route("**/api/settings/language?*", lambda route: route.fulfill(
                json={"mode": language, "effective": language, "source": "dashboard"},
            ))
            context.route_web_socket("**/ws/events", lambda _ws: None)
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: failures.append(str(error)))
            page.goto(args.url + "/tasks")
            page.get_by_role("combobox").click()
            page.get_by_role("option", name=project["name"], exact=True).click()
            hint = ("统计包含子任务，每个任务计一次。" if zh
                    else "Counts include subtasks, with each task counted once.")
            expect(page.get_by_text(hint, exact=True)).to_be_visible()
            stats = page.get_by_role("group", name="任务墙统计" if language == "zh" else "Task wall stats", exact=True)
            labels = ("未关", "进行中", "待办") if language == "zh" else ("Open", "Running", "Pending")
            for label, count in zip(labels, (digest["open_total"], counts["running"], counts["pending"]), strict=True):
                cell = stats.get_by_role("button").filter(has=page.get_by_text(label, exact=True))
                expect(cell.locator("p").nth(1)).to_have_text(str(count))
            stats.get_by_role("button").filter(has=page.get_by_text(labels[1], exact=True)).click()
            for child in running:
                expect(page.get_by_role("button", name=child["title"], exact=True)).to_be_visible()
            parent_link = ("父任务：" if zh else "Parent task: ") + parent["title"]
            page.get_by_role("button", name=parent_link, exact=True).first.click()
            dialog = page.get_by_role("dialog")
            expect(dialog.get_by_role("heading", name=parent["title"], exact=True)).to_be_visible()
            page.keyboard.press("Escape")
            page.get_by_role("button", name=running[0]["title"], exact=True).click()
            expect(dialog.get_by_role("heading", name=running[0]["title"], exact=True)).to_be_visible()
            dialog.get_by_role("button", name=back, exact=True).click()
            expect(dialog.get_by_role("heading", name=parent["title"], exact=True)).to_be_visible()
            page.keyboard.press("Escape")
            page.screenshot(path=str(output / f"subtask-wall-{language}-{width}.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Horizontal overflow"
            header = (f"已完成 {counts['completed']} · 近 7 天 {digest['closed_7d']}" if zh
                      else f"Completed {counts['completed']} · last 7 days {digest['closed_7d']}")
            with page.expect_response(lambda response: "completed_limit=50" in response.url):
                page.get_by_role("button", name=header, exact=True).click()
            completed_section = page.get_by_role("button", name=header, exact=True).locator("..")
            row = completed_section.get_by_role("button", name=re.compile(re.escape(child_done["title"]) + r".*"))
            for _ in range(completed.index(child_done) // 50):
                with page.expect_response(lambda response: "completed_limit=50" in response.url):
                    page.get_by_role("button", name="加载更早的 50 条" if zh else "Load 50 older", exact=True).click()
            row.click()
            expect(dialog.get_by_role("heading", name=child_done["title"], exact=True)).to_be_visible()
            expect(dialog.get_by_role("button", name=back, exact=True)).to_be_visible()
            context.close()
        browser.close()
    assert not failures, failures
    evidence = {"project_id": args.project_id, "parent_id": args.parent_id,
                "counts": dict(counts), "created_7d": digest["created_7d"], "closed_7d": digest["closed_7d"],
                "running_children": len(running), "completed_unique": len(completed),
                "views": ["zh-1440", "en-1440", "zh-375"], "page_errors": failures}
    (output / "subtask-wall-browser.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    main()
