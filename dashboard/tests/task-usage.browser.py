"""Controlled UI acceptance: only the temporary build and fixture APIs, no shared service.
PYTHONPATH=src /opt/homebrew/bin/python3.12 dashboard/tests/task-usage.browser.py
"""

import functools
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

from aiteam.api.schemas import TaskCompleteBody, TaskCreateBody, TaskUpdateBody
from aiteam.types import (
    ExternalExecInvocation,
    ExternalExecInvocationCompact,
    ExternalExecReconciliation,
    ExternalExecReconciliationItem,
    Task,
    TaskWallDigest,
)

ROOT = Path(__file__).resolve().parents[2]
BUILD = Path(os.environ.get("DASHBOARD_TEST_BUILD", ROOT / "dashboard" / "dist"))
OUTPUT = ROOT / "test-screenshots"
OUTPUT.mkdir(exist_ok=True)
NOW = "2026-10-09T09:00:00Z"
PROJECT = "11111111-1111-4111-8111-111111111111"
PARENT = "22222222-2222-4222-8222-222222222222"
CHILD = "33333333-3333-4333-8333-333333333333"
task = dict(
    id=PARENT,
    project_id=PROJECT,
    team_id=None,
    title="发布验收父任务",
    description="保留已有说明",
    status="blocked",
    priority="medium",
    horizon="short",
    tags=[],
    assigned_to="tester",
    result="旧结果",
    parent_id=None,
    depends_on=[CHILD],
    waiting_on={"kind": "user", "target_id": "", "reason": "请确认验收范围"},
    status_note="等待范围确认",
    revision=2,
    created_at=NOW,
    started_at=None,
    completed_at=None,
)
Task.model_validate(task)
child = {**task, "id": CHILD, "title": "未关子任务fixture", "parent_id": PARENT, "waiting_on": None, "depends_on": []}
row = {
    **task,
    "subtasks": [child],
    "subtask_progress": {"completed": 1, "total": 2},
    "latest_progress": {"content": "已完成接口联调", "author": "tester", "timestamp": NOW},
    "waiting_user": True,
    "executor_stopped": True,
    "management_hints": [],
}
item = dict(
    id=PARENT,
    title=task["title"],
    status="blocked",
    priority="medium",
    horizon="short",
    created_at=NOW,
    activity_at=NOW,
    activity_kind="memo",
    activity_memo_type="progress",
    activity_by="tester",
    idle_days=0,
    wall_days=0,
    stale=False,
    blocked_days=0,
    waiting_on=task["waiting_on"],
)
digest = dict(
    project_id=PROJECT,
    as_of=NOW,
    open_total=1,
    by_status={"pending": 0, "running": 0, "blocked": 1, "failed": 0},
    by_horizon={"short": 1, "mid": 0, "long": 0},
    matrix={},
    by_priority={"medium": 1},
    created_7d=1,
    closed_7d=0,
    completed_total=0,
    stale_days=7,
    stale_running=0,
    dormant_days=30,
    dormant_pending=0,
    blocked_oldest_days=0,
    recent=[item],
    top=[],
    heads={},
    stuck=[item],
    text="fixture",
    waiting_user=[item],
)
TaskWallDigest.model_validate(digest)
wall = {
    "wall": {"short": [row], "mid": [], "long": []},
    "digest": digest,
    "stats": {"total": 1, "by_status": {"blocked": 1}, "by_priority": {"medium": 1}, "avg_score": 0},
}
receipt = dict(
    receipt_id="turn-1",
    thread_id="native-thread-1",
    turn_id="turn-1",
    counting_level="turn_aggregate",
    usage={
        "input_tokens": 20,
        "cached_input_tokens": None,
        "output_tokens": 8,
        "reasoning_output_tokens": None,
        "total_tokens": None,
    },
    provider=None,
    model=None,
    service_tier=None,
    account_key=None,
    metadata_source="unknown",
    observed_at=NOW,
)
invocation = dict(
    invocation_id="44444444-4444-4444-8444-444444444444",
    project_id=None,
    task_id=None,
    caller_harness="claude-code",
    caller_session_id=None,
    callee_harness="codex",
    source_namespace="a" * 64,
    ephemeral=True,
    started_at=NOW,
    thread_id="native-thread-1",
    status="completed",
    exit_code=0,
    finished_at=NOW,
    revision=1,
    receipts={"turn-1": receipt},
    diagnostics=["error_event", "association_quarantined"],
    association_status="quarantined",
    association_reason="project_not_found",
    requested_project_id="77777777-7777-4777-8777-777777777777",
)
missing = {
    **invocation,
    "invocation_id": "55555555-5555-4555-8555-555555555555",
    "receipts": {},
    "status": "interrupted",
    "exit_code": None,
    "association_status": "rejected",
    "association_reason": "task_not_found",
    "requested_task_id": "88888888-8888-4888-8888-888888888888",
    "diagnostics": ["association_rejected"],
}
report = dict(
    observed_at=NOW,
    scope={
        "project_id": None,
        "source_namespace": None,
        "since": None,
        "until": None,
        "journal_project_attribution": "unknown",
        "history": "saved_evidence_only",
    },
    counts={
        "journal_observations": 2,
        "journal_threads": 2,
        "registered_codex_agents": 1,
        "external_invocations": 2,
        "discovered_threads": 3,
        "overlap_threads": 1,
    },
    buckets={
        "unregistered_threads": [{"thread_id": "unregistered-thread", "source_namespace": "a" * 64}],
        "finished_without_usage": [
            {"invocation_id": "55555555-5555-4555-8555-555555555555", "thread_id": None, "status": "interrupted"}
        ],
        "tokens_without_complete_pricing": [
            {
                "source": "external_exec",
                "invocation_id": "44444444-4444-4444-8444-444444444444",
                "thread_id": "native-thread-1",
                "reasons": ["model_unknown"],
            }
        ],
        "external_without_journal": [
            {"invocation_id": "44444444-4444-4444-8444-444444444444", "thread_id": "native-thread-1", "ephemeral": True}
        ],
        "registered_without_evidence": [],
        "running_invocations": [],
    },
    truncated={"journal": True, "agents": False, "invocations": False, "cursors": False},
    statements=["fixture：仅已保存证据，原生套餐百分比另有来源。"],
)
receipt_long = {
    **receipt,
    "receipt_id": "turn-long",
    "turn_id": "turn-long",
    "model": "future-model-" + "x" * 200,
    "provider": "unknown-provider-" + "x" * 150,
    "metadata_source": "explicit_observation",
    "usage": {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "cache_write_input_tokens": None,
        "total_tokens": 0,
    },
}
receipt_null = {**receipt, "receipt_id": "turn-missing", "turn_id": None, "usage": None}
invocation["receipts"].update({"turn-long": receipt_long, "turn-missing": receipt_null})
# Enough valid receipts to exercise the real 100-item page boundary.
for index in range(97):
    key = f"turn-fill-{index}"
    invocation["receipts"][key] = {**receipt_null, "receipt_id": key}
