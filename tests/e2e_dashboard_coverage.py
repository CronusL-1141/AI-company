"""
AI Team OS Dashboard — 补充 E2E 测试
覆盖现有测试未包含的页面和场景：
- ProjectsPage (/projects)
- ProjectDetailPage (/projects/:id)
- MeetingsPage (/meetings)
- TeamsPage (通过 dashboard / team detail 路由的 /teams 实际上是 ProjectDetail 内嵌)

前置条件:
- 后端 API 运行在 http://localhost:8000
- Dashboard (Vite) 运行在 http://localhost:5173
- playwright 已安装: pip install playwright && playwright install chromium

运行方式:
    cd ai-team-os
    python tests/e2e_dashboard_coverage.py
"""

import os
import traceback
from datetime import datetime

from playwright.sync_api import sync_playwright

BASE_URL = "http://localhost:5173"
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


def screenshot(page, name: str) -> None:
    dir_ = os.path.join(os.path.dirname(__file__), "..", "test-screenshots")
    os.makedirs(dir_, exist_ok=True)
    path = os.path.join(dir_, name)
    page.screenshot(path=path, full_page=True)
    log(f"  Screenshot: {name}")


# ─── 健康检查 ─────────────────────────────────────────────────────────────────


def test_api_health(page):
    """TC-000: 后端 API 可达性检查"""
    test = "TC000-API健康检查"
    try:
        log(f"\n{'=' * 60}\n开始 {test}\n{'=' * 60}")
        resp = page.request.get(f"{API_URL}/api/health")
        if resp.status == 200:
            record(test, "GET /api/health 返回 200", "PASS")
        else:
            record(test, "GET /api/health", "FAIL", f"HTTP {resp.status}")
    except Exception as e:
        record(test, "API连通性", "FAIL", str(e))


# ─── TC-001: ProjectsPage ─────────────────────────────────────────────────────


def test_projects_page(page):
    """TC-001: 项目列表页 — 页面可访问、核心元素渲染、创建项目对话框"""
    test = "TC001-ProjectsPage"
    try:
        log(f"\n{'=' * 60}\n开始 {test}\n{'=' * 60}")

        page.goto(f"{BASE_URL}/projects", wait_until="networkidle")
        page.wait_for_timeout(1500)
        screenshot(page, "e2e-projects-list.png")

        # 1. 页面标题可见
        try:
            heading = page.locator("h1").first
            heading_text = heading.inner_text() if heading.count() > 0 else ""
            record(test, f"页面标题可见: '{heading_text}'", "PASS" if heading_text else "WARN")
        except Exception as e:
            record(test, "页面标题", "FAIL", str(e))

        # 2. 核心元素: 表格 或 空状态
        try:
            table = page.locator("table")
            empty_hint = page.locator("text=暂无项目, text=没有项目")
            if table.count() > 0:
                rows = page.locator("table tbody tr")
                record(test, f"项目表格可见，行数: {rows.count()}", "PASS")
            elif empty_hint.count() > 0:
                record(test, "空状态提示可见", "PASS")
            else:
                record(test, "表格或空状态", "WARN", "两者均未找到")
        except Exception as e:
            record(test, "表格渲染验证", "FAIL", str(e))

        # 3. 创建项目按钮可见
        try:
            create_btn = page.locator("button:has-text('创建项目'), button:has-text('新建项目')")
            if create_btn.count() > 0:
                record(test, "创建项目按钮存在", "PASS")
            else:
                # 尝试 Plus 图标按钮
                plus_btn = page.locator("button svg.lucide-plus").locator("..")
                record(test, "创建按钮 (Plus图标)", "PASS" if plus_btn.count() > 0 else "WARN")
        except Exception as e:
            record(test, "创建按钮验证", "FAIL", str(e))

        # 4. 打开创建项目对话框
        try:
            create_btn = page.locator("button").filter(has_text="创建").first
            if create_btn.count() > 0:
                create_btn.click()
                page.wait_for_timeout(600)
                dialog = page.locator("[role='dialog']")
                if dialog.count() > 0:
                    screenshot(page, "e2e-projects-create-dialog.png")
                    record(test, "创建项目对话框打开", "PASS")
                    # 验证表单字段
                    name_input = page.locator("[role='dialog'] input").first
                    if name_input.count() > 0:
                        record(test, "对话框包含输入字段", "PASS")
                    else:
                        record(test, "对话框表单字段", "WARN", "未找到input")
                    # 关闭对话框
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(400)
                else:
                    record(test, "创建对话框打开", "WARN", "dialog元素未出现")
        except Exception as e:
            record(test, "创建对话框交互", "FAIL", str(e))

        # 5. 边界测试: 空名称创建（如对话框已打开）
        try:
            create_btn = page.locator("button").filter(has_text="创建").first
            if create_btn.count() > 0:
                create_btn.click()
                page.wait_for_timeout(500)
                dialog = page.locator("[role='dialog']")
                if dialog.count() > 0:
                    # 不填名称，点提交
                    submit = (
                        page.locator("[role='dialog'] button[type='submit'], [role='dialog'] button")
                        .filter(has_text="创建")
                        .last
                    )
                    submit.click()
                    page.wait_for_timeout(500)
                    # 对话框应仍然存在（未提交成功）
                    still_open = page.locator("[role='dialog']").count() > 0
                    record(test, "空名称不提交(对话框未关闭)", "PASS" if still_open else "WARN", "空名称应被阻止提交")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
        except Exception as e:
            record(test, "空名称边界测试", "WARN", str(e))

    except Exception as e:
        record(test, "整体", "FAIL", str(e))
        traceback.print_exc()


