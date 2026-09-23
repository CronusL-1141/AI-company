"""Hook 入口 guardrail 只标记不拦（用户 2026-09-23 裁定，任务墙 d3e0d6bb）。

hook 载荷来自本机 CC/Codex 进程，工具输入/输出里正常出现的上跳路径、执行调用、脚本标签、
强删、删表字样命中 L1 规则就整条 400：Pre/Post 事件丢失，只丢 Post 的让 span 卡在 running。
现在 POST /api/hooks/event 照扫、照记日志，命中的规则名记进这次 hook 产生的事件，请求放行；
其它路由、其它方法、形似路径、2MB 上限都不变。同批收紧 content-type 判定：大小写变体与
+json 后缀不再能绕过扫描。

整条生产 HTTP 栈（create_app）+ 临时文件库 + 真 HookTranslator；断言跨持久化边界直接查
events 表。载荷照 CC 生产形状构造，触发文本拼接生成，本文件原文不带这些字样。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from aiteam.api import app as app_module
from aiteam.api import debug_log, deps
from aiteam.api import event_bus as event_bus_module
from aiteam.api.event_bus import EventBus
from aiteam.api.guardrails import _DANGEROUS_RULES
from aiteam.api.hook_translator import GUARDRAIL_FLAGS_FIELD, HookTranslator
from aiteam.api.middleware import _MAX_BODY_BYTES, _rule_id
from aiteam.api.routes import hooks
from aiteam.api.ws.manager import ConnectionManager
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository

DOTDOT = ".." + "/"
CWD = "/tmp/aiteam-guardrail-flags"


def _base(sid: str, event: str) -> dict:
    return {"session_id": sid, "transcript_path": f"/tmp/fake/{sid}.jsonl", "cwd": CWD,
            "permission_mode": "default", "hook_event_name": event}


def _tool_use_id() -> str:
    return "toolu_" + uuid.uuid4().hex[:24]


def _traversal_bash() -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": _tool_use_id(),
            "tool_input": {"command": f"diff {DOTDOT}{DOTDOT}plugin/hooks/send_event.py src/aiteam/hooks/send_event.py",
                           "description": "Compare hook copies"}}


def _exec_read() -> dict:
    content = "import runpy\n\ndef main(code):\n    " + "ex" + "ec(compile(code, 'x', 'exec'))\n"
    return {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_use_id": _tool_use_id(),
            "tool_input": {"file_path": f"{CWD}/scripts/run.py"},
            "tool_response": {"type": "text", "file": {"filePath": f"{CWD}/scripts/run.py", "content": content,
                                                       "numLines": 4, "startLine": 1, "totalLines": 4}}}


def _script_write() -> dict:
    content = "<!doctype html><title>Chart</title>" + "<" + "script src=\"chart.js\"></" + "script>"
    return {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_use_id": _tool_use_id(),
            "tool_input": {"file_path": f"{CWD}/page.html", "content": content}}


def _rm_bash() -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": _tool_use_id(),
            "tool_input": {"command": "rm" + " -rf /tmp/aiteam-build-cache", "description": "Clear cache"}}


def _drop_stdout() -> dict:
    return {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": _tool_use_id(),
            "tool_input": {"command": "grep -rn legacy migrations/", "description": "Find legacy"},
            "tool_response": {"stdout": "migrations/0003.sql:1:" + "DROP" + " TABLE legacy_x;", "stderr": "",
                              "interrupted": False, "isImage": False}}


# (shape, rule ID recorded as the flag, event type the translator emits for it)
SHAPES = [
    pytest.param(_traversal_bash, "path_traversal", "cc.tool_use", id="traversal-bash-command"),
    pytest.param(_exec_read, "code_injection_exec", "cc.tool_complete", id="exec-read-file-content"),
    pytest.param(_script_write, "xss_script_tag", "cc.tool_use", id="script-write-content"),
    pytest.param(_rm_bash, "destructive_shell_command", "cc.tool_use", id="rm-bash-command"),
    pytest.param(_drop_stdout, "sql_drop_table", "cc.tool_complete", id="drop-bash-stdout"),
]
# 400 响应里的 violations 仍是规则显示名（删表规则的显示名本身就是触发文本，不写原文）。
_LABEL_BY_ID = {_rule_id(label): label for _, label in _DANGEROUS_RULES}


def _payload(shape) -> tuple[str, dict]:
    sid = f"guardrail-flags-{uuid.uuid4().hex[:12]}"
    extra = shape()
    return sid, {**_base(sid, extra["hook_event_name"]), **extra}


@pytest_asyncio.fixture
async def hook_app(tmp_path, monkeypatch):
    # 生产 HTTP 栈照原样，只摘掉 MCP 挂载和共享的文件日志；ASGITransport 不跑 lifespan，
    # 依赖成套换成隔离库上的实例（RequestLedger 直接取 deps 单例，同步替换）。
    monkeypatch.setattr(debug_log, "setup_debug_log", lambda: None)
    monkeypatch.setattr(app_module, "_get_mcp_http_app", lambda: None)
    app = app_module.create_app()
    database = tmp_path / "guardrail-flags.sqlite"
    database_url = f"sqlite+aiosqlite:///{database}"
    repo = StorageRepository(db_url=database_url)
    await repo.init_db()
    bus = EventBus(repo=repo)
    translator = HookTranslator(repo=repo, event_bus=bus)
    app.dependency_overrides.update({
        deps.get_repository: lambda: repo,
        deps.get_event_bus: lambda: bus,
        deps.get_hook_translator: lambda: translator,
    })
    monkeypatch.setattr(deps, "_repository", repo)
    monkeypatch.setattr(deps, "_event_bus", bus)
    monkeypatch.setattr(deps, "_hook_translator", translator)
    monkeypatch.setattr(event_bus_module, "ws_manager", ConnectionManager())
    monkeypatch.setattr(event_bus_module.cfg, "SLACK_WEBHOOK_URL", "")
    monkeypatch.delenv(hooks.HOOK_RAW_DUMP_ENV, raising=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, database
    finally:
        await translator.drain(5.0)
        app.dependency_overrides.clear()
        await get_engine(database_url).dispose()


async def _rows(database, sid: str) -> list[tuple[str, dict]]:
    """Events this session's payload produced, read back from the database file."""
    async with aiosqlite.connect(database) as db:
        async with db.execute(
            "SELECT type, data FROM events WHERE source = ? OR data LIKE ? ORDER BY timestamp",
            (f"session:{sid}", f"%{sid}%"),
        ) as cursor:
            return [(t, json.loads(d)) for t, d in await cursor.fetchall()]


