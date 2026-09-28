"""AI Team OS — pytest 全局 fixtures."""

from __future__ import annotations

import asyncio
import atexit
import importlib.util
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path


# Session-wide isolation from the real OS data directory. This must run before
# the first aiteam import: many modules resolve ~/.claude/data/ai-team-os into
# module-level constants (DB URL, port file, log dirs), and create_app()
# attaches a file handler to ~/.claude/data/ai-team-os/debug.log. Child
# processes inherit the redirected HOME as well.
def _account_home() -> str:
    """Where a process without HOME (or USERPROFILE on Windows) resolves ~ to."""
    try:
        import pwd
    except ImportError:
        return os.path.expanduser("~")
    return pwd.getpwuid(os.getuid()).pw_dir


_REAL_DATA_DIRS = tuple(sorted({
    os.path.join(os.path.realpath(home), ".claude", "data", "ai-team-os")
    for home in (os.path.expanduser("~"), _account_home())
}))
_TEST_HOME = tempfile.mkdtemp(prefix="aiteam-test-home-")
os.environ["HOME"] = _TEST_HOME
if os.name == "nt":
    os.environ["USERPROFILE"] = _TEST_HOME
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

_real_data_violations: list[str] = []
_PATH_EVENTS = frozenset({
    "os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.truncate", "os.chmod",
    "os.chown", "os.link", "os.symlink", "os.utime", "shutil.rmtree", "shutil.copyfile",
    "shutil.move", "sqlite3.connect",
})
_SPAWN_EVENTS = frozenset({"subprocess.Popen", "os.posix_spawn", "os.exec"})


def _in_real_data_dir(path) -> bool:
    if isinstance(path, int) or path is None:
        return False
    try:
        resolved = os.path.realpath(os.fsdecode(path))
    except (TypeError, ValueError):
        return False
    return any(resolved == root or resolved.startswith(root + os.sep) for root in _REAL_DATA_DIRS)


def _refuse(message: str) -> None:
    _real_data_violations.append(message)
    raise PermissionError(f"Test isolation: {message}")


def _guard_real_data_dir(event: str, args: tuple) -> None:
    """Refuse any test write into the real data dir, and any child that would inherit it."""
    if event == "open":
        path, mode, flags = args
        if mode is not None:
            writes = any(flag in str(mode) for flag in "wax+")
        else:
            writes = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
        if writes and _in_real_data_dir(path):
            _refuse(f"open({os.fsdecode(path)!r}, {mode or flags!r})")
    elif event in _PATH_EVENTS:
        # copyfile only reads its source; every other event mutates each path it names.
        for path in args[1:2] if event == "shutil.copyfile" else args[:2]:
            if isinstance(path, (str, bytes, os.PathLike)) and _in_real_data_dir(path):
                _refuse(f"{event}({os.fsdecode(path)!r})")
    elif event in _SPAWN_EVENTS:
        env = args[-1]
        if env is None:
            return
        home = env.get("HOME", env.get(b"HOME")) if hasattr(env, "get") else None
        # A child without HOME falls back to the passwd entry, i.e. the real home.
        effective = os.fsdecode(home) if home else _account_home()
        if _in_real_data_dir(os.path.join(effective, ".claude", "data", "ai-team-os")):
            _refuse(f"{event} with HOME={effective!r}: {str(args[1])[:200]}")


sys.addaudithook(_guard_real_data_dir)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from aiteam.storage.connection import close_db  # noqa: E402
from aiteam.storage.repository import StorageRepository  # noqa: E402