# ─── TC-002: ProjectDetailPage ────────────────────────────────────────────────


def test_project_detail_page(page):
    """TC-002: 项目详情页 — 通过首个项目进入，验证活动日志、决策时间线"""
    test = "TC002-ProjectDetailPage"
    try:
        log(f"\n{'=' * 60}\n开始 {test}\n{'=' * 60}")

        # 先获取项目列表，拿到第一个项目ID
        page.goto(f"{BASE_URL}/projects", wait_until="networkidle")
        page.wait_for_timeout(1500)

        # 优先通过 API 获取第一个项目 ID，直接导航到详情页（最可靠）
        resp = page.request.get(f"{API_URL}/api/projects?limit=1")
        if resp.status == 200:
            data = resp.json()
            projects = data.get("data", [])
            if projects:
                project_id = projects[0]["id"]
                page.goto(f"{BASE_URL}/projects/{project_id}", wait_until="networkidle")
                page.wait_for_timeout(2000)
            else:
                # 无项目时尝试点击列表页的查看按钮
                view_btn = page.locator(
                    "button:has-text('查看'), a:has-text('查看'), "
                    "button:has-text('详情'), a:has-text('详情'), "
                    "tr td a, table a"
                ).first
                if view_btn.count() > 0:
                    view_btn.click()
                    page.wait_for_timeout(2000)
                else:
                    record(test, "无项目数据，跳过详情页测试", "WARN", "需要先创建项目")
                    return
        else:
            record(test, "无法获取项目列表", "WARN", f"API {resp.status}")
            return

        current_url = page.url
        if "/projects/" not in current_url:
            record(test, "URL应包含/projects/:id", "FAIL", f"当前URL: {current_url}")
            return

        screenshot(page, "e2e-project-detail.png")
        record(test, f"进入项目详情页: {current_url}", "PASS")

        # 1. 项目名称/标题可见
        try:
            heading = page.locator("h1, h2").first
            heading_text = heading.inner_text() if heading.count() > 0 else ""
            record(test, f"项目标题可见: '{heading_text[:30]}'", "PASS" if heading_text else "WARN")
        except Exception as e:
            record(test, "项目标题", "FAIL", str(e))

        # 2. 团队/Agent 区域
        try:
            agent_section = page.locator("text=Agent, text=成员, text=团队成员")
            if agent_section.count() > 0:
                record(test, "Agent/成员区域可见", "PASS")
            else:
                record(test, "Agent/成员区域", "WARN", "未找到相关标签")
        except Exception as e:
            record(test, "Agent区域验证", "FAIL", str(e))

        # 3. 活动日志区域
        try:
            activity_labels = ["活动", "日志", "Activity", "历史"]
            found_activity = False
            for label in activity_labels:
                if page.locator(f"text={label}").count() > 0:
                    found_activity = True
                    record(test, f"活动区域可见 (匹配: '{label}')", "PASS")
                    break
            if not found_activity:
                record(test, "活动日志区域", "WARN", "未找到活动/日志标签")
        except Exception as e:
            record(test, "活动日志验证", "FAIL", str(e))

        # 4. 决策时间线
        try:
            decision_labels = ["决策", "时间线", "Decision"]
            found_decision = False
            for label in decision_labels:
                if page.locator(f"text={label}").count() > 0:
                    found_decision = True
                    record(test, f"决策区域可见 (匹配: '{label}')", "PASS")
                    break
            if not found_decision:
                record(test, "决策时间线区域", "WARN", "未找到决策相关标签")
        except Exception as e:
            record(test, "决策时间线验证", "FAIL", str(e))

        # 5. 返回按钮
        try:
            back_btn = page.locator("button:has-text('返回'), a:has-text('返回'), [aria-label='back']")
            if back_btn.count() > 0:
                record(test, "返回按钮存在", "PASS")
            else:
                record(test, "返回按钮", "WARN", "未找到返回按钮")
        except Exception as e:
            record(test, "返回按钮验证", "FAIL", str(e))

        # 6. 执行任务按钮
        try:
            run_task_btn = page.locator(
                "button:has-text('执行任务'), button:has-text('运行任务'), button:has-text('派发')"
            )
            if run_task_btn.count() > 0:
                record(test, "执行任务按钮可见", "PASS")
            else:
                record(test, "执行任务按钮", "WARN", "未找到")
        except Exception as e:
            record(test, "执行任务按钮验证", "FAIL", str(e))

        screenshot(page, "e2e-project-detail-full.png")

    except Exception as e:
        record(test, "整体", "FAIL", str(e))
        traceback.print_exc()


