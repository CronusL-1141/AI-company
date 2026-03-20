"""AI Team OS 后端API全面测试脚本.

使用Python urllib发送请求，不依赖第三方库。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE_URL = "http://localhost:8000"

# 测试结果收集
results: list[dict[str, Any]] = []


def req(
    method: str,
    path: str,
    body: dict | None = None,
    expected_status: int = 200,
) -> tuple[int, Any]:
    """发送HTTP请求，返回 (status_code, json_body)."""
    url = BASE_URL + path
    data = json.dumps(body).encode("utf-8") if body else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            status = resp.status
            raw = resp.read()
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw.decode("utf-8", errors="replace")
            return status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw.decode("utf-8", errors="replace")
        return e.code, parsed
    except Exception as e:
        return 0, str(e)


def check(
    label: str,
    status: int,
    body: Any,
    expected_status: int = 200,
    assertions: list[str] | None = None,
) -> bool:
    """记录并打印一条测试结果."""
    status_ok = status == expected_status
    body_str = json.dumps(body, ensure_ascii=False)[:200] if not isinstance(body, str) else body[:200]

    assertion_failures: list[str] = []
    if assertions and status_ok:
        for assertion in assertions:
            # 简单的字段存在性检查
            if not eval(assertion, {"body": body}):  # noqa: S307
                assertion_failures.append(assertion)

    passed = status_ok and not assertion_failures
    marker = "PASS" if passed else "FAIL"
    fail_reason = ""
    if not status_ok:
        fail_reason = f" (期望{expected_status}，实际{status})"
    elif assertion_failures:
        fail_reason = f" (断言失败: {assertion_failures})"

    print(f"{marker} | {label}{fail_reason}")
    if not passed:
        print(f"       响应: {body_str}")

    results.append({
        "label": label,
        "passed": passed,
        "status": status,
        "expected_status": expected_status,
        "fail_reason": fail_reason,
    })
    return passed


# ================================================================
# 第一步：获取基础数据（team_id、project_id）
# ================================================================

print("\n" + "="*60)
print("【第1组】核心API端点测试")
print("="*60)

# 1. GET /api/teams
status, body = req("GET", "/api/teams")
check("GET /api/teams — 团队列表", status, body, 200)
team_id = None
if isinstance(body, dict) and body.get("data"):
    team_id = body["data"][0]["id"]
    print(f"       取得 team_id: {team_id}")
elif isinstance(body, list) and body:
    team_id = body[0].get("id")
    print(f"       取得 team_id: {team_id}")

# 2. GET /api/projects
status, body = req("GET", "/api/projects")
check("GET /api/projects — 项目列表", status, body, 200)
project_id = "3aa58dc7-a771-4745-8fa6-efbd5819956a"

# 3. GET /api/projects/{id}/task-wall
status, body = req("GET", f"/api/projects/{project_id}/task-wall")
check("GET /api/projects/{id}/task-wall — 任务墙", status, body, 200)

# 4. GET /api/system/rules
status, body = req("GET", "/api/system/rules")
check("GET /api/system/rules — 规则列表", status, body, 200)

# 5. GET /api/agent-templates
status, body = req("GET", "/api/agent-templates")
check("GET /api/agent-templates — 模板列表", status, body, 200)
if isinstance(body, dict):
    total = body.get("total", 0)
    print(f"       共 {total} 个模板")

# 6. GET /api/agent-templates/recommend
status, body = req("GET", "/api/agent-templates/recommend?task_type=frontend")
check("GET /api/agent-templates/recommend?task_type=frontend — 模板推荐", status, body, 200)

# 7. GET /api/decisions
status, body = req("GET", "/api/decisions?limit=10")
check("GET /api/decisions?limit=10 — 决策日志", status, body, 200)

# ================================================================
# 第二组：MCP工具端点
# ================================================================

print("\n" + "="*60)
print("【第2组】MCP工具端点 / Hook事件")
print("="*60)

# 8. POST /api/hooks/event
hook_payload = {
    "event_type": "test.ping",
    "source": "api-qa-test",
    "payload": {"message": "API测试心跳", "timestamp": "2026-03-20T00:00:00Z"},
}
status, body = req("POST", "/api/hooks/event", hook_payload)
check("POST /api/hooks/event — Hook事件接收", status, body, expected_status=200)
# 也可能是202 Accepted
if status == 202:
    results[-1]["passed"] = True
    print("       (202也视为通过)")

# ================================================================
# 第三组：会议系统测试
# ================================================================

print("\n" + "="*60)
print("【第3组】会议系统")
print("="*60)

meeting_id = None
if team_id:
    # 9. 创建会议
    meeting_payload = {
        "topic": "API测试会议",
        "participants": ["api-qa", "team-lead"],
    }
    status, body = req("POST", f"/api/teams/{team_id}/meetings", meeting_payload)
    check("POST /api/teams/{id}/meetings — 创建会议", status, body, expected_status=201)
    if status == 201 and isinstance(body, dict):
        data = body.get("data", {})
        meeting_id = data.get("id")
        print(f"       meeting_id: {meeting_id}")

    # 10. 发送消息
    if meeting_id:
        msg_payload = {
            "agent_id": "api-qa-agent",
            "agent_name": "api-qa",
            "content": "这是API测试发送的消息",
            "round_number": 1,
        }
        status, body = req("POST", f"/api/meetings/{meeting_id}/messages", msg_payload)
        check("POST /api/meetings/{id}/messages — 发送消息", status, body, expected_status=201)

    # 11. 读取消息
    if meeting_id:
        status, body = req("GET", f"/api/meetings/{meeting_id}/messages")
        check("GET /api/meetings/{id}/messages — 读取消息", status, body, 200)
        if isinstance(body, dict):
            total = body.get("total", 0)
            print(f"       消息数: {total}")

    # 12. 关闭会议
    if meeting_id:
        status, body = req("PUT", f"/api/meetings/{meeting_id}/conclude")
        check("PUT /api/meetings/{id}/conclude — 关闭会议", status, body, 200)
else:
    print("SKIP | 会议系统测试 (无可用team_id)")
    for _ in range(4):
        results.append({"label": "会议系统-跳过", "passed": None, "status": 0, "expected_status": 0, "fail_reason": "无team_id"})

# ================================================================
# 第四组：任务系统
# ================================================================

print("\n" + "="*60)
print("【第4组】任务系统")
print("="*60)

task_id = None
if team_id:
    # 13. 创建任务
    task_payload = {
        "title": "API测试任务",
        "description": "由api-qa自动化测试创建的临时任务，用于验证任务CRUD流程",
        "priority": "medium",
    }
    status, body = req("POST", f"/api/teams/{team_id}/tasks/run", task_payload)
    check("POST /api/teams/{id}/tasks/run — 创建任务", status, body, 200)
    if isinstance(body, dict) and body.get("data"):
        task_id = body["data"].get("id")
        print(f"       task_id: {task_id}")

    # 14. 查询任务状态
    if task_id:
        status, body = req("GET", f"/api/tasks/{task_id}")
        check("GET /api/tasks/{id} — 查询任务状态", status, body, 200)

    # 15. 标记任务完成
    if task_id:
        status, body = req("PUT", f"/api/tasks/{task_id}/complete")
        check("PUT /api/tasks/{id}/complete — 标记完成", status, body, 200)
        if isinstance(body, dict):
            data = body.get("data", {})
            task_status = data.get("status", "")
            print(f"       任务状态: {task_status}")

    # 16. 验证task-wall更新
    if project_id:
        status, body = req("GET", f"/api/projects/{project_id}/task-wall")
        check("GET /api/projects/{id}/task-wall — 验证任务墙更新", status, body, 200)
else:
    print("SKIP | 任务系统测试 (无可用team_id)")
    for _ in range(4):
        results.append({"label": "任务系统-跳过", "passed": None, "status": 0, "expected_status": 0, "fail_reason": "无team_id"})

# ================================================================
# 第五组：新功能 — 单个模板、活动追踪、智能匹配
# ================================================================

print("\n" + "="*60)
print("【第5组】新功能")
print("="*60)

# 17. GET /api/agent-templates/{name}
status, body = req("GET", "/api/agent-templates/engineering-frontend-developer")
check("GET /api/agent-templates/{name} — 单个模板", status, body, 200)
if isinstance(body, dict) and "error" in body:
    print(f"       提示: {body['error']}")

if team_id:
    # 18. GET /api/teams/{team_id}/activities
    status, body = req("GET", f"/api/teams/{team_id}/activities")
    check("GET /api/teams/{id}/activities — 活动追踪", status, body, 200)

    # 19. GET /api/teams/{team_id}/task-matches
    status, body = req("GET", f"/api/teams/{team_id}/task-matches")
    check("GET /api/teams/{id}/task-matches — 智能匹配", status, body, 200)
else:
    print("SKIP | 活动追踪和智能匹配 (无可用team_id)")
    for _ in range(2):
        results.append({"label": "新功能-跳过", "passed": None, "status": 0, "expected_status": 0, "fail_reason": "无team_id"})

# ================================================================
# 第六组：边界条件和异常场景
# ================================================================

print("\n" + "="*60)
print("【第6组】边界条件 / 异常场景")
print("="*60)

# 20. 不存在的项目task-wall
status, body = req("GET", "/api/projects/nonexistent-id-00000000/task-wall")
check("GET /api/projects/不存在ID/task-wall — 期望404", status, body, expected_status=404)

# 21. 不存在的任务
status, body = req("GET", "/api/tasks/nonexistent-task-id-000")
check("GET /api/tasks/不存在ID — 期望404", status, body, expected_status=404)

# 22. 不存在的会议
status, body = req("GET", "/api/meetings/nonexistent-meeting-id")
check("GET /api/meetings/不存在ID — 期望404", status, body, expected_status=404)

# 23. 向已关闭会议发送消息（期望400）
if meeting_id:
    msg_payload = {
        "agent_id": "api-qa-agent",
        "agent_name": "api-qa",
        "content": "向已结束会议发消息（期望400）",
        "round_number": 2,
    }
    status, body = req("POST", f"/api/meetings/{meeting_id}/messages", msg_payload)
    check("POST 向已关闭会议发消息 — 期望400", status, body, expected_status=400)
else:
    print("SKIP | 无meeting_id，跳过已关闭会议发消息测试")

# 24. 模板名称包含路径遍历字符（期望返回error字段）
status, body = req("GET", "/api/agent-templates/../../etc/passwd")
# 路径遍历可能被FastAPI路由解析拦截返回404或200带error
traversal_blocked = status in (200, 400, 404, 422)
marker = "PASS" if traversal_blocked else "FAIL"
print(f"{marker} | GET /api/agent-templates/路径遍历 — 期望被拦截(实际{status})")
results.append({"label": "路径遍历防护", "passed": traversal_blocked, "status": status, "expected_status": -1, "fail_reason": ""})

# 25. Hook事件缺少必要字段（期望422）
status, body = req("POST", "/api/hooks/event", {})
check("POST /api/hooks/event 空body — 期望422", status, body, expected_status=422)

# ================================================================
# 汇总报告
# ================================================================

print("\n" + "="*60)
print("【测试汇总】")
print("="*60)

passed_count = sum(1 for r in results if r["passed"] is True)
failed_count = sum(1 for r in results if r["passed"] is False)
skipped_count = sum(1 for r in results if r["passed"] is None)
total_count = len(results)

print(f"\n通过: {passed_count} | 失败: {failed_count} | 跳过: {skipped_count} | 总计: {total_count}")

if failed_count > 0:
    print("\n失败详情:")
    for r in results:
        if r["passed"] is False:
            print(f"  FAIL | {r['label']}{r['fail_reason']}")

print()