invocation["receipts"]["thread-resumed"] = {
    **receipt,
    "receipt_id": "thread-resumed",
    "turn_id": "turn-resumed",
    "counting_level": "thread_aggregate",
}
invocation["receipts"]["zero-unverified"] = {
    **receipt_long,
    "receipt_id": "zero-unverified",
    "turn_id": None,
    "counting_level": "thread_aggregate",
    "usage_evidence": "zero_unverified",
}
invocation = ExternalExecInvocation.model_validate(invocation).model_dump(mode="json")
missing = ExternalExecInvocation.model_validate(missing).model_dump(mode="json")
report["buckets"]["unregistered_threads"] = [
    {"thread_id": f"unregistered-thread-{index}", "source_namespace": "a" * 64} for index in range(21)
]
report["buckets"].update(
    {
        "journal_project_unknown": [],
        "quarantined_invocations": [
            {"invocation_id": invocation["invocation_id"], "association_reason": "project_not_found"}
        ],
        "pricing_metadata_unverified": [],
    }
)
report["scope"].update(
    {
        "registration_existence": "global_all_saved_evidence",
        "agent_source_namespace": "unknown",
        "time_basis": {"journal": "occurred_at", "agents": "created_at", "invocations": "started_at"},
    }
)
report["counts"].update(journal_observations=23, journal_threads=22, discovered_threads=23)
report.update(
    bucket_counts={key: len(items) for key, items in report["buckets"].items()},
    bucket_truncated={key: len(items) > 20 for key, items in report["buckets"].items()},
    bucket_offset=0,
    bucket_limit=20,
    sample_limit=500,
    summary=False,
    journal_status={"cursor_count": 1, "incomplete_cursors": 0, "unknown_observation_times_in_sample": False},
)
report["buckets"] = {
    key: [ExternalExecReconciliationItem.model_validate(item).model_dump(mode="json") for item in items]
    for key, items in report["buckets"].items()
}
ExternalExecReconciliation.model_validate(report)