# ─── TC-003: MeetingsPage ─────────────────────────────────────────────────────


def test_meetings_page(page):
    """TC-003: 会议列表页 — 页面可访问、状态筛选、创建会议对话框"""
    test = "TC003-MeetingsPage"
    try:
        log(f"\n{'=' * 60}\n开始 {test}\n{'=' * 60}")

        page.goto(f"{BASE_URL}/meetings", wait_until="networkidle")
        page.wait_for_timeout(1500)
        screenshot(page, "e2e-meetings-list.png")

        # 1. 页面标题
        try:
            heading = page.locator("h1").first
            heading_text = heading.inner_text() if heading.count() > 0 else ""
            record(test, f"页面标题: '{heading_text}'", "PASS" if heading_text else "WARN")
        except Exception as e:
            record(test, "页面标题", "FAIL", str(e))

        # 2. 状态筛选按钮组 (全部/进行中/已结束)
        try:
            filter_buttons = ["全部", "进行中", "已结束"]
            found_filters = 0
            for label in filter_buttons:
                btn = page.locator(f"button:has-text('{label}')")
                if btn.count() > 0:
                    found_filters += 1
            if found_filters >= 2:
                record(test, f"状态筛选按钮组存在 ({found_filters}/3)", "PASS")
            else:
                record(test, f"状态筛选按钮 ({found_filters}/3)", "WARN", "部分筛选按钮未找到")
        except Exception as e:
            record(test, "状态筛选验证", "FAIL", str(e))

        # 3. 点击"进行中"筛选
        try:
            active_btn = page.locator("button").filter(has_text="进行中").first
            if active_btn.count() > 0:
                active_btn.click()
                page.wait_for_timeout(800)
                screenshot(page, "e2e-meetings-active-filter.png")
                record(test, "点击'进行中'筛选", "PASS")
            else:
                record(test, "'进行中'筛选按钮", "WARN", "未找到")
        except Exception as e:
            record(test, "筛选交互", "FAIL", str(e))

        # 4. 点击"已结束"筛选
        try:
            concluded_btn = page.locator("button").filter(has_text="已结束").first
            if concluded_btn.count() > 0:
                concluded_btn.click()
                page.wait_for_timeout(800)
                screenshot(page, "e2e-meetings-concluded-filter.png")
                record(test, "点击'已结束'筛选", "PASS")
        except Exception as e:
            record(test, "'已结束'筛选", "WARN", str(e))

        # 5. 回到"全部"
        try:
            all_btn = page.locator("button").filter(has_text="全部").first
            if all_btn.count() > 0:
                all_btn.click()
                page.wait_for_timeout(600)
        except Exception:
            pass

        # 6. 创建会议按钮（只有存在团队时才显示）
        try:
            create_btn = page.locator("button:has-text('创建会议'), button:has-text('新建会议')")
            if create_btn.count() > 0:
                record(test, "创建会议按钮可见", "PASS")
                # 打开对话框
                create_btn.first.click()
                page.wait_for_timeout(600)
                dialog = page.locator("[role='dialog']")
                if dialog.count() > 0:
                    screenshot(page, "e2e-meetings-create-dialog.png")
                    record(test, "创建会议对话框打开", "PASS")
                    # 验证 team 选择器 和 topic 输入
                    topic_input = page.locator("#meeting-topic")
                    if topic_input.count() > 0:
                        record(test, "会议主题输入框存在", "PASS")
                    else:
                        record(test, "会议主题输入框", "WARN", "未找到 #meeting-topic")
                    team_select = page.locator("#meeting-team")
                    if team_select.count() > 0:
                        record(test, "团队选择器存在", "PASS")
                    # 边界: 空主题点提交，按钮应为 disabled
                    submit_btn = page.locator("[role='dialog'] button[disabled]")
                    record(test, "空主题时提交按钮为禁用", "PASS" if submit_btn.count() > 0 else "WARN")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(400)
                else:
                    record(test, "创建对话框", "WARN", "dialog未出现")
            else:
                record(test, "创建会议按钮不可见（无团队时预期）", "WARN")
        except Exception as e:
            record(test, "创建会议交互", "FAIL", str(e))

        # 7. 空状态 或 会议卡片
        try:
            meeting_cards = page.locator("[class*='card'], [class*='Card']")
            no_meetings = page.locator("text=暂无会议, text=没有会议")
            if meeting_cards.count() > 2:  # 页面上有内容卡片
                record(test, f"会议卡片渲染 (约{meeting_cards.count()}个元素)", "PASS")
            elif no_meetings.count() > 0:
                record(test, "空状态提示可见", "PASS")
            else:
                record(test, "会议列表或空状态", "WARN", "两者均未找到明确标识")
        except Exception as e:
            record(test, "会议内容验证", "FAIL", str(e))

    except Exception as e:
        record(test, "整体", "FAIL", str(e))
        traceback.print_exc()


