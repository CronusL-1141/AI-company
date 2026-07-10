"""Unit tests for autopilot_auto_stop hook — 4 cases.

All HTTP calls are mocked; no real server needed.
"""
import io
import json


def _load_hook():
    import aiteam.hooks.autopilot_auto_stop as m
    return m


def _run_hook(payload: dict, monkeypatch, *, project_id=None, autopilot_tasks=None,
              stop_returns=True, api_reachable=True):
    """Run autopilot_auto_stop.main() with mocked internals; return (exit_code, stderr)."""
    m = _load_hook()

    raw = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr("sys.stdin", type("FB", (), {"buffer": io.BytesIO(raw)})())
    stderr_buf = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr_buf)

    def fake_resolve(cwd):
        if not api_reachable:
            return None
        return project_id

    def fake_find(proj_id):
        if not api_reachable:
            return []
        return autopilot_tasks or []

    def fake_stop(task_id):
        if not api_reachable:
            return False
        return stop_returns

    def fake_memo(task_id):
        pass

    monkeypatch.setattr(m, "_resolve_project_id", fake_resolve)
    monkeypatch.setattr(m, "_find_autopilot_tasks", fake_find)
    monkeypatch.setattr(m, "_stop_autopilot", fake_stop)
    monkeypatch.setattr(m, "_add_task_memo", fake_memo)

    exit_code = 0
    try:
        m.main()
    except SystemExit as e:
        exit_code = e.code if e.code is not None else 0

    return exit_code, stderr_buf.getvalue()


class TestAutopilotAutoStop:
    def test_stops_active_autopilot_tasks(self, monkeypatch):
        """When there are autopilot_active tasks, they get stopped and stderr logged."""
        tasks = [{"id": "task-001"}, {"id": "task-002"}]
        stopped = []

        m = _load_hook()
        raw = json.dumps({"cwd": "/proj"}).encode("utf-8")
        monkeypatch.setattr("sys.stdin", type("FB", (), {"buffer": io.BytesIO(raw)})())
        stderr_buf = io.StringIO()
        monkeypatch.setattr("sys.stderr", stderr_buf)

        monkeypatch.setattr(m, "_resolve_project_id", lambda cwd: "proj-1")
        monkeypatch.setattr(m, "_find_autopilot_tasks", lambda pid: tasks)
        monkeypatch.setattr(m, "_stop_autopilot", lambda tid: stopped.append(tid) or True)
        monkeypatch.setattr(m, "_add_task_memo", lambda tid: None)

        exit_code = 0
        try:
            m.main()
        except SystemExit as e:
            exit_code = e.code if e.code is not None else 0

        assert exit_code == 0
        assert "task-001" in stopped
        assert "task-002" in stopped
        assert "autopilot" in stderr_buf.getvalue().lower()

    def test_silent_when_no_autopilot_tasks(self, monkeypatch):
        """When no autopilot tasks exist, exits cleanly with no stderr."""
        code, stderr = _run_hook(
            {"cwd": "/proj"},
            monkeypatch,
            project_id="proj-1",
            autopilot_tasks=[],
        )
        assert code == 0
        assert stderr == ""

    def test_fail_open_when_api_unreachable(self, monkeypatch):
        """When API is down, hook exits 0 without raising."""
        code, _ = _run_hook(
            {"cwd": "/proj"},
            monkeypatch,
            api_reachable=False,
        )
        assert code == 0

    def test_stops_multiple_tasks(self, monkeypatch):
        """All autopilot tasks are stopped, not just the first one."""
        task_ids = ["task-A", "task-B", "task-C"]
        tasks = [{"id": tid} for tid in task_ids]
        stopped = []

        m = _load_hook()
        raw = json.dumps({"cwd": "/proj"}).encode("utf-8")
        monkeypatch.setattr("sys.stdin", type("FB", (), {"buffer": io.BytesIO(raw)})())
        monkeypatch.setattr("sys.stderr", io.StringIO())

        monkeypatch.setattr(m, "_resolve_project_id", lambda cwd: "proj-1")
        monkeypatch.setattr(m, "_find_autopilot_tasks", lambda pid: tasks)
        monkeypatch.setattr(m, "_stop_autopilot", lambda tid: stopped.append(tid) or True)
        monkeypatch.setattr(m, "_add_task_memo", lambda tid: None)

        exit_code = 0
        try:
            m.main()
        except SystemExit as e:
            exit_code = e.code if e.code is not None else 0

        assert exit_code == 0
        assert set(stopped) == set(task_ids)
