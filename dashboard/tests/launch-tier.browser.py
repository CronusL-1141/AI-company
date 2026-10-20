"""Build dashboard first; isolated fixture APIs, no shared service or account writes."""

import json
import mimetypes
import os
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
# Production schemas must load from this worktree after the path injection.
from aiteam.types import (  # noqa: E402
    CodexExecLaunchObservation,
    PricingAccount,
    PricingPlanCapacityEstimate,
)

DIST = ROOT / "dashboard/dist"
OUT = Path(os.environ.get("DASHBOARD_ACCEPTANCE_OUTPUT", "/tmp/aiteam-launch-tier-acceptance"))
OUT.mkdir(parents=True, exist_ok=True)
now = datetime.now(UTC)


def at(seconds):
    return (now + timedelta(seconds=seconds)).isoformat()


key = "a" * 64
account = PricingAccount.model_validate({
    "account_key": key, "label": "Launch tier fixture", "created_at": at(-86400),
}).model_dump(mode="json")
plan = PricingPlanCapacityEstimate.model_validate({
    "account_key": key, "limit_id": "codex", "window_duration_ms": 604800000,
    "resets_at": at(86400), "observed_at": at(-1), "used_percent": 12,
    "estimated_total_usd": "2651.42", "delta_usd": "26.5142", "delta_used_percent": 1,
    "start_snapshot_id": "start", "end_snapshot_id": "end", "interval_start": at(-3600),
    "status": "estimated", "source": "codex_local_logs", "pricing_mode": "launch_requested",
    "requested_tier_request_count": 910, "assumed_tier_request_count": 739, "priced_request_count": 1649,
    "prediction_basis": "cycle_anchor_missing_zero", "catalog_version": "browser-fixture",
    "catalog_sha256": "b" * 64, "reason_code": "pricing_incomplete",
}).model_dump(mode="json")
states = [
    ("unbound", "background_or_redirect"), ("planned", "awaiting_completion"),
    ("bound", "bound"), ("failed", "tool_failed"),
]
launches = [CodexExecLaunchObservation.model_validate({
    "launch_id": f"{index:064x}", "caller_session_id": "fixture-caller", "tool_use_id": f"tool-{index}",
    "command_index": index, "exec_mode": "new", "requested_service_tier": "fast",
    "requested_model": "gpt-6.1-sol", "requested_reasoning_effort": "high",
    "requested_metadata_source": "cc_bash_command",
    "state": states[index % 4][0], "binding_reason": states[index % 4][1], "pre_seen": True,
    "source_namespace": "c" * 64 if index % 4 == 2 else None,
    "thread_id": f"native-thread-{index}" if index % 4 == 2 else None,
    "execution_failed": index == 2, "background": index % 4 == 0,
    "started_at": at(-100 - index), "finished_at": None,
}).model_dump(mode="json") for index in range(21)]
detail = {"account": account, "snapshot_count": 0, "latest_snapshots": [], "estimates": [],
          "plan_estimates": [], "pricing_plan_estimates": [plan]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        file = (DIST / path.lstrip("/")).resolve()
        if not file.is_relative_to(DIST):
            self.send_error(404)
            return
        if not file.is_file():
            file = DIST / "index.html"
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.daemon_threads = True
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
origin = f"http://127.0.0.1:{server.server_port}"
requests = []
errors = []
checkpoints = []
fail_launch_read = False
browser = None
language = "zh"


def route_request(route):
    request = route.request
    url = urlsplit(request.url)
    assert request.url.startswith(origin + "/"), request.url
    if not url.path.startswith("/api/"):
        route.continue_()
        return
    requests.append({"path": url.path + ("?" + url.query if url.query else ""), "method": request.method})
    assert request.method == "GET", request.method
    if url.path == "/api/settings/language":
        payload = {"mode": "follow", "effective": language, "source": "system"}
    elif url.path == "/api/health":
        payload = {"status": "healthy", "version": "fixture"}
    else:
        data = None
        if url.path == "/api/account-usage":
            data = {"accounts": [account], "current_account_key": key}
        elif url.path == "/api/account-usage/" + key:
            data = detail
        elif url.path == "/api/account-usage/" + key + "/monitor":
            data = {"account_key": key, "settings": {"enabled": False, "interval_ms": 300000},
                    "revision": 0, "status": "disabled", "runtime_running": False,
                    "last_started_at": None, "last_finished_at": None, "next_run_at": None, "last_error": None}
        elif url.path == "/api/external-execs":
            data = {"items": [], "total": 0, "offset": 0, "limit": 20, "truncated": False}
        elif url.path == "/api/projects":
            data = []
        elif url.path == "/api/notices/summary":
            data = {"notices": 0, "briefings": 0, "tasks": 0, "total": 0, "language": language, "top": None}
        elif url.path == "/api/external-execs/launch-observations":
            if fail_launch_read:
                route.fulfill(status=503, json={"detail": "launch feed unavailable"})
                return
            offset = int(parse_qs(url.query).get("offset", ["0"])[0])
            data = {"items": launches[offset:offset + 20], "total": len(launches),
                    "offset": offset, "limit": 20, "truncated": offset + 20 < len(launches)}
        payload = {"success": True, "data": data}
    route.fulfill(json=payload)


try:
    print("START isolated browser fixture", flush=True)
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception:
            browser = playwright.chromium.launch(headless=True, channel="chrome")
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
        print("Browser launched", flush=True)
        context.route("**/*", route_request)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin + "/usage/accounts", wait_until="domcontentloaded")
        print("Account page loaded; fixture read checks starting", flush=True)
        weekly = page.get_by_role("article", name="Codex 周窗口", exact=True)
        expect(weekly).to_contain_text("$2,651.42")
        # Only the neutral basis note is shown; tier composition stays in the API.
        expect(weekly).to_contain_text("API 等值估算，非现金余额")
        expect(weekly).not_to_contain_text("档位")
        expect(weekly).not_to_contain_text("按标准价估算")
        page.screenshot(path=str(OUT / "01-requested-and-assumed-zh.png"), full_page=True)
        checkpoints.append("Chinese mixed tier estimates keep the server amount with only the neutral basis note")

        page.goto(origin + "/usage", wait_until="domcontentloaded")
        summary = page.locator("summary").filter(has_text="Codex 启动记录")
        expect(summary).to_be_visible()
        assert not any("launch-observations" in item["path"] for item in requests)
        summary.click()
        panel = summary.locator("..")
        expect(panel).to_contain_text("后台或重定向执行缺少可靠线程证据")
        for value in ["fast", "gpt-6.1-sol", "high", "拟启动，尚未确认启动", "未绑定", "已绑定线程", "启动失败"]:
            expect(panel).to_contain_text(value)
        expect(panel).to_contain_text("未绑定记录不参与档位计价")
        expect(panel.locator("li").filter(has_text="线程: native-thread-2").first).to_contain_text("命令执行失败")
        expect(panel).to_contain_text("1-20 / 21")
        panel.get_by_role("button", name="下一页", exact=True).click()
        expect(panel).to_contain_text("21-21 / 21")
        expect(panel.get_by_role("button", name="下一页", exact=True)).to_be_disabled()
        panel.get_by_role("button", name="上一页", exact=True).click()
        expect(panel).to_contain_text("1-20 / 21")
        checkpoints.append("Launch records lazy-load and paginate with explicit states and background binding reason")
        fail_launch_read = True
        page.get_by_role("button", name="重新取数", exact=True).click()
        expect(panel.get_by_role("alert")).to_contain_text("launch feed unavailable", timeout=15000)
        expect(panel).to_contain_text("gpt-6.1-sol")
        fail_launch_read = False
        page.get_by_role("button", name="重新取数", exact=True).click()
        expect(panel.get_by_role("alert")).to_have_count(0)
        checkpoints.append("Read failures retain cached evidence and explicit refresh recovers without writes")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        panel.screenshot(path=str(OUT / "02-launch-observations-mobile-zh.png"))
        checkpoints.append("Mobile launch list has no page overflow")

        page.evaluate("localStorage.setItem('lang', 'en')")
        language = "en"
        page.reload()
        en_summary = page.locator("summary").filter(has_text="Codex launch records")
        en_summary.click()
        en_panel = en_summary.locator("..")
        expect(en_panel).to_contain_text("Background or redirected execution lacks reliable thread evidence")
        expect(en_panel).to_contain_text("actual execution tiers are unobserved")
        en_panel.screenshot(path=str(OUT / "03-launch-observations-en.png"))
        page.goto(origin + "/usage/accounts", wait_until="domcontentloaded")
        weekly = page.get_by_role("article", name="Codex Weekly window", exact=True)
        expect(weekly).to_contain_text("$2,651.42")
        expect(weekly).to_contain_text("API-equivalent estimate, not a cash balance")
        expect(weekly).not_to_contain_text("tier")
        expect(weekly).not_to_contain_text("Standard rates")
        page.screenshot(path=str(OUT / "04-requested-and-assumed-en.png"), full_page=True)
        checkpoints.append("English launch evidence is explicit and plan pricing shows only the neutral basis note")
        assert not errors, errors
        result = {"checkpoints": checkpoints, "page_errors": errors, "requests": requests,
                  "schema_validation": (
                      "Production PricingAccount, PricingPlanCapacityEstimate and CodexExecLaunchObservation"
                  ),
                  "boundary": "Fixture APIs, private ephemeral static server, no shared service access or POST"}
        (OUT / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({"passed": len(checkpoints), "page_errors": errors, "output": str(OUT)}, ensure_ascii=False))
finally:
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