# ─── TC-004: Navigation — 导航完整性 ─────────────────────────────────────────


def test_navigation_completeness(page):
    """TC-004: 侧边栏导航完整性 — 所有路由可访问无 404"""
    test = "TC004-导航完整性"
    routes = [
        ("/", "首页/Dashboard"),
        ("/projects", "项目列表"),
        ("/tasks", "任务看板"),
        ("/events", "事件日志"),
        ("/meetings", "会议列表"),
        ("/analytics", "数据分析"),
        ("/settings", "系统设置"),
    ]
    try:
        log(f"\n{'=' * 60}\n开始 {test}\n{'=' * 60}")
        for route, name in routes:
            try:
                resp = page.goto(f"{BASE_URL}{route}", wait_until="domcontentloaded")
                page.wait_for_timeout(800)
                status = resp.status if resp else -1
                # React SPA 所有路由都返回 200（index.html），通过页面渲染判断
                has_content = page.locator("main, #root, [class*='layout']").count() > 0
                record(
                    test,
                    f"{route} ({name}) 可访问",
                    "PASS" if has_content and status == 200 else "FAIL",
                    f"HTTP {status}",
                )
            except Exception as e:
                record(test, f"{route} 访问", "FAIL", str(e))
    except Exception as e:
        record(test, "整体", "FAIL", str(e))
        traceback.print_exc()


# ─── TC-005: 边界条件 — 无效路由 ─────────────────────────────────────────────