@pytest.fixture(autouse=True)
def _fail_on_real_data_access():
    """Turn a refused real-data access into a test failure even if the caller swallowed it."""
    seen = len(_real_data_violations)
    yield
    if len(_real_data_violations) > seen:
        pytest.fail("Touched the real OS data dir:\n" + "\n".join(_real_data_violations[seen:]),
                    pytrace=False)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Fail the test that leaves sse_starlette's process-wide exit flag set.

    Once mcp is imported, sse_starlette wraps uvicorn's Server.handle_exit to set
    AppStatus.should_exit, and every SSE response in the process then ends at once.
    Left set, it fails some later SSE test in the same worker instead of this one.
    Checked here, after every fixture of the test (monkeypatch included) is undone.
    """
    result = yield
    sse = sys.modules.get("sse_starlette.sse")
    if sse is not None and sse.AppStatus.should_exit:
        sse.AppStatus.should_exit = False  # keep the damage to this test
        pytest.fail(f"{item.nodeid} left sse_starlette's AppStatus.should_exit set; "
                    "every later SSE response in this process would end at once", pytrace=False)
    return result


def pytest_sessionfinish(session, exitstatus):
    # Catches accesses made at import/collection time or from background threads.
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        # An xdist worker: only the controller sets the run's exit status and prints
        # the summary, so hand the refused accesses over to it.
        workeroutput["real_data_violations"] = list(_real_data_violations)
    if _real_data_violations:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    """xdist controller: a worker's refused accesses fail this run as well."""
    _real_data_violations.extend(getattr(node, "workeroutput", {}).get("real_data_violations", []))


def pytest_terminal_summary(terminalreporter):
    if _real_data_violations:
        terminalreporter.section("real OS data dir access (refused)", red=True)
        for line in dict.fromkeys(_real_data_violations):
            terminalreporter.line(line)


def pytest_configure(config):
    # An external kill (runner or agent-tool timeout) sends SIGTERM, which by default
    # ends Python without unwinding. Raise instead so context managers and fixture
    # teardown still reap spawned processes.
    def _interrupt(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGTERM, _interrupt)


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """为同步测试兜底一个可用 event loop（Python 3.12 + pytest-asyncio）.

    pytest-asyncio 在 async 测试结束后会关闭并清除 MainThread 的 loop，
    之后排队的同步测试再调旧式 asyncio.get_event_loop().run_until_complete
    会 RuntimeError（单独跑通过、全量跑报错的测试间污染）。
    """
    try:
        closed = asyncio.get_event_loop().is_closed()
    except RuntimeError:
        closed = True
    if closed:
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield


_USER_NOTICE_PATH = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "user_notice.py"


@pytest.fixture(autouse=True)
def _isolate_user_notice(tmp_path_factory, monkeypatch):
    """Hooks run in-process write the user-notice ledger; keep it off the real HOME.

    Every hook loads the shared module as sys.modules["user_notice"]. Pin that
    module to a per-test state directory, reset its per-run latches (one output
    document per hook run, the last fetch failure), and never let it reach the
    real service on the default port.
    """
    module = sys.modules.get("user_notice")
    if module is None:
        spec = importlib.util.spec_from_file_location("user_notice", _USER_NOTICE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["user_notice"] = module
        spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE_DIR_OVERRIDE", str(tmp_path_factory.mktemp("notice-state")))
    monkeypatch.setattr(module, "_WROTE_DOCUMENT", False)
    monkeypatch.setattr(module, "_LAST_FAILURE", "")
    # Without an explicit AITEAM_API_URL the module would fall back to port 8000,
    # which on a developer machine is the real OS service. Refuse instead.
    monkeypatch.setattr(module, "api_url", lambda: os.environ.get("AITEAM_API_URL") or "http://127.0.0.1:9")
    yield


@pytest.fixture()
def tmp_project_dir(tmp_path: Path) -> Path:
    """创建临时目录作为项目目录."""
    project_dir = tmp_path / "test-project"
    project_dir.mkdir()
    aiteam_dir = project_dir / ".aiteam"
    aiteam_dir.mkdir()
    return project_dir


@pytest_asyncio.fixture()
async def db_repository() -> StorageRepository:
    """创建内存 SQLite 的 StorageRepository 实例.

    使用 sqlite+aiosqlite:// 内存数据库，测试结束后自动清理。
    """
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    await repo.init_db()
    yield repo  # type: ignore[misc]
    await close_db()
