"""Playwright against production task routes with an isolated on-disk SQLite.
No shared API/database, account capture or real model execution is used.
PYTHONPATH=src /opt/homebrew/bin/python3.12 dashboard/tests/task-management.browser.py
"""

import asyncio
import functools
import json
import os
import threading
from contextlib import asynccontextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.clock import utc_now
from aiteam.loop.task_wall_engine import TaskWallEngine
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository

ROOT = Path(__file__).resolve().parents[2]
BUILD = Path(os.environ.get("DASHBOARD_TEST_BUILD", ROOT / "dashboard" / "dist"))
OUTPUT = ROOT / "test-screenshots"
OUTPUT.mkdir(exist_ok=True)
requests = []
errors = []
known_asset_errors = []
evidence = {}


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


@asynccontextmanager
async def isolated_lifespan(_app):
    # Deliberately no normal startup: no capture/monitor/background services.
    yield


def run(coro):
    return asyncio.run(coro)


with TemporaryDirectory(prefix="aiteam-dashboard-managed-") as temporary:
    db_url = f"sqlite+aiosqlite:///{temporary}/acceptance.sqlite"
    repository = StorageRepository(db_url=db_url)
    run(repository.init_db())
    memory = MemoryStore(repository=repository)
    bus = EventBus(repo=repository)
    translator = HookTranslator(repo=repository, event_bus=bus)
    # Replace the complete production dependency assembly, not only one side.
    deps._repository = repository
    deps._memory_store = memory
    deps._event_bus = bus
    deps._manager = TeamManager(repository=repository, memory=memory)
    deps._task_wall_engine = TaskWallEngine(repo=repository)
    deps._hook_translator = translator
    app = create_app()
    app.router.lifespan_context = isolated_lifespan
    project = run(repository.create_project("隔离无团队项目", root_path=f"{temporary}/project"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(BUILD)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    with TestClient(app) as client:

        def write(method, path, payload):
            response = client.request(method, path, json=payload)
            assert response.is_success, response.text
            return response.json()["data"]

        baseline = write(
            "POST",
            f"/api/projects/{project.id}/tasks",
            {"title": "并发编辑任务", "assigned_to": "执行者A", "horizon": "short"},
        )
        baseline = write(
            "PUT", f"/api/tasks/{baseline['id']}", {"result": "初始结果", "expected_revision": baseline["revision"]}
        )

        def api(route):
            request = route.request
            url = urlsplit(request.url)
            if not url.path.startswith("/api/"):
                route.continue_()
                return
            body = request.post_data_json if request.post_data else None
            entry = {"method": request.method, "path": url.path, "body": body}
            requests.append(entry)
            if url.path == "/api/settings/language":
                route.fulfill(json={"mode": "zh", "effective": "zh", "source": "dashboard"})
            elif url.path == "/api/health":
                route.fulfill(json={"status": "healthy", "version": "isolated"})
            elif url.path == "/api/notices":
                route.fulfill(json={"items": [], "total": 0})
            elif url.path == "/api/notices/summary":
                route.fulfill(
                    json={"notices": 0, "briefings": 0, "tasks": 0, "total": 0, "top": None, "language": "zh"}
                )
            elif (
                url.path.startswith("/api/tasks/")
                or url.path.startswith("/api/projects")
                or url.path.startswith("/api/teams")
                or url.path in ("/api/workflows", "/api/decisions", "/api/events")
            ):
                response = client.request(request.method, url.path + ("?" + url.query if url.query else ""), json=body)
                if request.method in ("POST", "PUT") and response.is_success:
                    payload = response.json()
                    entry["warnings"] = payload.get("warnings", [])
                    entry["_warning"] = payload.get("_warning")
                route.fulfill(status=response.status_code, content_type="application/json", body=response.content)
            else:
                errors.append("Unexpected API " + url.path)
                route.fulfill(status=500, json={"detail": "unexpected acceptance endpoint"})

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel="chrome")
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
            context.route_web_socket("**/ws/events", lambda ws: None)
            context.route("**/*", api)
            page = context.new_page()
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))

            def console(message):
                if message.type != "error" or "409" in message.text:
                    return
                if message.location.get("url", "").endswith("/vite.svg"):
                    known_asset_errors.append(message.text)
                else:
                    errors.append(message.text)

            page.on("console", console)
            page.goto(origin + "/tasks")
            page.get_by_role("button", name="并发编辑任务", exact=True).click()
            page.get_by_role("button", name="编辑任务", exact=True).click()
            page.get_by_label("任务标题", exact=True).fill("我的标题修改")
            latest = write(
                "PUT",
                f"/api/tasks/{baseline['id']}",
                {
                    "assigned_to": "执行者B",
                    "result": "并发新结果",
                    "waiting_on": {"kind": "user", "target_id": "", "reason": "并发用户决定"},
                    "expected_revision": baseline["revision"],
                },
            )
            page.get_by_role("button", name="保存", exact=True).click()
            page.get_by_role("alert").get_by_text("负责人: 执行者B", exact=False).wait_for()
            assert page.get_by_label("任务标题", exact=True).input_value() == "我的标题修改"
            page.get_by_text("重试只提交你修改的字段；这些字段将覆盖所展示的最新值： 标题", exact=True).wait_for()
            page.screenshot(path=str(OUTPUT / "task-dirty-retry.png"), full_page=False)
            page.get_by_role("button", name="基于最新版本重试", exact=True).click()
            page.get_by_label("任务标题", exact=True).wait_for(state="hidden")
            saved = client.get(f"/api/tasks/{baseline['id']}").json()["data"]
            persisted = client.portal.call(repository.get_task, baseline["id"])
            assert saved["title"] == "我的标题修改"
            for field in ["assigned_to", "result", "waiting_on", "status", "block_reason"]:
                assert saved[field] == latest[field], (field, saved, latest)
            assert persisted.assigned_to == "执行者B" and persisted.result == "并发新结果"
            assert persisted.waiting_on.reason == "并发用户决定"
            evidence["concurrent_edit"] = {"http_read": saved, "database_read": persisted.model_dump(mode="json")}
            patches = [
                entry["body"]
                for entry in requests
                if entry["method"] == "PUT" and entry["path"] == f"/api/tasks/{baseline['id']}"
            ]
            assert patches == [
                {"title": "我的标题修改", "expected_revision": baseline["revision"]},
                {"title": "我的标题修改", "expected_revision": latest["revision"]},
            ]
            page.get_by_role("dialog").get_by_text("并发新结果", exact=True).wait_for()
            # Same-field conflict: retain the local title, show the latest title,
            # and require the explicit retry action before overwriting that field.
            page.get_by_role("button", name="编辑任务", exact=True).click()
            page.get_by_label("任务标题", exact=True).fill("明确保留我的标题")
            latest = write(
                "PUT",
                f"/api/tasks/{baseline['id']}",
                {"title": "另一个会话的标题", "expected_revision": saved["revision"]},
            )
            page.get_by_role("button", name="保存", exact=True).click()
            page.get_by_role("alert").get_by_text("另一个会话的标题", exact=False).wait_for()
            page.get_by_text("重试只提交你修改的字段；这些字段将覆盖所展示的最新值： 标题", exact=True).wait_for()
            page.get_by_role("button", name="基于最新版本重试", exact=True).click()
            page.get_by_label("任务标题", exact=True).wait_for(state="hidden")
            saved = client.get(f"/api/tasks/{baseline['id']}").json()["data"]
            assert saved["title"] == "明确保留我的标题" and saved["waiting_on"] == latest["waiting_on"]
            page.get_by_role("dialog").get_by_text("并发新结果", exact=True).wait_for()
            # Explicit dirty clears pass through the actual schema/service and DB.
            page.get_by_role("button", name="编辑任务", exact=True).click()
            page.get_by_label("负责人", exact=True).fill("")
            page.get_by_label("结果", exact=True).fill("")
            page.get_by_label("等待对象", exact=True).select_option("none")
            page.get_by_role("button", name="保存", exact=True).click()
            page.get_by_label("结果", exact=True).wait_for(state="hidden")
            cleared = client.get(f"/api/tasks/{baseline['id']}").json()["data"]
            assert cleared["assigned_to"] is None and cleared["waiting_on"] is None and cleared["result"] == ""
            assert cleared["status"] == "pending"
            evidence["explicit_clear"] = {
                "http_read": cleared,
                "database_read": client.portal.call(repository.get_task, baseline["id"]).model_dump(mode="json"),
            }
            last_patch = [
                entry["body"]
                for entry in requests
                if entry["method"] == "PUT" and entry["path"] == f"/api/tasks/{baseline['id']}"
            ][-1]
            assert set(last_patch) == {"assigned_to", "result", "waiting_on", "expected_revision"}
            page.get_by_role("button", name="Close", exact=True).click()
            # Create both parent and child through the UI in a project with no team.
            page.get_by_role("button", name="创建任务", exact=True).click()
            page.get_by_label("任务标题", exact=True).fill("闭环父任务")
            page.get_by_role("button", name="提交", exact=True).click()
            page.get_by_role("dialog").wait_for(state="hidden")
            page.get_by_role("button", name="闭环父任务", exact=True).wait_for()
            wall_data = client.get(f"/api/projects/{project.id}/task-wall").json()
            parent_id = next(
                row["id"] for bucket in wall_data["wall"].values() for row in bucket if row["title"] == "闭环父任务"
            )
            parent = client.portal.call(repository.get_task, parent_id)
            page.get_by_role("button", name="创建任务", exact=True).click()
            page.get_by_label("任务标题", exact=True).fill("闭环子任务")
            page.get_by_label("任务描述", exact=True).fill("子任务完整说明")
            page.get_by_label("父任务", exact=True).select_option(label="闭环父任务")
            page.get_by_role("button", name="提交", exact=True).click()
            page.get_by_role("dialog").wait_for(state="hidden")
            child = [
                task for task in client.portal.call(repository.list_subtasks, parent.id) if task.title == "闭环子任务"
            ][0]
            assert child.team_id is None and parent.team_id is None and child.parent_id == parent.id
            # A waiting child must be discoverable without first opening the parent.
            waiting_child = write(
                "PUT",
                f"/api/tasks/{child.id}",
                {
                    "waiting_on": {"kind": "user", "target_id": "", "reason": "请用户确认子任务验收"},
                    "expected_revision": child.revision,
                },
            )
            visibility_wall = client.get(f"/api/projects/{project.id}/task-wall").json()
            visibility_parent = next(
                row for rows in visibility_wall["wall"].values() for row in rows if row["id"] == parent.id
            )
            visibility_digest = client.get(f"/api/projects/{project.id}/task-wall/digest").json()
            waiting_item = next(item for item in visibility_digest["waiting_user"] if item["id"] == child.id)
            assert waiting_item["parent_id"] == parent.id and waiting_item["parent_title"] == "闭环父任务"
            assert visibility_parent["waiting_on"] is None and visibility_parent["waiting_user"] is True
            assert visibility_parent["waiting_user_subtasks"][0]["id"] == child.id
            evidence["waiting_child_visibility"] = {"digest_item": waiting_item, "parent_wall": visibility_parent}
            page.reload()
            waiting_region = page.get_by_role("region", name="等用户的子任务", exact=True)
            waiting_region.get_by_role("button", name="闭环子任务", exact=False).wait_for()
            page.set_viewport_size({"width": 375, "height": 1000})
            waiting_region.scroll_into_view_if_needed()
            page.screenshot(path=str(OUTPUT / "task-waiting-child-card-375.png"), full_page=False)
            waiting_region.get_by_role("button", name="闭环子任务", exact=False).click()
            page.get_by_role("dialog").get_by_text("子任务完整说明", exact=True).wait_for()
            page.get_by_role("dialog").get_by_text("等用户: 请用户确认子任务验收", exact=True).wait_for()
            page.get_by_role("button", name="Close", exact=True).click()
            page.goto(origin + "/briefings")
            briefing_region = page.get_by_role("region", name="等用户决定的任务", exact=True)
            briefing_region.get_by_role("button", name="闭环父任务 / 闭环子任务", exact=False).click()
            page.get_by_role("dialog").get_by_text("子任务完整说明", exact=True).wait_for()
            page.get_by_role("dialog").get_by_text("等用户: 请用户确认子任务验收", exact=True).wait_for()
            page.screenshot(path=str(OUTPUT / "task-waiting-child-briefing-375.png"), full_page=False)
            page.get_by_role("button", name="Close", exact=True).click()
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.goto(origin + "/tasks")
            page.get_by_role("button", name="闭环父任务", exact=True).click()
            page.get_by_role("button", name="标记完成", exact=True).click()
            page.get_by_role("alert").get_by_role("button", name="闭环子任务", exact=True).wait_for()
            page.screenshot(path=str(OUTPUT / "task-open-child-conflict.png"), full_page=False)
            page.get_by_role("alert").get_by_role("button", name="闭环子任务", exact=True).click()
            page.get_by_role("dialog").get_by_text("子任务完整说明", exact=True).wait_for()
            page.get_by_role("button", name="编辑任务", exact=True).click()
            page.get_by_label("任务描述", exact=True).fill("子任务编辑已验证")
            page.get_by_label("等待对象", exact=True).select_option("none")
            page.get_by_role("button", name="保存", exact=True).click()
            page.get_by_role("dialog").get_by_text("子任务编辑已验证", exact=True).wait_for()
            assert client.portal.call(repository.get_task, child.id).description == "子任务编辑已验证"
            assert client.portal.call(repository.get_task, child.id).waiting_on is None
            assert waiting_child["status"] == "blocked"
            for width in [375, 768, 1024, 1440]:
                page.set_viewport_size({"width": width, "height": 1000})
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth && "
                    "document.querySelector('main').scrollWidth <= document.querySelector('main').clientWidth"
                )
                page.screenshot(path=str(OUTPUT / f"task-child-detail-{width}.png"), full_page=False)
            page.get_by_role("button", name="标记完成", exact=True).click()
            page.get_by_role("dialog").get_by_text("已完成", exact=True).wait_for()
            assert client.portal.call(repository.get_task, child.id).status.value == "completed"
            page.get_by_role("button", name="返回父任务", exact=True).click()
            page.get_by_role("dialog").get_by_role("button", name="闭环子任务", exact=False).wait_for()
            page.get_by_role("dialog").get_by_text("已完成", exact=True).wait_for()
            page.get_by_role("button", name="标记完成", exact=True).click()
            page.get_by_role("dialog").get_by_text("已完成", exact=True).first.wait_for()
            page.get_by_role("button", name="标记完成", exact=True).wait_for(state="hidden")
            assert client.portal.call(repository.get_task, parent.id).status.value == "completed"
            evidence["parent_child_completion"] = {
                "parent_database_read": client.portal.call(repository.get_task, parent.id).model_dump(mode="json"),
                "child_database_read": client.portal.call(repository.get_task, child.id).model_dump(mode="json"),
            }
            page.screenshot(path=str(OUTPUT / "task-parent-child-completed.png"), full_page=False)
            # Warnings are the actual production response, not a fabricated toast.
            page.get_by_role("button", name="Close", exact=True).click()
            predecessor = write("POST", f"/api/projects/{project.id}/tasks", {"title": "运行前置", "horizon": "short"})
            predecessor = write(
                "PUT", f"/api/tasks/{predecessor['id']}/complete", {"expected_revision": predecessor["revision"]}
            )
            running = write(
                "POST",
                f"/api/projects/{project.id}/tasks",
                {"title": "运行事实保留", "horizon": "short", "auto_start": True, "depends_on": [predecessor["id"]]},
            )
            write(
                "PUT",
                f"/api/tasks/{predecessor['id']}",
                {"status": "pending", "expected_revision": predecessor["revision"]},
            )
            page.reload()
            page.get_by_role("button", name="运行事实保留", exact=True).click()
            page.get_by_role("button", name="编辑任务", exact=True).click()
            page.get_by_label("任务描述", exact=True).fill("执行进度已记录")
            page.get_by_role("button", name="保存", exact=True).click()
            page.get_by_label("任务描述", exact=True).wait_for(state="hidden")
            running_reply = next(
                entry
                for entry in reversed(requests)
                if entry["path"] == f"/api/tasks/{running['id']}" and entry["method"] == "PUT"
            )
            assert len(running_reply["warnings"]) == 1 and "保留 running" in running_reply["warnings"][0]
            warning = running_reply["warnings"][0]
            page.get_by_role("dialog").get_by_role("alert", name="任务写入提示", exact=True).get_by_text(
                warning, exact=True
            ).wait_for()
            running_saved = client.get(f"/api/tasks/{running['id']}").json()["data"]
            assert running_saved["status"] == "running" and running_saved["started_at"] == running["started_at"]
            assert client.portal.call(repository.get_task, running["id"]).description == "执行进度已记录"
            page.get_by_role("button", name="Close", exact=True).click()
            page.get_by_role("dialog").wait_for(state="hidden")
            warning_alert = page.get_by_role("alert", name="任务写入提示", exact=True)
            warning_alert.get_by_text(warning, exact=True).wait_for()
            page.set_viewport_size({"width": 375, "height": 1000})
            warning_alert.scroll_into_view_if_needed()
            page.screenshot(path=str(OUTPUT / "task-warning-after-close-375.png"), full_page=False)
            warning_alert.get_by_role("button", name="关闭提示", exact=True).click()
            page.get_by_role("alert", name="任务写入提示", exact=True).wait_for(state="hidden")
            evidence["running_warning_display"] = {
                "warnings": running_reply["warnings"],
                "database_read": running_saved,
                "visible_after_dialog_close": True,
            }

            # Completing a predecessor leaves legacy unknown blockage untouched,
            # and surfaces its warning through the separate completion action.
            complete_source = write(
                "POST", f"/api/projects/{project.id}/tasks", {"title": "完成警告前置", "horizon": "short"}
            )
            legacy_blocked = client.portal.call(
                functools.partial(
                    repository.create_task,
                    None,
                    "旧阻塞任务",
                    project_id=project.id,
                    status="blocked",
                    config={},
                    depends_on=[complete_source["id"]],
                )
            )
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.reload()
            page.get_by_role("button", name="完成警告前置", exact=True).click()
            page.get_by_role("button", name="标记完成", exact=True).click()
            page.get_by_role("button", name="标记完成", exact=True).wait_for(state="hidden")
            complete_reply = next(
                entry
                for entry in reversed(requests)
                if entry["path"] == f"/api/tasks/{complete_source['id']}/complete" and entry["method"] == "PUT"
            )
            assert complete_reply["warnings"] and "阻塞原因未知" in complete_reply["warnings"][0]
            page.get_by_role("dialog").get_by_role("alert", name="任务写入提示", exact=True).get_by_text(
                complete_reply["warnings"][0], exact=True
            ).wait_for()
            assert client.portal.call(repository.get_task, legacy_blocked.id).status.value == "blocked"
            evidence["complete_warning_display"] = {
                "warnings": complete_reply["warnings"],
                "downstream_database_read": client.portal.call(repository.get_task, legacy_blocked.id).model_dump(
                    mode="json"
                ),
            }
            page.get_by_role("button", name="Close", exact=True).click()
            page.get_by_role("dialog").wait_for(state="hidden")
            page.get_by_role("alert", name="任务写入提示", exact=True).get_by_text(
                complete_reply["warnings"][0], exact=True
            ).wait_for()
            page.get_by_role("button", name="关闭提示", exact=True).click()

            # Legacy team creation uses _warning, not the newer warnings array.
            team = client.portal.call(
                functools.partial(repository.create_team, "提示测试团队", "coordinate", project_id=project.id)
            )
            write(
                "POST",
                f"/api/teams/{team.id}/tasks/run",
                {"title": "运行 重复 检查", "description": "重复检测", "auto_start": True},
            )
            page.reload()
            page.get_by_role("button", name="创建任务", exact=True).click()
            page.get_by_label("目标团队", exact=True).select_option(team.id)
            page.get_by_label("任务标题", exact=True).fill("运行 重复 检查")
            page.get_by_role("button", name="提交", exact=True).click()
            page.get_by_role("dialog").wait_for(state="hidden")
            create_reply = next(
                entry
                for entry in reversed(requests)
                if entry["path"] == f"/api/teams/{team.id}/tasks/run" and entry["method"] == "POST"
            )
            assert create_reply["_warning"] and "相似的运行中任务" in create_reply["_warning"]
            page.get_by_role("alert", name="任务写入提示", exact=True).get_by_text(
                create_reply["_warning"], exact=True
            ).wait_for()
            evidence["create_warning_display"] = {"warning": create_reply["_warning"]}
            # Verify the other routed team-create surface uses the same feedback.
            page.goto(origin + f"/projects/{project.id}")
            page.get_by_role("button", name="任务", exact=True).click()
            dialog = page.get_by_role("dialog")
            dialog.get_by_role("textbox").first.fill("运行 重复 检查")
            dialog.get_by_role("button", name="创建", exact=True).click()
            dialog.wait_for(state="hidden")
            page.get_by_role("alert", name="任务写入提示", exact=True).get_by_text(
                create_reply["_warning"], exact=True
            ).wait_for()
            page.screenshot(path=str(OUTPUT / "task-create-warning-project-detail.png"), full_page=False)
            assert not errors, errors
            browser.close()
        run(translator.drain(5))
    server.shutdown()
    run(get_engine(db_url).dispose())

(OUTPUT / "task-management-persisted-validation.json").write_text(
    json.dumps(
        {
            "passed": True,
            "observed_at": utc_now().isoformat(),
            "persisted_evidence": evidence,
            "requests": requests,
            "errors": errors,
            "known_baseline_asset_errors": known_asset_errors,
        },
        ensure_ascii=False,
        indent=2,
    )
)
print(json.dumps({"passed": True, "screenshots": str(OUTPUT), "production_sqlite": True}, ensure_ascii=False))