def test_invalid_routes(page):
    """TC-005: 无效路由处理 — 404 页面或重定向"""
    test = "TC005-无效路由"
    try:
        log(f"\n{'=' * 60}\n开始 {test}\n{'=' * 60}")

        # 不存在的项目ID
        page.goto(f"{BASE_URL}/projects/nonexistent-id-99999", wait_until="networkidle")
        page.wait_for_timeout(1500)
        screenshot(page, "e2e-invalid-project.png")

        has_error = (
            page.locator("text=404, text=找不到, text=不存在, text=加载失败").count() > 0
            or page.locator("[class*='error'], [class*='destructive']").count() > 0
        )
        record(test, "无效项目ID有错误处理", "PASS" if has_error else "WARN", "建议显示404或错误提示")

        # 完全不存在的路由
        page.goto(f"{BASE_URL}/this-route-does-not-exist", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        has_any_content = page.locator("#root").count() > 0
        record(test, "未知路由有内容渲染（SPA回退）", "PASS" if has_any_content else "FAIL")

    except Exception as e:
        record(test, "整体", "FAIL", str(e))
        traceback.print_exc()


# ─── TC-006: TeamsPage（通过项目详情访问） ────────────────────────────────────


def test_teams_via_project_detail(page):
    """TC-006: 团队管理 — 通过项目详情页访问团队/Agent管理功能"""
    test = "TC006-团队Agent管理"
    try:
        log(f"\n{'=' * 60}\n开始 {test}\n{'=' * 60}")

        # 获取首个项目
        resp = page.request.get(f"{API_URL}/api/projects?limit=1")
        if resp.status != 200:
            record(test, "无法获取项目列表", "WARN", "跳过团队测试")
            return
        projects = resp.json().get("data", [])
        if not projects:
            record(test, "无项目，跳过", "WARN")
            return

        project_id = projects[0]["id"]
        page.goto(f"{BASE_URL}/projects/{project_id}", wait_until="networkidle")
        page.wait_for_timeout(2000)
        screenshot(page, "e2e-team-via-project.png")

        # 1. 验证页面渲染了团队列表
        try:
            team_section = page.locator("text=团队, text=Teams").first
            record(test, "团队区域存在", "PASS" if team_section.count() > 0 else "WARN")
        except Exception as e:
            record(test, "团队区域", "FAIL", str(e))

        # 2. 添加 Agent 按钮
        try:
            add_agent_btn = page.locator(
                "button:has-text('添加 Agent'), button:has-text('添加Agent'), button:has-text('新增成员')"
            )
            if add_agent_btn.count() > 0:
                record(test, "添加Agent按钮存在", "PASS")
                # 打开 dialog
                add_agent_btn.first.click()
                page.wait_for_timeout(600)
                if page.locator("[role='dialog']").count() > 0:
                    screenshot(page, "e2e-add-agent-dialog.png")
                    record(test, "添加Agent对话框打开", "PASS")
                    # 边界: 空 name 提交
                    submit_btn = page.locator("[role='dialog'] button[disabled]")
                    record(test, "空Agent名称时提交按钮禁用", "PASS" if submit_btn.count() > 0 else "WARN")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(400)
            else:
                record(test, "添加Agent按钮", "WARN", "未找到（可能无团队）")
        except Exception as e:
            record(test, "添加Agent交互", "FAIL", str(e))

        # 3. Agent 状态显示
        try:
            agent_badges = page.locator("[class*='badge'], [class*='Badge']")
            record(test, f"状态标签数量: {agent_badges.count()}", "PASS")
        except Exception as e:
            record(test, "Agent状态标签", "WARN", str(e))

    except Exception as e:
        record(test, "整体", "FAIL", str(e))
        traceback.print_exc()


# ─── 主入口 ───────────────────────────────────────────────────────────────────


def main():
    log("=" * 60)
    log("AI Team OS Dashboard — 补充 E2E 测试")
    log(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Dashboard: {BASE_URL}")
    log(f"API: {API_URL}")
    log("=" * 60)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = context.new_page()
        # 设置合理超时
        page.set_default_timeout(15_000)

        test_api_health(page)
        test_navigation_completeness(page)
        test_projects_page(page)
        test_project_detail_page(page)
        test_meetings_page(page)
        test_teams_via_project_detail(page)
        test_invalid_routes(page)

        page.wait_for_timeout(1500)
        browser.close()

    # ── 汇总 ──────────────────────────────────────────────────────────────────
    log("\n" + "=" * 60)
    log("测试结果汇总")
    log("=" * 60)

    pass_count = sum(1 for r in RESULTS if "[PASS]" in r)
    fail_count = sum(1 for r in RESULTS if "[FAIL]" in r)
    warn_count = sum(1 for r in RESULTS if "[WARN]" in r)

    for r in RESULTS:
        log(r)

    log(f"\n总计: {len(RESULTS)} 项 | PASS: {pass_count} | FAIL: {fail_count} | WARN: {warn_count}")

    # ── 写入报告 ──────────────────────────────────────────────────────────────
    report_dir = os.path.join(os.path.dirname(__file__), "..", "test-screenshots")
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, "e2e_coverage_report.txt")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("AI Team OS Dashboard — 补充 E2E 测试报告\n")
        f.write(f"日期: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Dashboard: {BASE_URL} | API: {API_URL}\n")
        f.write("=" * 60 + "\n\n")
        for r in RESULTS:
            f.write(r + "\n")
        f.write(f"\n{'=' * 60}\n")
        f.write(f"总计: {len(RESULTS)} 项 | PASS: {pass_count} | FAIL: {fail_count} | WARN: {warn_count}\n")

    log(f"\n报告保存至: {report_path}")
    return fail_count


if __name__ == "__main__":
    import sys

    fails = main()
    sys.exit(0 if fails == 0 else 1)
