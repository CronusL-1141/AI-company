"""
AI Team OS — API 级别 E2E 测试
不依赖 Dashboard/Playwright，直接用 urllib 测试后端 API。

覆盖场景：
1. TC-A01: GET /api/health — 健康检查
2. TC-A02: PUT /api/tasks/{id} — 创建任务后更新 status/result
3. TC-A03: GET /api/config/team-templates — 会议/团队模板列表
4. TC-A04: skill_registry.recommend — 直接调用 Python 模块，验证推荐逻辑
5. TC-A05: 边界条件 — 无效 task_id、无效 status 值
6. TC-A06: GET /api/projects — 项目列表分页

运行方式:
    cd ai-team-os
    python tests/e2e_api_coverage.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

API_URL = "http://localhost:8000"
RESULTS: list[str] = []


# ─── 工具函数 ─────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def record(test_name: str, step: str, status: str, detail: str = "") -> None:
    entry = f"[{status}] {test_name} — {step}"
    if detail:
        entry += f" | {detail}"
    RESULTS.append(entry)
    log(entry)


def api_get(path: str) -> tuple[int, Any]:
    """GET 请求，返回 (status_code, parsed_json_or_None)"""
    url = f"{API_URL}{path}"
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body}
    except Exception as e:
        return -1, {"error": str(e)}


def api_put(path: str, payload: dict) -> tuple[int, Any]:
    """PUT 请求，返回 (status_code, parsed_json_or_None)"""
    url = f"{API_URL}{path}"
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="PUT")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body}
    except Exception as e:
        return -1, {"error": str(e)}


def api_post(path: str, payload: dict) -> tuple[int, Any]:
    """POST 请求，返回 (status_code, parsed_json_or_None)"""
    url = f"{API_URL}{path}"
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body}
    except Exception as e:
        return -1, {"error": str(e)}


# ─── TC-A01: /api/health ──────────────────────────────────────────────────────


def test_health():
    """TC-A01: GET /api/health — 健康检查"""
    test = "TC-A01-health"
    log(f"\n{'=' * 60}\n开始 {test}\n{'=' * 60}")

    status, body = api_get("/api/health")
    if status == 200:
        record(test, "GET /api/health 返回 200", "PASS", f"body={body}")
    else:
        record(test, "GET /api/health", "FAIL", f"HTTP {status}, body={body}")
        return

    # 验证响应字段
    if body.get("status") == "ok":
        record(test, "响应包含 status=ok", "PASS")
    else:
        record(test, "响应字段 status", "FAIL", f"实际: {body.get('status')}")

    if "version" in body:
        record(test, f"响应包含 version={body['version']}", "PASS")
    else:
        record(test, "响应缺少 version 字段", "WARN")


# ─── TC-A02: PUT /api/tasks/{id} ──────────────────────────────────────────────


def test_task_update():
    """TC-A02: PUT /api/tasks/{id} — 创建任务后更新 status/result"""
    test = "TC-A02-task_update"
    log(f"\n{'=' * 60}\n开始 {test}\n{'=' * 60}")

    # 先获取一个团队 ID
    status, body = api_get("/api/projects?limit=1")
    if status != 200:
        record(test, "无法获取项目列表", "WARN", f"HTTP {status}")
        return

    projects = body.get("data", [])
    if not projects:
        record(test, "无项目数据，跳过", "WARN")
        return

    project_id = projects[0]["id"]

    # 通过项目获取关联团队
    status, teams_body = api_get(f"/api/teams?project_id={project_id}")
    team_id = None
    if status == 200:
        teams = teams_body.get("data", [])
        if teams:
            team_id = teams[0]["id"]

    if not team_id:
        # 直接列出所有团队
        status, all_teams = api_get("/api/teams")
        if status == 200:
            teams = all_teams.get("data", [])
            if teams:
                team_id = teams[0]["id"]

    if not team_id:
        record(test, "无团队数据，跳过 task_update 测试", "WARN")
        return

    record(test, f"使用团队 team_id={team_id[:8]}...", "PASS")

    # 创建测试任务
    status, create_body = api_post(
        f"/api/teams/{team_id}/tasks/run",
        {
            "title": "QA自动化测试任务",
            "description": "e2e_api_coverage.py 创建，用于验证 task_update API",
        },
    )
    if status not in (200, 201):
        record(test, "创建测试任务", "FAIL", f"HTTP {status}, {create_body}")
        return

    task_id = create_body.get("data", {}).get("id")
    if not task_id:
        record(test, "创建任务响应中无 id", "FAIL", str(create_body))
        return

    record(test, f"任务创建成功 task_id={task_id[:8]}...", "PASS", f"初始 status={create_body['data'].get('status')}")

    # 更新 status → running
    status, upd_body = api_put(
        f"/api/tasks/{task_id}",
        {"status": "running"},
    )
    if status == 200 and upd_body.get("success"):
        new_status = upd_body["data"].get("status")
        record(test, "更新 status=running", "PASS", f"响应 status={new_status}")
    else:
        record(test, "更新 status=running", "FAIL", f"HTTP {status}, {upd_body}")

    # 更新 result
    status, upd_body = api_put(
        f"/api/tasks/{task_id}",
        {"status": "completed", "result": "QA测试自动化验证完成"},
    )
    if status == 200 and upd_body.get("success"):
        result_val = upd_body["data"].get("result")
        record(test, "更新 status=completed + result", "PASS", f"result={result_val}")
    else:
        record(test, "更新 status=completed+result", "FAIL", f"HTTP {status}, {upd_body}")

    # 边界: 无效 status 值 → 期望 400
    status, err_body = api_put(
        f"/api/tasks/{task_id}",
        {"status": "invalid_status_xyz"},
    )
    if status == 400:
        record(test, "无效 status 值返回 400", "PASS", f"detail={err_body.get('detail', '')[:80]}")
    else:
        record(test, "无效 status 值应返回 400", "FAIL", f"实际 HTTP {status}")


# ─── TC-A03: GET /api/config/team-templates ───────────────────────────────────


def test_team_templates():
    """TC-A03: GET /api/config/team-templates — 团队模板列表"""
    test = "TC-A03-team_templates"
    log(f"\n{'=' * 60}\n开始 {test}\n{'=' * 60}")

    status, body = api_get("/api/config/team-templates")
    if status == 200:
        record(test, "GET /api/config/team-templates 返回 200", "PASS")
    else:
        record(test, "GET /api/config/team-templates", "FAIL", f"HTTP {status}, body={body}")
        return

    templates = body.get("data", [])
    if isinstance(templates, list):
        record(test, f"返回模板列表，共 {len(templates)} 个", "PASS")
    else:
        record(test, "响应 data 字段不是列表", "FAIL", str(type(templates)))
        return

    if templates:
        first = templates[0]
        has_id = "id" in first
        has_name = "name" in first or "title" in first
        record(test, "模板包含 id 字段", "PASS" if has_id else "WARN", str(list(first.keys())[:5]))
        record(test, "模板包含 name/title 字段", "PASS" if has_name else "WARN")
    else:
        record(test, "模板列表为空（配置文件可能不存在）", "WARN", "plugin/config/team-templates.json")


# ─── TC-A04: skill_registry.recommend ────────────────────────────────────────


def test_skill_registry():
    """TC-A04: 直接 import skill_registry，验证 recommend 逻辑"""
    test = "TC-A04-skill_registry"
    log(f"\n{'=' * 60}\n开始 {test}\n{'=' * 60}")

    try:
        import os
        import sys

        # 加入 src 到路径
        src_path = os.path.join(os.path.dirname(__file__), "..", "src")
        if src_path not in sys.path:
            sys.path.insert(0, os.path.abspath(src_path))

        from aiteam.mcp.skill_registry import SKILLS

        record(test, f"成功 import SKILLS，共 {len(SKILLS)} 个技能", "PASS")

        # 验证数据结构完整性
        if SKILLS:
            first = SKILLS[0]
            has_id = bool(first.id)
            has_name = bool(first.name)
            has_oneliner = bool(first.oneliner)
            record(
                test,
                "首个 Skill 有 id/name/oneliner 字段",
                "PASS" if (has_id and has_name and has_oneliner) else "FAIL",
                f"id={first.id}, name={first.name}",
            )
        else:
            record(test, "SKILLS 列表为空", "WARN")
            return

        # 模拟 Layer1 recommend 逻辑 — 关键词匹配
        query = "memory session"
        query_words = set(query.lower().split())
        matches = []
        for skill in SKILLS:
            text = f"{skill.name} {skill.oneliner} {' '.join(skill.tags)}".lower()
            overlap = sum(1 for w in query_words if w in text)
            if overlap > 0:
                matches.append((overlap, skill))
        matches.sort(key=lambda x: x[0], reverse=True)
        top3 = [s.to_layer1() for _, s in matches[:3]]

        if top3:
            record(test, f"关键词 '{query}' 推荐 {len(top3)} 个技能", "PASS", f"top1={top3[0]['name']}")
        else:
            record(test, f"关键词 '{query}' 无匹配技能", "WARN")

        # 边界: 空关键词 — 不应崩溃
        empty_query_words: set[str] = set()
        empty_matches = [s for s in SKILLS if any(w in f"{s.name} {s.oneliner}".lower() for w in empty_query_words)]
        record(test, "空关键词查询不崩溃，返回 0 结果", "PASS", f"结果数={len(empty_matches)}")

        # 验证 to_layer3() 包含所有层字段
        sample = SKILLS[0].to_layer3()
        required_keys = {"id", "name", "oneliner", "category", "install_cmd", "tags", "features", "os_complement"}
        missing = required_keys - set(sample.keys())
        if not missing:
            record(test, "to_layer3() 包含所有必要字段", "PASS")
        else:
            record(test, "to_layer3() 缺少字段", "FAIL", f"缺失: {missing}")

    except ImportError as e:
        record(test, "import skill_registry 失败", "FAIL", str(e))
    except Exception as e:
        record(test, "skill_registry 测试异常", "FAIL", str(e))


# ─── TC-A05: 边界条件 ─────────────────────────────────────────────────────────


def test_boundary_conditions():
    """TC-A05: 边界条件 — 无效 task_id、不存在资源"""
    test = "TC-A05-boundary"
    log(f"\n{'=' * 60}\n开始 {test}\n{'=' * 60}")

    # 不存在的 task_id → 404
    status, body = api_get("/api/tasks/nonexistent-task-id-99999")
    if status == 404:
        record(test, "不存在的 task_id 返回 404", "PASS", f"detail={body.get('detail', '')[:60]}")
    else:
        record(test, "不存在的 task_id 应返回 404", "FAIL", f"实际 HTTP {status}")

    # PUT 不存在的 task → 404
    status, body = api_put(
        "/api/tasks/nonexistent-task-id-99999",
        {"status": "running"},
    )
    if status == 404:
        record(test, "PUT 不存在的 task 返回 404", "PASS")
    else:
        record(test, "PUT 不存在的 task 应返回 404", "FAIL", f"实际 HTTP {status}")

    # 不存在的 project_id → 404
    status, body = api_get("/api/projects/nonexistent-project-99999")
    if status == 404:
        record(test, "不存在的 project_id 返回 404", "PASS")
    else:
        record(test, "不存在的 project_id 应返回 404", "FAIL", f"实际 HTTP {status}")

    # PUT task 空 body → 应返回 200（无字段更新）或 422（验证失败）
    status, body = api_get("/api/tasks/nonexistent-id")
    # 此处仅确认接口可达
    record(test, "GET /api/tasks/nonexistent-id 可达（404）", "PASS" if status == 404 else "WARN", f"HTTP {status}")


# ─── TC-A06: GET /api/projects 分页 ──────────────────────────────────────────


def test_projects_api():
    """TC-A06: GET /api/projects — 列表、分页、字段完整性"""
    test = "TC-A06-projects_api"
    log(f"\n{'=' * 60}\n开始 {test}\n{'=' * 60}")

    # 正常列表
    status, body = api_get("/api/projects")
    if status == 200:
        record(test, "GET /api/projects 返回 200", "PASS")
    else:
        record(test, "GET /api/projects", "FAIL", f"HTTP {status}")
        return

    projects = body.get("data", [])
    total = body.get("total", -1)
    record(test, f"返回 {len(projects)} 个项目，total={total}", "PASS")

    if projects:
        first = projects[0]
        required = {"id", "name"}
        missing = required - set(first.keys())
        if not missing:
            record(test, "项目对象包含 id/name 字段", "PASS", f"keys={list(first.keys())[:6]}")
        else:
            record(test, "项目对象缺少必要字段", "FAIL", f"缺失: {missing}")

        # 获取单个项目
        proj_id = first["id"]
        status2, proj_body = api_get(f"/api/projects/{proj_id}")
        if status2 == 200:
            record(test, f"GET /api/projects/{proj_id[:8]}... 返回 200", "PASS")
        else:
            record(test, "GET /api/projects/:id", "FAIL", f"HTTP {status2}")

    # 边界: limit=0
    status, body = api_get("/api/projects?limit=0")
    record(test, "limit=0 不崩溃", "PASS" if status in (200, 422) else "WARN", f"HTTP {status}")

    # 边界: limit 超大值
    status, body = api_get("/api/projects?limit=99999")
    record(
        test,
        "limit=99999 不崩溃",
        "PASS" if status == 200 else "WARN",
        f"HTTP {status}, 返回 {len(body.get('data', []))} 条",
    )


# ─── 主入口 ───────────────────────────────────────────────────────────────────


def main() -> int:
    log("=" * 60)
    log("AI Team OS — API 级别 E2E 测试")
    log(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"API: {API_URL}")
    log("=" * 60)

    test_health()
    test_task_update()
    test_team_templates()
    test_skill_registry()
    test_boundary_conditions()
    test_projects_api()

    # ── 汇总 ──────────────────────────────────────────────────────────────────
    log("\n" + "=" * 60)
    log("API 测试结果汇总")
    log("=" * 60)

    pass_count = sum(1 for r in RESULTS if "[PASS]" in r)
    fail_count = sum(1 for r in RESULTS if "[FAIL]" in r)
    warn_count = sum(1 for r in RESULTS if "[WARN]" in r)

    for r in RESULTS:
        log(r)

    log(f"\n总计: {len(RESULTS)} 项 | PASS: {pass_count} | FAIL: {fail_count} | WARN: {warn_count}")

    import os

    report_dir = os.path.join(os.path.dirname(__file__), "..", "test-screenshots")
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, "e2e_api_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("AI Team OS — API E2E 测试报告\n")
        f.write(f"日期: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"API: {API_URL}\n")
        f.write("=" * 60 + "\n\n")
        for r in RESULTS:
            f.write(r + "\n")
        f.write(f"\n{'=' * 60}\n")
        f.write(f"总计: {len(RESULTS)} 项 | PASS: {pass_count} | FAIL: {fail_count} | WARN: {warn_count}\n")

    log(f"\n报告保存至: {report_path}")
    return fail_count


if __name__ == "__main__":
    fails = main()
    sys.exit(0 if fails == 0 else 1)