def reconciliation_page(offset):
    value = {
        **report,
        "buckets": {key: items[offset : offset + 20] for key, items in report["buckets"].items()},
        "bucket_offset": offset,
        "bucket_truncated": {key: offset + 20 < len(items) for key, items in report["buckets"].items()},
    }
    return ExternalExecReconciliation.model_validate(value).model_dump(mode="json")


def invocation_page(value, offset=0, limit=100):
    pairs = list(value["receipts"].items())
    data = {
        **value,
        "receipts": dict(pairs[offset : offset + limit]),
        "receipts_count": len(pairs),
        "receipts_offset": offset,
        "receipts_limit": limit,
        "receipts_truncated": offset + limit < len(pairs),
        "usage_observed": any(
            receipt["usage"] is not None and any(field is not None for field in receipt["usage"].values())
            for receipt in value["receipts"].values()
        ),
    }
    return ExternalExecInvocationCompact.model_validate(data).model_dump(mode="json")


requests = []
errors = []
known_asset_errors = []
state = {"conflict": True, "complete_count": 0, "create_fail": False, "external_fail": False, "complete_success": False}


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


server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(BUILD)))
threading.Thread(target=server.serve_forever, daemon=True).start()
origin = f"http://127.0.0.1:{server.server_port}"


def api(route):
    from urllib.parse import parse_qs, urlsplit

    url = urlsplit(route.request.url)
    path = url.path
    query = parse_qs(url.query)
    if not path.startswith("/api/"):
        route.continue_()
        return
    method = route.request.method
    body = route.request.post_data_json if route.request.post_data else None
    requests.append(dict(path=path, method=method, body=body, query=url.query))

    def send(value, wrapped=False, status=200):
        route.fulfill(status=status, json={"success": True, "data": value, "message": "已保存"} if wrapped else value)

    if path == "/api/settings/language":
        send({"mode": "zh", "effective": "zh", "source": "dashboard"})
    elif path == "/api/health":
        send({"status": "healthy", "version": "fixture"})
    elif path == "/api/projects":
        send(
            [
                {
                    "id": PROJECT,
                    "name": "无团队项目",
                    "root_path": "/fixture",
                    "status": "active",
                    "created_at": NOW,
                    "config": {},
                }
            ],
            True,
        )
    elif path == "/api/teams":
        send([], True)
    elif path == f"/api/projects/{PROJECT}/task-wall":
        send(wall)
    elif path == f"/api/projects/{PROJECT}/task-wall/digest":
        send(digest)
    elif path == f"/api/projects/{PROJECT}/tasks":
        TaskCreateBody.model_validate(body)
        if state["create_fail"]:
            send({"detail": "创建失败fixture"}, status=400)
        else:
            send({**task, **body}, True)
    elif path == f"/api/tasks/{PARENT}/complete":
        TaskCompleteBody.model_validate(body)
        state["complete_count"] += 1
        if state["complete_success"]:
            assert body["expected_revision"] == 4
            task.update(status="completed", revision=5, completed_at=NOW)
            row["subtask_progress"]["completed"] = 2
            send(task, True)
        else:
            send(
                {
                    "detail": {
                        "message": "父任务尚有未完成子任务",
                        "open_subtasks": [{"id": CHILD, "title": "未关子任务fixture"}],
                    }
                },
                status=409,
            )
    elif path == f"/api/tasks/{PARENT}" and method == "PUT":
        TaskUpdateBody.model_validate(body)
        if state["conflict"]:
            state["conflict"] = False
            send(
                {
                    "detail": {
                        "message": "版本冲突",
                        "current_revision": 3,
                        "current_task": {**task, "revision": 3, "description": "另一人更新的说明"},
                    }
                },
                status=409,
            )
        else:
            assert body["expected_revision"] == 3
            Task.model_validate({**task, **body})
            task.update(body)
            task["revision"] = 4
            send(task, True)
    elif path == f"/api/tasks/{PARENT}":
        send(task, True)
    elif path == f"/api/tasks/{PARENT}/subtasks":
        send({"parent_id": PARENT, "subtasks": row["subtasks"], "total": len(row["subtasks"])}, True)
    elif path == f"/api/tasks/{PARENT}/memo":
        send([], True)
    elif path == "/api/notices/summary":
        send({"notices": 0, "briefings": 0, "tasks": 1, "total": 1, "top": None, "language": "zh"})
    elif path == "/api/notices" or path == "/api/leader-briefings":
        send({"items": [], "total": 0})
    elif path == "/api/external-execs/reconciliation":
        if state["external_fail"]:
            send({"detail": "对账暂不可用fixture"}, status=503)
        else:
            assert query["limit"] == ["500"] and query["bucket_limit"] == ["20"]
            send(reconciliation_page(int(query["bucket_offset"][0])), True)
    elif path == "/api/external-execs/44444444-4444-4444-8444-444444444444":
        assert query["receipt_limit"] == ["100"]
        send(invocation_page(invocation, int(query["receipt_offset"][0])), True)
    elif path == "/api/external-execs/55555555-5555-4555-8555-555555555555":
        send(invocation_page(missing, int(query["receipt_offset"][0])), True)
    elif path == "/api/external-execs":
        send(
            {
                "items": [invocation_page(invocation, limit=0), invocation_page(missing, limit=0)],
                "total": 2,
                "limit": 20,
                "offset": 0,
                "truncated": False,
            },
            True,
        )
    elif path == "/api/usage/coverage":
        send({"generated_at": NOW, "rows": [], "hops": [], "excluded_rows": {}}, True)
    elif path == "/api/usage/scopes":
        send([], True)
    elif path == "/api/usage/attribution":
        send(
            {
                "scope": "project",
                "scope_id": "",
                "metric": "usage_sum",
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "dispatches_attributed": 0,
                "dispatches_total": 0,
                "unattributed_reasons": {},
                "measured_window": None,
                "method": "transcript_parse",
            },
            True,
        )
    else:
        errors.append("Unexpected API " + path)
        send({"detail": "unexpected fixture endpoint"}, status=500)


