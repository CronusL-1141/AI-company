"""workflow_reminder / cc_task_bridge: latency, guard order and shared-state safety.

These run the real hook file as Claude Code does (a fresh interpreter, JSON on
stdin, event name in argv) against an in-process fake OS API, with HOME pointed
at a temp dir so nothing touches the real supervisor-state.json or the real API.

What is pinned down here, each measured end to end before the fix:
  A. project resolve is cached per realpath(cwd), and only a named Agent
     dispatch (the task-wall check) resolves at all - every other call makes no
     HTTP request.
  B. local guards (S3-S6) answer before any HTTP - with the API stalled, a
     model-less Agent dispatch used to wait behind 6+ requests and get killed by
     Claude Code at 5s, which lets the tool run unblocked.
  C. S4 has a process deadline - a chain of teardown probes used to outlive the
     5s limit (single git timeouts were 5s/8s), i.e. teardown ran unassessed.
  D. concurrent writers never tear supervisor-state.json and never wipe what
     other sessions recorded (session buckets, S5 claims). There is deliberately
     no lock file, so an update may still be lost under heavy contention; the
     tests pin "no collapse", not exactness.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

import aiteam.hooks.cc_task_bridge as bridge
import aiteam.hooks.workflow_reminder as wr

HOOK = wr.__file__


# ---------------------------------------------------------------------------
# Fake OS API
# ---------------------------------------------------------------------------


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False  # stalled handlers must not hold up teardown


class _FakeApi:
    """Records every request; `delay` stalls each one (an API whose loop is stuck)."""

    def __init__(self, delay: float = 0.0, projects: dict[str, str] | None = None,
                 team_tasks: list[dict] | None = None):
        self.delay = delay
        self.projects = projects or {}
        self.team_tasks = team_tasks if team_tasks is not None else []
        self.log: list[dict] = []
        self._lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with api._lock:
                    api.log.append({
                        "method": self.command,
                        "path": self.path.split("?")[0],
                        "project": self.headers.get("X-Project-Id") or "",
                        "body": json.loads(body) if body else None,
                    })
                if api.delay:
                    time.sleep(api.delay)
                payload = api._route(self.command, self.path.split("?")[0], body)
                raw = json.dumps(payload).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass  # the hook gave up on a stalled request

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler dispatch names
                self._serve()

            def do_POST(self):  # noqa: N802
                self._serve()

            def do_PUT(self):  # noqa: N802
                self._serve()

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _route(self, method: str, path: str, body: bytes) -> dict:
        if path == "/api/context/resolve":
            cwd = json.loads(body).get("cwd", "")
            pid = self.projects.get(os.path.realpath(cwd), "proj-A")
            return {"project_id": pid, "project_name": "", "root_path": "", "created": False}
        if path == "/api/teams":
            return {"data": [{"id": "t1", "name": "team-a", "status": "active", "project_id": "proj-A"}]}
        if path == "/api/teams/t1/tasks":
            return {"data": self.team_tasks}
        if path.endswith("/tasks/running-count"):
            return {"count": 1}
        if path.endswith("/task-wall"):
            return {"wall": {"short": [{"id": "k1", "status": "running", "title": "hook latency diag"}]},
                    "stats": {"by_status": {"running": 1}}}
        return {}

    def resolves(self, cwd=None) -> list[dict]:
        with self._lock:
            hits = [r for r in self.log if r["path"] == "/api/context/resolve"]
        if cwd is not None:
            hits = [r for r in hits if os.path.realpath(r["body"]["cwd"]) == os.path.realpath(str(cwd))]
        return hits

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _env(home, api_url: str, path_prefix: str | None = None) -> dict:
    env = dict(os.environ, HOME=str(home), AITEAM_API_URL=api_url)
    env.pop("CLAUDE_PLUGIN_ROOT", None)  # never take the plugin-yield path
    if path_prefix:
        env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
    return env


def _run_hook(event: str, payload: dict, cwd, env: dict, timeout: float = 30.0):
    """Run the hook once. Returns (returncode, stdout, stderr, seconds)."""
    body = dict(payload, hook_event_name=event, cwd=str(payload.get("cwd", cwd)))
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, HOOK, event],
        input=json.dumps(body).encode(),
        capture_output=True,
        cwd=str(cwd),
        env=env,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.decode(), proc.stderr.decode(), time.perf_counter() - t0


def _run_bridge(payload: dict, cwd, env: dict) -> int:
    """Run cc_task_bridge once, as Claude Code does on TaskCompleted."""
    proc = subprocess.run(
        [sys.executable, bridge.__file__],
        input=json.dumps(dict(payload, hook_event_name="TaskCompleted", cwd=str(cwd))).encode(),
        capture_output=True,
        cwd=str(cwd),
        env=env,
        timeout=30,
    )
    return proc.returncode


def _bash(cmd: str, session: str = "sess-latency") -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": session}


def _named_agent(session: str = "sess-latency") -> dict:
    """The one call that asks the API: a named dispatch gets the task-wall check."""
    return {"tool_name": "Agent", "session_id": session,
            "tool_input": {"prompt": "hook latency diag", "description": "d", "model": "opus", "name": "w1"}}


def _state_path(home) -> str:
    return os.path.join(str(home), ".claude", "data", "ai-team-os", "supervisor-state.json")


# ---------------------------------------------------------------------------
# A. resolve cache
# ---------------------------------------------------------------------------


class TestResolveCache:
    def test_same_cwd_twenty_calls_resolve_at_most_twice(self, tmp_path):
        home, proj = tmp_path / "home", tmp_path / "proj-a"
        home.mkdir()
        proj.mkdir()
        with _FakeApi() as api:
            env = _env(home, api.url)
            for _ in range(20):
                rc, _out, _err, _t = _run_hook("PreToolUse", _named_agent(), proj, env)
                assert rc == 0
            n = len(api.resolves(proj))
        assert n <= 2, f"20 calls in one cwd made {n} resolve requests (want <= 0.1 per call)"

    @pytest.mark.parametrize("payload", [
        pytest.param(_bash("ls"), id="bash"),
        pytest.param({"tool_name": "Edit", "session_id": "s",
                      "tool_input": {"file_path": "a.py", "new_string": "x = 1"}}, id="edit"),
        pytest.param({"tool_name": "Workflow", "session_id": "s",
                      "tool_input": {"script": "await agent('x', { model: 'opus' })"}}, id="workflow"),
        pytest.param({"tool_name": "Agent", "session_id": "s",
                      "tool_input": {"prompt": "x", "model": "opus"}}, id="unnamed-agent"),
    ])
    def test_calls_without_the_task_wall_check_make_no_request(self, tmp_path, payload):
        home = tmp_path / "home"
        home.mkdir()
        with _FakeApi() as api:
            rc, _out, err, _t = _run_hook("PreToolUse", payload, tmp_path, _env(home, api.url))
            assert rc == 0, err
            assert api.log == []

    def test_another_cwd_does_not_reuse_the_cache(self, tmp_path):
        home, proj_a, proj_b = tmp_path / "home", tmp_path / "proj-a", tmp_path / "proj-b"
        for d in (home, proj_a, proj_b):
            d.mkdir()
        with _FakeApi(projects={os.path.realpath(proj_b): "proj-B"}) as api:
            env = _env(home, api.url)
            for _ in range(3):
                _run_hook("PreToolUse", _named_agent(), proj_a, env)
            _run_hook("PreToolUse", _named_agent(), proj_b, env)
            assert len(api.resolves(proj_a)) == 1
            assert len(api.resolves(proj_b)) == 1, "a second project must resolve on its own"
        cache = json.load(open(_state_path(home)))["project_id_by_cwd"]
        assert cache[os.path.realpath(proj_a)]["id"] == "proj-A"
        assert cache[os.path.realpath(proj_b)]["id"] == "proj-B"

    def test_no_project_answer_is_cached_too(self, tmp_path):
        home, loose = tmp_path / "home", tmp_path / "not-a-project"
        home.mkdir()
        loose.mkdir()
        with _FakeApi(projects={os.path.realpath(loose): ""}) as api:
            env = _env(home, api.url)
            for _ in range(10):
                _run_hook("PreToolUse", _named_agent(), loose, env)
            assert len(api.resolves(loose)) == 1

    def test_expired_entry_is_refreshed(self, tmp_path):
        home, proj = tmp_path / "home", tmp_path / "proj-a"
        home.mkdir()
        proj.mkdir()
        os.makedirs(os.path.dirname(_state_path(home)))
        with open(_state_path(home), "w") as f:
            json.dump({"project_id_by_cwd": {os.path.realpath(proj): {"id": "old", "at": 0}}}, f)
        with _FakeApi() as api:
            _run_hook("PreToolUse", _named_agent(), proj, _env(home, api.url))
            assert len(api.resolves(proj)) == 1
        entry = json.load(open(_state_path(home)))["project_id_by_cwd"][os.path.realpath(proj)]
        assert entry["id"] == "proj-A" and entry["at"] > 0

    def test_failed_resolve_caches_nothing_and_keeps_the_stale_id(self):
        key = os.path.realpath(os.getcwd())
        state = {"project_id_by_cwd": {key: {"id": "stale", "at": 0}}}
        with mock.patch.object(wr.urllib.request, "urlopen", side_effect=OSError("down")):
            assert wr._resolve_project_id(state, os.getcwd()) == "stale"
        assert state["project_id_by_cwd"][key]["at"] == 0

    def test_bridge_reads_the_entry_workflow_reminder_wrote_via_a_symlink(self, tmp_path):
        """One cache, one key: realpath(cwd). CC hands the bridge a logical cwd."""
        real = tmp_path / "proj-real"
        real.mkdir()
        link = tmp_path / "proj-link"
        link.symlink_to(real)
        state = {"project_id_by_cwd": {os.path.realpath(real): {"id": "proj-R", "at": time.time()}}}
        with mock.patch.object(bridge, "_load_state", return_value=state), mock.patch.object(
            bridge.urllib.request, "urlopen", side_effect=AssertionError("must not call the API")
        ):
            assert bridge._resolve_project_id(str(link)) == "proj-R"

    def test_bridge_asks_again_when_the_cache_says_no_project(self):
        """A cached "" may predate the directory's registration; TaskCompleted fires once."""
        key = os.path.realpath("/no/such/place")
        state: dict = {"project_id_by_cwd": {key: {"id": "", "at": time.time()}}}
        resp = mock.MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = mock.MagicMock(return_value=False)
        resp.read.return_value = json.dumps({"project_id": ""}).encode()
        with mock.patch.object(bridge, "_load_state", side_effect=lambda: state), mock.patch.object(
            bridge, "_save_state"
        ) as save, mock.patch.object(bridge.urllib.request, "urlopen", return_value=resp) as urlopen:
            assert bridge._resolve_project_id("/no/such/place") is None
            assert bridge._resolve_project_id("/no/such/place") is None
        assert urlopen.call_count == 2
        save.assert_not_called()  # the bridge only ever writes hits back

    def test_task_completed_after_registration_is_mirrored_despite_a_cached_no_project(self, tmp_path):
        """End to end: workflow_reminder caches "" for a fresh directory, the project is
        registered a moment later, then a teammate finishes a task there."""
        home, proj = tmp_path / "home", tmp_path / "proj-new"
        home.mkdir()
        proj.mkdir()
        with _FakeApi(projects={os.path.realpath(proj): ""}) as api:
            env = _env(home, api.url)
            _run_hook("PreToolUse", _named_agent(), proj, env)
            cached = json.load(open(_state_path(home)))["project_id_by_cwd"][os.path.realpath(proj)]
            assert cached["id"] == ""  # the precondition: a fresh negative entry
            api.projects[os.path.realpath(proj)] = "proj-NEW"  # project_create
            rc = _run_bridge({"task_id": "7", "task_subject": "ship it", "teammate_name": "worker-1",
                              "team_name": ""}, proj, env)
            posts = [r["path"] for r in api.log if r["method"] == "POST"]
        assert rc == 0
        assert "/api/projects/proj-NEW/tasks" in posts, f"completion not mirrored: {posts}"

    def test_an_answer_from_this_call_is_not_asked_for_twice(self):
        state: dict = {}
        http: dict = {}
        resp = mock.MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = mock.MagicMock(return_value=False)
        resp.read.return_value = json.dumps({"project_id": ""}).encode()
        with mock.patch.object(wr.urllib.request, "urlopen", return_value=resp) as urlopen:
            assert wr._resolve_project_id(state, os.getcwd(), http=http) is None
            state.clear()  # even with the cache gone, this call already has its answer
            assert wr._resolve_project_id(state, os.getcwd(), http=http) is None
        assert urlopen.call_count == 1
        with mock.patch.object(wr.urllib.request, "urlopen", side_effect=OSError("down")) as urlopen:
            http = {}
            assert wr._resolve_project_id({}, os.getcwd(), http=http) is None
            assert wr._resolve_project_id({}, os.getcwd(), http=http) is None
        assert urlopen.call_count == 1, "a request that just failed must not be retried in the same call"


