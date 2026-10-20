"""Large ranking acceptance against production routes and an isolated SQLite file.
Build first; run through testlock with Homebrew Python 3.12. No shared API is used.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import delete

from aiteam.api import deps
from aiteam.api.routes import analytics, projects, teams
from aiteam.clock import utc_now
from aiteam.storage.connection import get_engine, get_session
from aiteam.storage.models import AgentActivityModel, AgentModel
from aiteam.storage.repository import StorageRepository

ROOT = Path(__file__).resolve().parents[2]
BUILD = Path(os.environ.get("DASHBOARD_TEST_BUILD", ROOT / "dashboard/dist"))
OUTPUT = Path(os.environ.get("DASHBOARD_ACCEPTANCE_OUTPUT", ROOT / "test-screenshots" / "analytics-pagination"))
OUTPUT.mkdir(parents=True, exist_ok=True)
requests, errors, measurements = [], [], []


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if "." not in self.path.rsplit("/", 1)[-1]:
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, *_args):
        pass


with TemporaryDirectory(prefix="aiteam-analytics-pagination-") as temporary:
    db_url = f"sqlite+aiosqlite:///{temporary}/fixture.db"
    repo = StorageRepository(db_url=db_url)

    async def seed():
        await repo.init_db()
        result = []
        now = utc_now()
        for scope, size in enumerate((5003, 23, 0)):
            project = await repo.create_project(f"分页项目-{scope}", root_path=f"{temporary}/{scope}")
            team = await repo.create_team(f"分页团队-{scope}", "coordinate", project_id=project.id)
            async with get_session(db_url) as session:
                for index in range(size):
                    identifier = f"{scope:02d}-{index:030d}"
                    model = AgentModel(
                        id=identifier,
                        team_id=team.id,
                        project_id=project.id,
                        name=f"Agent-{scope}-{index:05d}-" + "超长名称" * 30,
                        role="worker",
                        status="waiting",
                        created_at=now,
                    )
                    # Validate bulk fixture records through production conversion.
                    model.to_pydantic()
                    session.add(model)
                    activity = AgentActivityModel(
                        id=identifier,
                        agent_id=identifier,
                        session_id="fixture",
                        tool_name="Read",
                        timestamp=now,
                        status="completed",
                    )
                    activity.to_pydantic()
                    session.add(activity)
                await session.commit()
            result.append((project.id, team.id))
        return result

    scopes = asyncio.run(seed())
    previous_repo = deps._repository
    deps._repository = repo
    app = FastAPI()
    for router in (analytics.router, projects.router, teams.router):
        app.include_router(router)
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(BUILD)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with TestClient(app) as client, sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, channel="chrome")
            context = browser.new_context(viewport={"width": 1280, "height": 1000}, locale="zh-CN")
            context.route_web_socket("**/ws/events", lambda ws: None)

            def api(route):
                request = route.request
                url = urlsplit(request.url)
                if not url.path.startswith("/api/"):
                    route.continue_()
                    return
                assert request.method == "GET", f"Unexpected write: {request.method} {url.path}"
                if url.path.startswith("/api/analytics/") or url.path in ("/api/projects", "/api/teams"):
                    response = client.get(url.path + ("?" + url.query if url.query else ""))
                    if url.path.endswith("/agent-productivity"):
                        payload = response.json()
                        requests.append(
                            {"query": parse_qs(url.query), "count": len(payload["data"]), "total": payload["total"]}
                        )
                        assert len(payload["data"]) <= 20
                    route.fulfill(status=response.status_code, content_type="application/json", body=response.content)
                elif url.path == "/api/settings/language":
                    route.fulfill(json={"mode": "zh", "effective": "zh", "source": "dashboard"})
                elif url.path == "/api/health":
                    route.fulfill(json={"status": "healthy", "version": "isolated"})
                elif url.path == "/api/notices/summary":
                    route.fulfill(
                        json={"notices": 0, "briefings": 0, "tasks": 0, "total": 0, "top": None, "language": "zh"}
                    )
                elif url.path == "/api/notices":
                    route.fulfill(json={"items": [], "total": 0})
                else:
                    errors.append("Unexpected API " + url.path)
                    route.fulfill(status=500, json={"detail": "unexpected acceptance endpoint"})

            context.route("**/*", api)
            page = context.new_page()
            page.clock.install()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/analytics")
            ranking = page.locator('[data-slot="card"]').filter(has=page.get_by_text("Agent活跃度排行", exact=True))
            expect(ranking.locator("tbody tr")).to_have_count(20)
            expect(ranking.get_by_text("1–20 / 5026", exact=True)).to_be_visible()
            first = ranking.locator("tbody tr").first.inner_text()
            expect(ranking.get_by_role("button", name="上一页", exact=True)).to_be_disabled()
            ranking.get_by_role("button", name="下一页", exact=True).click()
            expect(ranking.get_by_text("21–40 / 5026", exact=True)).to_be_visible()
            assert ranking.locator("tbody tr").first.inner_text() != first
            ranking.get_by_role("button", name="上一页", exact=True).click()
            expect(ranking.get_by_text("1–20 / 5026", exact=True)).to_be_visible()
            assert ranking.locator("tbody tr").first.inner_text() == first

            def measure(width):
                page.set_viewport_size({"width": width, "height": 1000})
                result = ranking.evaluate("""card => ({height:card.getBoundingClientRect().height,
                  tableWidth:card.querySelector('[data-slot=table-container]').clientWidth,
                  tableScroll:card.querySelector('[data-slot=table-container]').scrollWidth,
                  pageWidth:document.documentElement.clientWidth,pageScroll:document.documentElement.scrollWidth,
                  pageHeight:document.documentElement.scrollHeight, rows:card.querySelectorAll('tbody tr').length})""")
                timeline = page.locator('[data-slot="card"]').filter(
                    has=page.get_by_text("活动时间线（过去24小时）", exact=True)
                )
                result["timeline_bar_area_height"] = timeline.locator(".h-24").first.evaluate(
                    "element => element.getBoundingClientRect().height"
                )
                assert result["timeline_bar_area_height"] == 96
                measurements.append({"viewport": width, **result})
                assert result["height"] < 1300, result
                assert result["pageHeight"] < 4000, result
                assert result["tableScroll"] <= result["tableWidth"] + 1, result
                assert result["pageScroll"] <= result["pageWidth"] + 1, result
                page.screenshot(path=str(OUTPUT / f"ranking-{width}.png"), full_page=False)

            measure(1280)
            measure(1440)
            # Both filters reset the API offset, including a partially filled last page.
            ranking.get_by_role("button", name="下一页", exact=True).click()
            expect(ranking.get_by_text("21–40 / 5026", exact=True)).to_be_visible()
            page.get_by_role("combobox").filter(has_text="全部团队").click()
            page.get_by_role("option", name="分页团队-1", exact=True).click()
            expect(ranking.get_by_text("1–20 / 23", exact=True)).to_be_visible()
            assert requests[-1]["query"]["offset"] == ["0"]
            ranking.get_by_role("button", name="下一页", exact=True).click()
            expect(ranking.get_by_text("21–23 / 23", exact=True)).to_be_visible()
            expect(ranking.locator("tbody tr")).to_have_count(3)
            expect(ranking.get_by_role("button", name="下一页", exact=True)).to_be_disabled()

            async def remove_last_page():
                async with get_session(db_url) as session:
                    await session.execute(
                        delete(AgentActivityModel).where(
                            AgentActivityModel.agent_id.in_([f"01-{index:030d}" for index in range(20, 23)])
                        )
                    )
                    await session.commit()

            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(asyncio.run, remove_last_page()).result()
            page.clock.fast_forward(30_001)
            expect(ranking.get_by_text("1–20 / 20", exact=True)).to_be_visible()
            expect(ranking.locator("tbody tr")).to_have_count(20)
            assert requests[-1]["query"]["offset"] == ["0"]
            page.get_by_role("combobox").filter(has_text="全部项目").click()
            page.get_by_role("option", name="分页项目-2", exact=True).click()
            expect(ranking.get_by_text("0–0 / 0", exact=True)).to_be_visible()
            expect(ranking.locator("tbody tr")).to_have_count(0)
            expect(ranking.get_by_role("button", name="上一页", exact=True)).to_be_disabled()
            expect(ranking.get_by_role("button", name="下一页", exact=True)).to_be_disabled()
            assert requests[-1]["query"]["offset"] == ["0"]
            page.get_by_role("combobox").filter(has_text="分页项目-2").click()
            page.get_by_role("option", name="全部项目", exact=True).click()
            expect(ranking.get_by_text("1–20 / 5023", exact=True)).to_be_visible()
            for title in ("总活动数", "有活动记录的 Agent"):
                card = page.locator('[data-slot="card"]').filter(has=page.get_by_text(title, exact=True))
                expect(card.get_by_text("5023", exact=True)).to_be_visible()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        deps._repository = previous_repo
        asyncio.run(get_engine(db_url).dispose())

(OUTPUT / "browser-evidence.json").write_text(
    json.dumps(
        {"passed": True, "sqlite_agents": 5026, "requests": requests, "measurements": measurements, "errors": errors},
        ensure_ascii=False,
        indent=2,
    )
)
print(json.dumps({"passed": True, "measurements": measurements, "output": str(OUTPUT)}, ensure_ascii=False))