@pytest.mark.asyncio
@pytest.mark.parametrize(("shape", "rule", "event_type"), SHAPES)
async def test_flagged_hook_is_recorded_with_its_rule(hook_app, shape, rule, event_type):
    client, database = hook_app
    sid, payload = _payload(shape)

    response = await client.post("/api/hooks/event", json=payload)

    assert response.status_code == 200
    rows = await _rows(database, sid)
    assert event_type in [t for t, _ in rows]
    assert all(data.get(GUARDRAIL_FLAGS_FIELD) == [rule] for _, data in rows), rows

    # 落库的标记会随事件 data 被引用、转发到其它受 guardrail 保护的入口：它自己不能再触发规则。
    quoted = await client.post("/api/hooks/diagnose_denial", json={
        "tool_name": "Bash", "reason": "audit",
        "tool_input": {"quoted_event": {GUARDRAIL_FLAGS_FIELD: rows[0][1][GUARDRAIL_FLAGS_FIELD]}},
    })
    assert quoted.status_code == 200, quoted.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("shape", "rule", "event_type"), SHAPES)
@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/api/hooks/diagnose_denial"),
    ("POST", "/api/teams"),
    ("POST", "/api/hooks/eventx"),
    ("POST", "/api/hooks/event/"),
    ("PUT", "/api/hooks/event"),
    ("PATCH", "/api/hooks/event"),
], ids=["sibling-route", "other-route", "lookalike", "trailing-slash", "put", "patch"])
async def test_same_body_elsewhere_is_still_blocked(hook_app, shape, rule, event_type, method, path):
    client, database = hook_app
    sid, payload = _payload(shape)

    response = await client.request(method, path, json=payload)

    assert response.status_code == 400
    assert any(v.endswith(_LABEL_BY_ID[rule]) for v in response.json()["violations"])
    assert await _rows(database, sid) == []


@pytest.mark.asyncio
async def test_get_on_hook_path_gains_nothing(hook_app):
    # GET 从来不扫，也没有这条 GET 路由：不豁免即行为不变（不成功、零写库）。
    client, database = hook_app
    sid, payload = _payload(_traversal_bash)

    response = await client.request("GET", "/api/hooks/event", json=payload)

    assert not response.is_success
    assert await _rows(database, sid) == []