# ---------------------------------------------------------------------------
# B. local guards before any HTTP
# ---------------------------------------------------------------------------


_STALLED_BLOCKS = [
    pytest.param(
        {"tool_name": "Agent", "tool_input": {"description": "d", "prompt": "do x",
                                              "subagent_type": "general-purpose", "name": "w1"}},
        "未指定 model",
        id="agent-named-no-model",
    ),
    pytest.param(
        {"tool_name": "Agent", "tool_input": {"description": "d", "prompt": "do x",
                                              "subagent_type": "general-purpose", "name": "w1",
                                              "team_name": "team-a"}},
        "未指定 model",
        id="agent-team-no-model",
    ),
    pytest.param(
        {"tool_name": "Workflow", "tool_input": {"script": "await agent('x', {label: 'a'})"}},
        "未写 model",
        id="workflow-no-model",
    ),
    pytest.param(
        {"tool_name": "Bash", "tool_input": {"command": "git add .env"}},
        "敏感文件",
        id="bash-git-add-env",
    ),
]


class TestGuardsBeforeHttp:
    @pytest.mark.parametrize("payload, needle", _STALLED_BLOCKS)
    def test_block_lands_fast_while_the_api_stalls(self, tmp_path, payload, needle):
        home = tmp_path / "home"
        home.mkdir()
        with _FakeApi(delay=3.0) as api:
            try:
                rc, _out, err, took = _run_hook(
                    "PreToolUse", dict(payload, session_id="s6"), tmp_path, _env(home, api.url),
                    timeout=5.0,  # Claude Code's own limit for this hook
                )
            except subprocess.TimeoutExpired:
                pytest.fail("hook still running at 5s: Claude Code would kill it and run the tool")
        assert rc == 2 and needle in err
        assert took < 0.5, f"block took {took:.2f}s with the API stalled"

    def test_team_in_another_project_is_not_blocked(self, tmp_path):
        """team_name is ignored by Claude Code, so a cross-project check on it guarded nothing."""
        home = tmp_path / "home"
        home.mkdir()
        with _FakeApi(projects={os.path.realpath(tmp_path): "proj-OTHER"}) as api:
            payload = {"tool_name": "Agent", "session_id": "xp",
                       "tool_input": {"prompt": "work", "model": "opus", "team_name": "team-a"}}
            rc, _out, err, _t = _run_hook("PreToolUse", payload, tmp_path, _env(home, api.url))
        assert rc == 0, err
        assert "跨项目" not in err

    def test_fresh_no_project_answer_is_asked_once_per_call(self, tmp_path):
        """An unregistered directory: one resolve per call, no advisory that needs the project."""
        home, loose = tmp_path / "home", tmp_path / "loose"
        home.mkdir()
        loose.mkdir()
        with _FakeApi(projects={os.path.realpath(loose): ""}) as api:
            payload = {"tool_name": "Agent", "session_id": "xp",
                       "tool_input": {"prompt": "work", "model": "opus", "team_name": "team-a"}}
            rc, _out, err, _t = _run_hook("PreToolUse", payload, loose, _env(home, api.url))
            n = len(api.resolves(loose))
        assert rc == 0, err
        assert n == 1