metrics = []
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, channel="chrome")
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
    context.route_web_socket("**/ws/events", lambda ws: None)
    context.route("**/*", api)
    context.add_init_script(
        """window.__metrics={lcp:0,cls:0,fid:null};
        new PerformanceObserver(l=>{
          for(const e of l.getEntries())window.__metrics.lcp=e.startTime
        }).observe({type:'largest-contentful-paint',buffered:true});
        new PerformanceObserver(l=>{
          for(const e of l.getEntries())if(!e.hadRecentInput)window.__metrics.cls+=e.value
        }).observe({type:'layout-shift',buffered:true});
        new PerformanceObserver(l=>{
          for(const e of l.getEntries())window.__metrics.fid=e.processingStart-e.startTime
        }).observe({type:'first-input',buffered:true});"""
    )
    page = context.new_page()
    page.set_default_timeout(10000)
    page.on("pageerror", lambda error: errors.append(str(error)))

    def console(message):
        if message.type != "error" or any(status in message.text for status in ["400", "409", "503"]):
            return
        if message.location.get("url", "").endswith("/vite.svg"):
            known_asset_errors.append(message.text)
        else:
            errors.append(message.text)

    page.on("console", console)
    page.goto(origin + "/tasks")
    page.get_by_role("button", name="创建任务", exact=True).wait_for()
    assert page.get_by_role("button", name="创建任务", exact=True).is_enabled()
    page.get_by_role("button", name="创建任务", exact=True).click()
    page.get_by_label("任务标题", exact=True).fill("项目任务fixture")
    page.get_by_label("父任务", exact=True).select_option(PARENT)
    page.get_by_role("checkbox", name="未关子任务fixture", exact=True).check()
    page.get_by_label("等待对象", exact=True).select_option("user")
    page.get_by_label("等待理由", exact=True).fill("确认范围")
    page.get_by_label("周期", exact=True).select_option("mid")
    page.screenshot(path=str(OUTPUT / "task-create-title-selection.png"), full_page=True)
    state["create_fail"] = True
    page.get_by_role("button", name="提交", exact=True).click()
    page.get_by_role("alert").wait_for()
    assert page.get_by_label("任务标题", exact=True).input_value() == "项目任务fixture"
    state["create_fail"] = False
    page.get_by_role("button", name="提交", exact=True).click()
    page.get_by_role("dialog").wait_for(state="hidden")
    created = next(r for r in requests if r["path"].endswith("/tasks") and r["method"] == "POST")["body"]
    assert created["parent_id"] == PARENT and created["depends_on"] == [CHILD]
    assert "team_id" not in created and created["waiting_on"]["kind"] == "user"
    page.get_by_role("button", name="发布验收父任务", exact=True).focus()
    assert page.get_by_role("button", name="发布验收父任务", exact=True).evaluate("el => el === document.activeElement")
    page.get_by_role("button", name="发布验收父任务", exact=True).press("Enter")
    page.get_by_role("button", name="标记完成", exact=True).click()
    page.get_by_role("alert").get_by_role("button", name="未关子任务fixture", exact=True).wait_for()
    page.screenshot(path=str(OUTPUT / "task-complete-conflict.png"), full_page=True)
    page.get_by_role("button", name="编辑任务", exact=True).click()
    page.get_by_label("任务描述", exact=True).fill("我的输入必须保留")
    page.get_by_role("button", name="清空前置任务", exact=True).click()
    page.get_by_label("父任务", exact=True).select_option("")
    page.get_by_label("当前状态说明", exact=True).fill("")
    page.get_by_label("负责人", exact=True).fill("")
    page.get_by_label("结果", exact=True).fill("")
    page.get_by_label("等待对象", exact=True).select_option("none")
    page.get_by_role("button", name="保存", exact=True).click()
    page.get_by_text("另一人更新的说明", exact=True).wait_for()
    assert page.get_by_label("任务描述", exact=True).input_value() == "我的输入必须保留"
    page.screenshot(path=str(OUTPUT / "task-revision-conflict.png"), full_page=True)
    page.get_by_role("button", name="基于最新版本重试", exact=True).click()
    page.get_by_label("任务描述", exact=True).wait_for(state="hidden")
    updates = [r["body"] for r in requests if r["path"] == f"/api/tasks/{PARENT}" and r["method"] == "PUT"]
    assert len(updates) == 2 and updates[-1]["expected_revision"] == 3
    for body in updates:
        assert body["waiting_on"] is None and body["depends_on"] == [] and "parent_id" not in body
        assert body["status_note"] == "" and body["assigned_to"] is None and body["result"] == ""
        assert "status" not in body, "editing unrelated fields must not convert waiting blockage to manual blockage"
    page.get_by_text("我的输入必须保留", exact=True).wait_for()
    state["complete_success"] = True
    page.get_by_role("button", name="标记完成", exact=True).click()
    page.get_by_role("dialog").get_by_text("已完成", exact=True).wait_for()
    page.screenshot(path=str(OUTPUT / "task-detail-completed.png"), full_page=False)
    assert not page.get_by_role("button", name="标记完成", exact=True).count()
    assert not page.get_by_role("dialog").get_by_text("执行者已停止，任务尚未完成", exact=True).count()
    page.get_by_role("button", name="Close", exact=True).click()
    metrics.append({"width": 1440, "page": "task-core-interactions", **page.evaluate("window.__metrics")})
    for width in [375, 768, 1024, 1440]:
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto(origin + "/tasks")
        page.get_by_role("button", name="创建任务", exact=True).wait_for()
        page.screenshot(path=str(OUTPUT / f"task-wall-{width}.png"), full_page=True)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth && "
            "document.querySelector('main').scrollWidth <= document.querySelector('main').clientWidth"
        ), f"overflow at {width}"
        metrics.append({"width": width, "page": "tasks", **page.evaluate("window.__metrics")})
    page.goto(origin + "/briefings")
    page.get_by_role("heading", name="等用户决定的任务", exact=True).wait_for()
    page.get_by_text("等用户: 请确认验收范围", exact=True).wait_for()
    page.screenshot(path=str(OUTPUT / "briefings-waiting-user.png"), full_page=True)
    page.goto(origin + "/usage")
    page.get_by_text("外部 Codex exec 对账", exact=True).wait_for()
    page.get_by_text("结果已截断： journal", exact=True).wait_for()
    scope_text = page.get_by_text("证据时间范围:", exact=False).inner_text()
    assert " - " in scope_text and " — " not in scope_text
    page.get_by_text("当前页证据覆盖：1/2 条已记录 usage", exact=False).first.wait_for()
    page.get_by_text("已结束但无 usage 回执", exact=False).click()
    page.get_by_text("55555555-5555-4555-8555-555555555555 · interrupted", exact=True).wait_for()
    assert not any(r["path"] == "/api/external-execs/44444444-4444-4444-8444-444444444444" for r in requests)
    page.locator("summary").filter(has_text="44444444-4444-4444-8444-444444444444").click()
    page.get_by_text("模型 / provider / tier: 未知 / 未知 / 未知", exact=True).first.wait_for()
    assert page.get_by_text("turn-long · turn: turn-long", exact=True).locator("..").locator(
        "dd"
    ).all_text_contents() == ["0", "0", "—", "0", "0", "0"]
    assert (
        page.get_by_text("turn-missing · turn: 未知", exact=True).locator("..").locator("dd").all_text_contents()
        == ["—"] * 6
    )
    page.get_by_text(
        "回合累计回执（不是精确请求次数） · native_activity：原生执行活动回执", exact=True
    ).first.wait_for()
    page.get_by_text("关联状态: 隔离保存 · 项目不存在", exact=True).wait_for()
    execution = page.locator("summary").filter(has_text="44444444-4444-4444-8444-444444444444").locator("..")
    execution.get_by_role("button", name="下一页", exact=True).click()
    page.get_by_text("thread-resumed · turn: turn-resumed", exact=True).wait_for()
    page.get_by_text(
        "线程累计回执（含先前回合，不按回合相加） · native_activity：原生执行活动回执", exact=True
    ).first.wait_for()
    zero_fields = (
        page.get_by_text("zero-unverified · turn: 未知", exact=True).locator("..").locator("dd").all_text_contents()
    )
    assert zero_fields == [
        "0 (零值未核实)",
        "0 (零值未核实)",
        "—",
        "0 (零值未核实)",
        "0 (零值未核实)",
        "0 (零值未核实)",
    ]
    assert not execution.get_by_role("button", name="下一页", exact=True).is_enabled()
    page.get_by_text("原生事件返回全零，但未证明真实用量为零；保留原值并标为未核实。", exact=True).wait_for()
    page.get_by_role("button", name="下一页桶明细", exact=True).click()
    page.get_by_text("已发现但未登记线程", exact=False).click()
    page.get_by_text("unregistered-thread-20", exact=False).wait_for()
    assert not page.get_by_role("button", name="下一页桶明细", exact=True).is_enabled()
    page.get_by_role("button", name="上一页桶明细", exact=True).click()
    page.locator("summary").filter(has_text="55555555-5555-4555-8555-555555555555").click()
    page.get_by_text("无 usage 回执，用量未知", exact=True).wait_for()
    page.get_by_text("关联状态: 已拒绝 · 任务不存在", exact=True).wait_for()
    for width in [375, 768, 1024, 1440]:
        page.set_viewport_size({"width": width, "height": 1000})
        page.locator("summary").filter(has_text="44444444-4444-4444-8444-444444444444").scroll_into_view_if_needed()
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth && "
            "document.querySelector('main').scrollWidth <= document.querySelector('main').clientWidth"
        ), f"usage overflow at {width}"
        page.screenshot(path=str(OUTPUT / f"external-exec-expanded-{width}.png"), full_page=False)
    page.screenshot(path=str(OUTPUT / "external-exec-reconciliation.png"), full_page=False)
    page.set_viewport_size({"width": 375, "height": 1000})
    page.locator("summary").filter(has_text="44444444-4444-4444-8444-444444444444").scroll_into_view_if_needed()
    page.screenshot(path=str(OUTPUT / "external-exec-reconciliation-mobile.png"), full_page=False)
    page.get_by_text("zero-unverified · turn: 未知", exact=True).scroll_into_view_if_needed()
    page.screenshot(path=str(OUTPUT / "external-exec-zero-unverified-375.png"), full_page=False)
    state["external_fail"] = True
    page.get_by_role("button", name="重新取数", exact=True).click()
    page.get_by_text("加载失败: 对账暂不可用fixture", exact=False).wait_for(timeout=10000)
    page.screenshot(path=str(OUTPUT / "external-exec-error.png"), full_page=True)
    assert not errors, errors
    browser.close()
server.shutdown()
(OUTPUT / "task-usage-validation.json").write_text(
    json.dumps(
        {
            "passed": True,
            "metrics": metrics,
            "requests": requests,
            "errors": errors,
            "known_baseline_asset_errors": known_asset_errors,
        },
        ensure_ascii=False,
        indent=2,
    )
)
print(json.dumps({"passed": True, "screenshots": str(OUTPUT), "metrics": metrics}, ensure_ascii=False))