@pytest.mark.asyncio
async def test_oversized_hook_body_still_rejected_413(hook_app):
    client, database = hook_app
    sid, payload = _payload(_traversal_bash)
    payload["tool_input"]["pad"] = "x" * (_MAX_BODY_BYTES + 1024)

    response = await client.post("/api/hooks/event", json=payload)

    assert response.status_code == 413
    assert await _rows(database, sid) == []


@pytest.mark.asyncio
async def test_clean_hook_events_carry_no_flag_field(hook_app):
    client, database = hook_app
    sid = f"guardrail-clean-{uuid.uuid4().hex[:12]}"
    payload = {**_base(sid, "PreToolUse"), "tool_name": "Bash", "tool_use_id": _tool_use_id(),
               "tool_input": {"command": "git status --porcelain"}}

    response = await client.post("/api/hooks/event", json=payload)

    assert response.status_code == 200
    rows = await _rows(database, sid)
    assert "cc.tool_use" in [t for t, _ in rows]
    assert all(GUARDRAIL_FLAGS_FIELD not in data for _, data in rows)


@pytest.mark.asyncio
async def test_body_supplied_flags_are_discarded(hook_app):
    """标记只认服务端扫描结果：请求体自带的同名字段既不能伪造标记，也不能盖掉真标记。"""
    client, database = hook_app
    clean_sid = f"guardrail-forged-{uuid.uuid4().hex[:12]}"
    clean = {**_base(clean_sid, "PreToolUse"), "tool_name": "Bash", "tool_use_id": _tool_use_id(),
             "tool_input": {"command": "git status"}, GUARDRAIL_FLAGS_FIELD: ["forged"]}
    flagged_sid, flagged = _payload(_rm_bash)
    flagged[GUARDRAIL_FLAGS_FIELD] = ["forged"]

    assert (await client.post("/api/hooks/event", json=clean)).status_code == 200
    assert (await client.post("/api/hooks/event", json=flagged)).status_code == 200

    clean_rows = await _rows(database, clean_sid)
    assert clean_rows and all(GUARDRAIL_FLAGS_FIELD not in data for _, data in clean_rows)
    flagged_rows = await _rows(database, flagged_sid)
    assert flagged_rows
    assert all(data[GUARDRAIL_FLAGS_FIELD] == ["destructive_shell_command"] for _, data in flagged_rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", [
    "Application/JSON", "APPLICATION/JSON; charset=utf-8", "application/vnd.aiteam+json",
])
async def test_content_type_spelling_no_longer_bypasses_the_scan(hook_app, content_type):
    """以前只认区分大小写的 "application/json" 子串：换个写法，任何路由都能带着触发文本进来。"""
    client, database = hook_app
    sid, payload = _payload(_traversal_bash)
    body = json.dumps(payload).encode()

    sibling = await client.post("/api/hooks/diagnose_denial", content=body, headers={"content-type": content_type})
    hook = await client.post("/api/hooks/event", content=body, headers={"content-type": content_type})

    assert sibling.status_code == 400
    assert hook.status_code == 200
    rows = await _rows(database, sid)
    assert rows and all(data.get(GUARDRAIL_FLAGS_FIELD) == ["path_traversal"] for _, data in rows)


async def test_flags_only_tag_events_of_the_handling_task():
    """后台作业另起 task、继承 context 副本：它们的事件不是这份载荷直接产生的，不打标；
    处理结束后标记也不会漏到同一 task 的下一次调用。"""
    emitted: list[tuple[str, dict]] = []

    class _Bus:
        async def emit(self, event_type, source, data, **kwargs):
            emitted.append((event_type, data))

    bus = _Bus()
    translator = HookTranslator(repo=None, event_bus=bus)  # type: ignore[arg-type]
    background: list[asyncio.Task] = []

    async def _background_job() -> None:
        await translator.event_bus.emit("bg.job", "s", {"k": 2})

    async def _handler(payload: dict) -> dict:
        assert GUARDRAIL_FLAGS_FIELD not in payload
        await translator.event_bus.emit("in.task", "s", {"k": 1})
        background.append(asyncio.create_task(_background_job()))
        return {"ok": True}

    translator._handle_event = _handler  # type: ignore[method-assign]

    result = await translator.handle_event({"hook_event_name": "PreToolUse", GUARDRAIL_FLAGS_FIELD: ["path_traversal"]})
    await asyncio.gather(*background)
    await translator.event_bus.emit("after", "s", {"k": 3})

    assert result == {"ok": True}
    assert emitted == [
        ("in.task", {"k": 1, GUARDRAIL_FLAGS_FIELD: ["path_traversal"]}),
        ("bg.job", {"k": 2}),
        ("after", {"k": 3}),
    ]
    assert translator.event_bus is bus