# ---------------------------------------------------------------------------
# Agent chain: each resource at most once per invocation
# ---------------------------------------------------------------------------


class TestAgentChainFetchesOnce:
    _PAYLOAD = {
        "tool_name": "Agent",
        "session_id": "chain",
        "tool_input": {"description": "d", "prompt": "hook latency diag", "model": "opus",
                       "subagent_type": "general-purpose", "name": "w1", "team_name": "team-a"},
    }

    def test_each_resource_fetched_at_most_once(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        # No running team task: the check falls back to running-count, which
        # says 1, so the task-wall match runs as well.
        with _FakeApi(team_tasks=[]) as api:
            rc, _out, _err, _t = _run_hook("PreToolUse", self._PAYLOAD, tmp_path, _env(home, api.url))
            gets = [(r["path"], r["project"]) for r in api.log if r["method"] == "GET"]
        assert rc == 0
        counts = {key: gets.count(key) for key in set(gets)}
        assert counts == {
            ("/api/teams", "proj-A"): 1,
            ("/api/teams/t1/tasks", "proj-A"): 1,
            ("/api/projects/proj-A/tasks/running-count", "proj-A"): 1,
            ("/api/projects/proj-A/task-wall", "proj-A"): 1,
        }

    def test_post_tool_use_asks_nothing_and_says_nothing(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        with _FakeApi(team_tasks=[]) as api:
            rc, out, err, _t = _run_hook("PostToolUse", self._PAYLOAD, tmp_path, _env(home, api.url))
            assert api.log == []
        assert (rc, out, err) == (0, "", "")
        assert not os.path.exists(_state_path(home))

    def test_memo_also_holds_a_failure(self):
        http: dict = {}
        with mock.patch.object(wr.urllib.request, "urlopen", side_effect=OSError("stall")) as urlopen:
            for _ in range(3):
                with pytest.raises(OSError):
                    wr._get_data_list("http://x", "/api/teams", "p", http)
        assert urlopen.call_count == 1


# ---------------------------------------------------------------------------
# C. S4 process deadline
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _repo_with_clean_worktrees(tmp_path, n: int):
    repo = tmp_path / "main"
    repo.mkdir()
    _git(["init", "-b", "master"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "initial"], repo)
    wts = []
    for i in range(n):
        wt = repo / ".claude" / "worktrees" / f"wt{i}"
        _git(["worktree", "add", str(wt), "-b", f"worktree-wt{i}"], repo)
        wts.append(wt)
    return repo, wts


def _slow_git_shim(tmp_path, seconds: float) -> str:
    real = shutil.which("git")
    shim = tmp_path / "shim"
    shim.mkdir()
    script = shim / "git"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        sleep {seconds}
        exec "{real}" "$@"
    """))
    script.chmod(0o755)
    return str(shim)


_SINGLE_TARGET_HINT = "只拆一个目标仍然超时"


class TestS4Deadline:
    @pytest.mark.parametrize("n", [1, 3], ids=["one-worktree", "three-worktrees"])
    def test_slow_git_blocks_inside_the_hook_limit_with_a_timeout_message(self, tmp_path, n):
        repo, wts = _repo_with_clean_worktrees(tmp_path, n)
        shim = _slow_git_shim(tmp_path, 1)
        home = tmp_path / "home"
        home.mkdir()
        cmd = "; ".join(f'git worktree remove "{wt}"' for wt in wts)
        try:
            rc, _out, err, took = _run_hook(
                "PreToolUse", _bash(cmd), repo, _env(home, "http://127.0.0.1:9", shim), timeout=6.0
            )
        except subprocess.TimeoutExpired:
            pytest.fail("S4 still probing at 6s: Claude Code kills it at 5s and the teardown runs unassessed")
        assert rc == 2
        assert "超时" in err and "已按拦截处理" in err and "不要重放" in err
        if n == 1:
            # Splitting cannot help a single slow target: the way out is a manual check.
            assert _SINGLE_TARGET_HINT in err and "请分批" not in err, err
        else:
            assert "请分批" in err and _SINGLE_TARGET_HINT not in err, err
        assert took <= 4.2, f"hook took {took:.2f}s"

    def test_fast_git_still_gets_a_full_assessment(self, tmp_path):
        """The deadline must not turn ordinary teardowns into timeouts."""
        repo, wts = _repo_with_clean_worktrees(tmp_path, 3)
        home = tmp_path / "home"
        home.mkdir()
        cmd = "; ".join(f'git worktree remove "{wt}"' for wt in wts)
        rc, out, err, _t = _run_hook("PreToolUse", _bash(cmd), repo, _env(home, "http://127.0.0.1:9"))
        assert rc == 0, err
        assert "超时" not in out + err

    def test_expired_deadline_blocks_teardown_in_process(self, tmp_path, monkeypatch, capsys):
        repo, wts = _repo_with_clean_worktrees(tmp_path, 1)
        monkeypatch.setattr(wr, "_hook_deadline", time.monotonic() - 1)
        event = {"tool_name": "Bash", "cwd": str(repo), "hook_event_name": "PreToolUse",
                 "tool_input": {"command": f'git worktree remove "{wts[0]}"'}}
        with pytest.raises(SystemExit) as exc:
            wr._check_local_guards(event, {})
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "超时" in err and _SINGLE_TARGET_HINT in err and "请分批" not in err

    @pytest.mark.parametrize(
        "cmd, single",
        [
            ("git branch -D worktree-wt0", True),
            ("git branch -D worktree-wt0 worktree-wt1", False),
            ('git worktree remove "{wt}"; git branch -D worktree-wt1', False),
        ],
        ids=["one-ref", "two-refs", "worktree-plus-ref"],
    )
    def test_deadline_message_counts_every_teardown_target(
        self, tmp_path, monkeypatch, capsys, cmd, single
    ):
        repo, wts = _repo_with_clean_worktrees(tmp_path, 2)
        monkeypatch.setattr(wr, "_hook_deadline", time.monotonic() - 1)
        event = {"tool_name": "Bash", "cwd": str(repo), "hook_event_name": "PreToolUse",
                 "tool_input": {"command": cmd.format(wt=wts[0])}}
        with pytest.raises(SystemExit) as exc:
            wr._check_local_guards(event, {})
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert (_SINGLE_TARGET_HINT in err) is single, err
        assert ("请分批" in err) is not single, err

    def test_git_probe_is_capped_by_the_deadline(self, tmp_path, monkeypatch):
        shim = _slow_git_shim(tmp_path, 3)
        monkeypatch.setenv("PATH", shim + os.pathsep + os.environ.get("PATH", ""))
        monkeypatch.setattr(wr, "_hook_deadline", time.monotonic() + 0.3)
        t0 = time.perf_counter()
        code, _ = wr._run_git_readonly(["status"], cwd=str(tmp_path))
        took = time.perf_counter() - t0
        assert code == wr._GIT_UNAVAILABLE
        assert took < 1.0, f"probe ran {took:.2f}s past a 0.3s deadline (grandchild kept the pipe?)"

    def test_s5_under_an_expired_deadline_still_only_warns(self, tmp_path, monkeypatch):
        """S5's contract is fail-loud, never block: the deadline must not change that."""
        monkeypatch.setattr(wr, "_hook_deadline", time.monotonic() - 1)
        event = {"tool_name": "Bash", "cwd": str(tmp_path), "hook_event_name": "PreToolUse",
                 "session_id": "a1", "tool_input": {"command": "git commit -m x"}}
        warnings = wr._check_local_guards(event, {})
        assert any("未能执行" in w for w in warnings)


class TestDeadlineLifecycle:
    """main() called in-process: the deadline must be this call's, and must not outlive it."""

    @staticmethod
    def _call_main(monkeypatch, tmp_path, payload: dict):
        monkeypatch.setattr(wr, "_hook_deadline", None)  # restored at teardown whatever main() leaves
        monkeypatch.setattr(wr, "_SUPERVISOR_STATE_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(wr, "_SUPERVISOR_STATE_FILE", str(tmp_path / "data" / "supervisor-state.json"))
        monkeypatch.setenv("AITEAM_API_URL", "http://127.0.0.1:9")  # refused at once, never the real API
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
        out, err = io.TextIOWrapper(io.BytesIO()), io.TextIOWrapper(io.BytesIO())
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        code = 0
        try:
            wr.main()
        except SystemExit as exc:
            code = exc.code
        err.seek(0)
        return code, err.read()

    def test_in_process_main_counts_its_deadline_from_its_own_start(self, tmp_path, monkeypatch):
        repo, wts = _repo_with_clean_worktrees(tmp_path, 1)
        monkeypatch.setattr(wr, "_HOOK_T0", time.monotonic() - 60)  # module imported a minute ago
        payload = {"tool_name": "Bash", "session_id": "inproc", "cwd": str(repo),
                   "hook_event_name": "PreToolUse",
                   "tool_input": {"command": f'git worktree remove "{wts[0]}"'}}
        code, err = self._call_main(monkeypatch, tmp_path, payload)
        assert code == 0, f"a clean teardown was blocked: {err}"

    @pytest.mark.parametrize(
        "command", ["ls", "git add .env"], ids=["returns", "exits-2"]
    )
    def test_main_leaves_no_deadline_behind(self, tmp_path, monkeypatch, command):
        payload = {"tool_name": "Bash", "session_id": "inproc", "cwd": str(tmp_path),
                   "hook_event_name": "PreToolUse", "tool_input": {"command": command}}
        self._call_main(monkeypatch, tmp_path, payload)
        assert wr._hook_deadline is None
        code, out = wr._run_git_readonly(["--version"], cwd=str(tmp_path))
        assert code == 0 and out.startswith("git version")


# ---------------------------------------------------------------------------
# D. shared state under concurrency
# ---------------------------------------------------------------------------


_WRITER = textwrap.dedent("""\
    import json, sys, time
    sys.path.insert(0, {src!r})
    import aiteam.hooks.{mod} as m
    big = {{"k%d" % i: "v" * 200 for i in range(1500)}}
    stop = time.time() + {secs}
    while time.time() < stop:
        {call}
""")

_READER = textwrap.dedent("""\
    import json, sys, time
    path, secs = sys.argv[1], float(sys.argv[2])
    bad = reads = 0
    stop = time.time() + secs
    while time.time() < stop:
        try:
            raw = open(path, encoding="utf-8").read()
        except FileNotFoundError:
            continue
        reads += 1
        try:
            json.loads(raw)
        except ValueError:
            bad += 1
    print(json.dumps({"reads": reads, "bad": bad}))
""")


def _src_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(wr.__file__)))


class TestSharedStateConcurrency:
    def test_concurrent_saves_never_expose_a_torn_file(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        path = _state_path(home)
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            json.dump({}, f)
        env = _env(home, "http://127.0.0.1:9")
        secs = 1.5
        bridge_entry = '{"/p%f" % time.time(): {"id": "x", "at": time.time()}}'
        calls = {
            "workflow_reminder": 'm._save_supervisor_state({"big": big, "n": time.time()})',
            "cc_task_bridge": f'm._save_state({{"project_id_by_cwd": {bridge_entry}, "big": big}})',
        }
        writers = [
            subprocess.Popen(
                [sys.executable, "-c", _WRITER.format(src=_src_dir(), mod=mod, secs=secs, call=call)],
                env=env,
            )
            for mod, call in calls.items()
            for _ in range(3)
        ]
        reader = subprocess.run(
            [sys.executable, "-c", _READER, path, str(secs)], capture_output=True, text=True, env=env
        )
        for w in writers:
            w.wait(timeout=30)
        result = json.loads(reader.stdout)
        assert result["reads"] > 50
        assert result["bad"] == 0, f"{result['bad']} of {result['reads']} reads saw a half-written file"
        json.load(open(path))

    def test_concurrent_hooks_keep_every_bucket_and_claim(self, tmp_path):
        home, proj = tmp_path / "home", tmp_path / "proj"
        home.mkdir()
        proj.mkdir()
        path = _state_path(home)
        os.makedirs(os.path.dirname(path))
        now = time.time()
        seed = {
            "session_scoped": {f"s{i}": {"_ts": now, "workflow_reminder_shown": True} for i in range(300)},
            "branch_ownership": {f"/repo/wt{i}": {"agent-x": {"branch": f"b{i}", "ts": now}}
                                 for i in range(300)},
        }
        with open(path, "w") as f:
            json.dump(seed, f)
        workers, per_worker = 12, 10
        workflow = {"tool_name": "Workflow", "tool_input": {"script": "await agent('x', { model: 'opus' })"}}
        shown: dict[int, int] = {}
        with _FakeApi() as api:
            env = _env(home, api.url)

            def work(i: int) -> None:
                for _ in range(per_worker):
                    rc, out, _err, _t = _run_hook("PreToolUse", dict(workflow, session_id=f"w{i}"), proj, env)
                    assert rc == 0
                    if out and "Workflow 运行已自动追踪" in json.loads(out)["hookSpecificOutput"]["additionalContext"]:
                        shown[i] = shown.get(i, 0) + 1

            threads = [threading.Thread(target=work, args=(i,)) for i in range(workers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        final = json.load(open(path))
        assert len(final["branch_ownership"]) == 300, "S5 claims were wiped"
        assert len([k for k in final["session_scoped"] if k.startswith("s")]) == 300
        # Every session saw its reminder, and (no lock file, so exactness is not
        # promised) nearly always exactly once: a lost save only means one more.
        assert set(shown) == set(range(workers)), shown
        assert sum(shown.values()) <= workers + 2, shown
        assert len([k for k in final["session_scoped"] if k.startswith("w")]) == workers
        leftovers = sorted(set(os.listdir(os.path.dirname(path))) - {"supervisor-state.json"})
        assert leftovers == [], f"lock or temp files left in the data directory: {leftovers}"


class _JsonSpy:
    """Stands in for a hook module's `json`; runs `on_dump` before each json.dump."""

    def __init__(self, on_dump):
        self._on_dump = on_dump
        self.dumps_seen = 0

    def __getattr__(self, name):
        return getattr(json, name)

    def dump(self, obj, fp, **kw):
        self.dumps_seen += 1
        self._on_dump(self.dumps_seen)
        return json.dump(obj, fp, **kw)


def _land_other_save(path: str, content: dict) -> None:
    """Another process's save, the way every current writer lands one: a new file renamed in."""
    tmp = f"{path}.other.{time.monotonic_ns()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(content, f)
    os.replace(tmp, path)


class TestSaveRace:
    """A save that lands between this save's re-read and its replace (no lock file to stop it)."""

    def _wr_setup(self, tmp_path, monkeypatch):
        path = str(tmp_path / "supervisor-state.json")
        monkeypatch.setattr(wr, "_SUPERVISOR_STATE_FILE", path)
        monkeypatch.setattr(wr, "_SUPERVISOR_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(wr, "_SAVE_BACKOFF_S", 0.0, raising=False)
        with open(path, "w") as f:
            json.dump({"session_scoped": {"a": {"_ts": 1.0}}}, f)
        state = wr._load_supervisor_state()
        base = json.loads(json.dumps(state))
        state["session_scoped"]["mine"] = {"_ts": 2.0, "workflow_reminder_shown": True}
        return path, state, base

    def test_workflow_reminder_merges_again_instead_of_overwriting(self, tmp_path, monkeypatch):
        path, state, base = self._wr_setup(tmp_path, monkeypatch)
        other = {"session_scoped": {"a": {"_ts": 1.0}, "b": {"_ts": 3.0}},
                 "branch_ownership": {"/r": {"a": {"branch": "b", "ts": 1}}}}

        def on_dump(n):
            if n == 1:
                _land_other_save(path, other)

        monkeypatch.setattr(wr, "json", _JsonSpy(on_dump))
        wr._save_supervisor_state(state, base)
        final = json.load(open(path))
        assert set(final["session_scoped"]) == {"a", "b", "mine"}, "the other save's bucket was overwritten"
        assert "branch_ownership" in final

    def test_workflow_reminder_gives_up_rather_than_clobber_a_busy_file(self, tmp_path, monkeypatch):
        path, state, base = self._wr_setup(tmp_path, monkeypatch)

        def on_dump(n):
            _land_other_save(path, {"session_scoped": {"a": {"_ts": 100.0 + n}}})

        spy = _JsonSpy(on_dump)
        monkeypatch.setattr(wr, "json", spy)
        wr._save_supervisor_state(state, base)
        final = json.load(open(path))
        assert final == {"session_scoped": {"a": {"_ts": 100.0 + spy.dumps_seen}}}, "a winner's save was overwritten"
        assert spy.dumps_seen == wr._SAVE_ATTEMPTS

    def test_bridge_merges_again_instead_of_overwriting(self, tmp_path, monkeypatch):
        path = str(tmp_path / "supervisor-state.json")
        monkeypatch.setattr(bridge, "_PROJECT_CACHE_FILE", path)
        with open(path, "w") as f:
            json.dump({"bottleneck_check_count": 5}, f)

        def on_dump(n):
            if n == 1:
                _land_other_save(path, {"bottleneck_check_count": 9,
                                        "branch_ownership": {"/r": {"a": {"branch": "b", "ts": 1}}}})

        monkeypatch.setattr(bridge, "json", _JsonSpy(on_dump))
        bridge._save_state({"project_id_by_cwd": {"/p": {"id": "proj-1", "at": time.time()}}})
        final = json.load(open(path))
        assert final["bottleneck_check_count"] == 9 and "branch_ownership" in final
        assert final["project_id_by_cwd"]["/p"]["id"] == "proj-1"

    def test_saves_leave_no_lock_or_temp_file(self, tmp_path, monkeypatch):
        """Runtime lock files are ruled out for this state (see the S5 docstring)."""
        path, state, base = self._wr_setup(tmp_path, monkeypatch)
        monkeypatch.setattr(bridge, "_PROJECT_CACHE_FILE", path)
        wr._save_supervisor_state(state, base)
        bridge._save_state({"project_id_by_cwd": {"/p": {"id": "proj-1", "at": time.time()}}})
        assert sorted(os.listdir(tmp_path)) == ["supervisor-state.json"]


class TestMergeState:
    def test_a_value_this_call_changed_wins_as_written(self):
        """No counters live in this state any more, so an int is a plain value."""
        assert wr._merge_state({"c": 5}, {"c": 6}, {"c": 9}) == {"c": 6}

    def test_untouched_keys_keep_theirs(self):
        merged = wr._merge_state({"a": 1, "t": 1.0}, {"a": 1, "t": 1.0}, {"a": 3, "t": 2.0, "x": 1})
        assert merged == {"a": 3, "t": 2.0, "x": 1}

    def test_nested_claims_from_both_sides_survive(self):
        base = {"branch_ownership": {"/r": {"a1": {"branch": "x", "ts": 1.0}}}}
        mine = {"branch_ownership": {"/r": {"a1": {"branch": "x", "ts": 1.0}, "a2": {"branch": "y", "ts": 2.0}}}}
        theirs = {"branch_ownership": {"/r": {"a1": {"branch": "x", "ts": 1.0}, "a3": {"branch": "z", "ts": 3.0}}}}
        merged = wr._merge_state(base, mine, theirs)
        assert set(merged["branch_ownership"]["/r"]) == {"a1", "a2", "a3"}

    def test_delete_applies_only_if_nobody_else_changed_the_value(self):
        assert wr._merge_state({"k": 1}, {}, {"k": 1}) == {}
        assert wr._merge_state({"k": 1}, {}, {"k": 2}) == {"k": 2}

    def test_save_merges_onto_what_is_on_disk_now(self, tmp_path, monkeypatch):
        path = str(tmp_path / "supervisor-state.json")
        monkeypatch.setattr(wr, "_SUPERVISOR_STATE_FILE", path)
        monkeypatch.setattr(wr, "_SUPERVISOR_STATE_DIR", str(tmp_path))
        with open(path, "w") as f:
            json.dump({"branch_ownership": {"/r": {"a1": {"branch": "x", "ts": 1}}}}, f)
        state = wr._load_supervisor_state()
        base = json.loads(json.dumps(state))
        state["session_scoped"] = {"mine": {"_ts": 5.0}}
        # another session writes while this call is still busy
        with open(path, "w") as f:
            json.dump({"session_scoped": {"theirs": {"_ts": 7.0}},
                       "branch_ownership": {"/r": {"a1": {"branch": "x", "ts": 1}, "a2": {"branch": "y", "ts": 2}}}}, f)
        wr._save_supervisor_state(state, base)
        final = json.load(open(path))
        assert set(final["session_scoped"]) == {"mine", "theirs"}
        assert set(final["branch_ownership"]["/r"]) == {"a1", "a2"}

    def test_unreadable_file_is_replaced_by_the_whole_state(self, tmp_path, monkeypatch):
        path = str(tmp_path / "supervisor-state.json")
        monkeypatch.setattr(wr, "_SUPERVISOR_STATE_FILE", path)
        monkeypatch.setattr(wr, "_SUPERVISOR_STATE_DIR", str(tmp_path))
        with open(path, "w") as f:
            f.write('{"half": ')
        wr._save_supervisor_state({"a": 1}, {})
        assert json.load(open(path)) == {"a": 1}


class TestConcurrentFirstClaims:
    """S5 under a race: two agents' first commits on one branch overlap.

    Each call sees no claim and records its own. Merging both would leave each
    agent finding its own claim first, so S5 would never block either again.
    Exactly one claim must survive, like a serial run would leave it."""

    _CK = "/repo/main"

    def _setup(self, tmp_path, monkeypatch, on_disk: dict):
        path = str(tmp_path / "supervisor-state.json")
        monkeypatch.setattr(wr, "_SUPERVISOR_STATE_FILE", path)
        monkeypatch.setattr(wr, "_SUPERVISOR_STATE_DIR", str(tmp_path))
        with open(path, "w") as f:
            json.dump({}, f)
        state = wr._load_supervisor_state()
        base = json.loads(json.dumps(state))
        with open(path, "w") as f:  # the other agent's save lands first
            json.dump(on_disk, f)
        return path, state, base

    def test_second_concurrent_claim_on_the_same_branch_is_dropped(self, tmp_path, monkeypatch, capsys):
        other = {"branch_ownership": {self._CK: {"a1": {"branch": "feat", "ts": time.time()}}}}
        path, state, base = self._setup(tmp_path, monkeypatch, other)
        state["branch_ownership"] = {self._CK: {"a2": {"branch": "feat", "ts": time.time()}}}
        wr._save_supervisor_state(state, base)
        claims = json.load(open(path))["branch_ownership"][self._CK]
        assert set(claims) == {"a1"}

        # ...and a2's next commit on that branch meets a1's claim and is blocked.
        event = {"tool_name": "Bash", "session_id": "a2", "cwd": self._CK,
                 "hook_event_name": "PreToolUse", "tool_input": {"command": "git commit -m x"}}
        with mock.patch.object(wr, "_run_git_readonly", return_value=(0, f"{self._CK}\nfeat")):
            with pytest.raises(SystemExit) as exc:
                wr._check_local_guards(event, wr._load_supervisor_state())
        assert exc.value.code == 2
        assert "分支所有权冲突" in capsys.readouterr().err

    def test_claims_on_different_branches_both_survive(self, tmp_path, monkeypatch):
        other = {"branch_ownership": {self._CK: {"a1": {"branch": "feat-1", "ts": time.time()}}}}
        path, state, base = self._setup(tmp_path, monkeypatch, other)
        state["branch_ownership"] = {self._CK: {"a2": {"branch": "feat-2", "ts": time.time()}}}
        wr._save_supervisor_state(state, base)
        assert set(json.load(open(path))["branch_ownership"][self._CK]) == {"a1", "a2"}

    def test_an_expired_claim_does_not_evict_a_legal_successor(self, tmp_path, monkeypatch):
        stale = time.time() - wr._BRANCH_OWNERSHIP_ACTIVE_TTL - 60
        other = {"branch_ownership": {self._CK: {"a1": {"branch": "feat", "ts": stale}}}}
        path, state, base = self._setup(tmp_path, monkeypatch, other)
        state["branch_ownership"] = {self._CK: {"a2": {"branch": "feat", "ts": time.time()}}}
        wr._save_supervisor_state(state, base)
        assert "a2" in json.load(open(path))["branch_ownership"][self._CK]

    def test_refreshing_an_existing_own_claim_is_not_a_new_claim(self, tmp_path, monkeypatch):
        path = str(tmp_path / "supervisor-state.json")
        monkeypatch.setattr(wr, "_SUPERVISOR_STATE_FILE", path)
        monkeypatch.setattr(wr, "_SUPERVISOR_STATE_DIR", str(tmp_path))
        with open(path, "w") as f:
            json.dump({"branch_ownership": {self._CK: {"a2": {"branch": "feat", "ts": 1.0}}}}, f)
        state = wr._load_supervisor_state()
        base = json.loads(json.dumps(state))
        state["branch_ownership"][self._CK]["a2"]["ts"] = time.time()
        wr._save_supervisor_state(state, base)
        assert json.load(open(path))["branch_ownership"][self._CK]["a2"]["ts"] > 1.0


class TestBridgeSave:
    def test_bridge_writes_only_its_cache_entry(self, tmp_path, monkeypatch):
        """The bridge holds its copy across an HTTP call of up to 3s; it must not
        write that stale copy over whatever other sessions recorded meanwhile."""
        path = str(tmp_path / "supervisor-state.json")
        monkeypatch.setattr(bridge, "_PROJECT_CACHE_FILE", path)
        with open(path, "w") as f:
            json.dump({"bottleneck_check_count": 5}, f)
        state = bridge._load_state()
        with open(path, "w") as f:
            json.dump({"bottleneck_check_count": 9, "branch_ownership": {"/r": {"a": {"branch": "b", "ts": 1}}}}, f)
        state["project_id_by_cwd"] = {"/p": {"id": "proj-1", "at": time.time()}}
        bridge._save_state(state)
        final = json.load(open(path))
        assert final["bottleneck_check_count"] == 9
        assert "branch_ownership" in final
        assert final["project_id_by_cwd"]["/p"]["id"] == "proj-1"

    def test_bridge_keeps_a_newer_entry_on_disk(self, tmp_path, monkeypatch):
        path = str(tmp_path / "supervisor-state.json")
        monkeypatch.setattr(bridge, "_PROJECT_CACHE_FILE", path)
        with open(path, "w") as f:
            json.dump({"project_id_by_cwd": {"/p": {"id": "new", "at": 200.0}}}, f)
        bridge._save_state({"project_id_by_cwd": {"/p": {"id": "old", "at": 100.0}}})
        assert json.load(open(path))["project_id_by_cwd"]["/p"]["id"] == "new"
