"""Real isolated ownership probes; never address or signal the deployed API."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
import pytest

from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.external_exec import ExternalExecRepository
from aiteam.types import ExternalExecRegistration

SRC = Path(__file__).parents[3] / "src"
EVENTS = (
    '{"type":"thread.started","thread_id":"ownership-thread"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"turn.completed","usage":{"input_tokens":31,"output_tokens":1}}\n'
)


def evidence(name, facts):
    target = os.environ.get("AITEAM_EXTERNAL_EVIDENCE_DIR")
    if target:
        directory = Path(target)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(json.dumps(facts, indent=2) + "\n")


def live(pid):
    try:
        return psutil.Process(pid).status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
    except psutil.NoSuchProcess:
        return False


def cleanup_recorded(paths):
    for path in paths:
        if path.exists():
            try:
                psutil.Process(int(path.read_text())).kill()
            except psutil.NoSuchProcess:
                pass


async def launch(tmp_path, body, *, environment=None):
    repo = ExternalExecRepository(f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}")
    await repo.init_db()
    value = ExternalExecRegistration(invocation_id=str(uuid4()), source_namespace="e" * 64)
    fake = tmp_path / "codex"
    fake.write_text(f"#!{sys.executable}\nimport os,sys,time,subprocess,json\n{body}\n")
    fake.chmod(0o700)
    helper = (
        "import asyncio,sys\n"
        f"sys.path.insert(0,{str(SRC)!r})\n"
        "from aiteam.storage.external_exec import ExternalExecRepository\n"
        "from aiteam.services.external_exec import run_external_exec\n"
        "from aiteam.types import ExternalExecRegistration\n"
        f"r=ExternalExecRepository({repo._db_url!r})\n"
        f"v=ExternalExecRegistration.model_validate_json({value.model_dump_json()!r})\n"
        f"raise SystemExit(asyncio.run(run_external_exec(r,v,[],executable={str(fake)!r},stdout=sys.stdout.buffer)))\n"
    )
    adapter = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        helper,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    return repo, value, adapter


async def ready(repo, value, adapter):
    async with asyncio.timeout(15):
        for _ in range(3):
            assert await adapter.stdout.readline()
        while True:
            item = await repo.get(value.invocation_id)
            if item and item.receipts:
                return
            await asyncio.sleep(0.01)


async def wait_stopped(pids):
    async with asyncio.timeout(12):
        while any(live(pid) for pid in pids):
            await asyncio.sleep(0.03)


@pytest.mark.parametrize("mode", ["adopted-same-session", "double-fork-new-session"])
async def test_adopted_work_is_stopped_or_explicitly_unverified(tmp_path, mode):
    native, worker = tmp_path / "native.pid", tmp_path / "worker.pid"
    code = (
        "import os,time\n"
        + ("os.setpgid(0,0)\n" if mode == "adopted-same-session" else "os.setsid()\n")
        + f"open({str(worker)!r},'w').write(str(os.getpid()))\n"
        + "time.sleep(40)\n"
    )
    middle = "import os,sys,subprocess,time\n" + (
        f"subprocess.Popen([sys.executable,'-c',{code!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
    )
    # The intermediate exits before discovery. PPID ancestry is already gone.
    body = (
        f"open({str(native)!r},'w').write(str(os.getpid()))\n"
        f"p=subprocess.Popen([sys.executable,'-c',{middle!r}]);p.wait()\n"
        f"while not os.path.exists({str(worker)!r}):time.sleep(.01)\n"
        f"sys.stdout.write({EVENTS!r});sys.stdout.flush();time.sleep(40)"
    )
    repo, value, adapter = await launch(tmp_path, body)
    try:
        await ready(repo, value, adapter)
        os.kill(adapter.pid, signal.SIGKILL)
        await wait_stopped([int(native.read_text())])
        async with asyncio.timeout(12):
            while (await repo.get(value.invocation_id)).status == "running":
                await asyncio.sleep(0.03)
        stored = await repo.get(value.invocation_id)
        assert "process_cleanup_unverified" in stored.diagnostics
        if mode == "adopted-same-session":
            await wait_stopped([int(worker.read_text())])
        else:
            assert live(int(worker.read_text()))  # Honest ownership limit, not a success cleanup claim.
        evidence(
            mode,
            {
                "native_pid": int(native.read_text()),
                "worker_pid": int(worker.read_text()),
                "native_active": False,
                "worker_active": live(int(worker.read_text())),
                "cleanup_unverified": True,
            },
        )
    finally:
        cleanup_recorded([native, worker])
        if adapter.returncode is None:
            adapter.kill()
        await adapter.communicate()
        await engine_pool.get_engine(repo._db_url).dispose()


async def test_term_then_adapter_kill_during_cleanup_still_records_interrupted(tmp_path):
    native, worker = tmp_path / "native.pid", tmp_path / "worker.pid"
    code = f"import os,time;open({str(worker)!r},'w').write(str(os.getpid()));time.sleep(40)"
    body = (
        f"open({str(native)!r},'w').write(str(os.getpid()))\n"
        f"subprocess.Popen([sys.executable,'-c',{code!r}],start_new_session=True)\n"
        f"while not os.path.exists({str(worker)!r}):time.sleep(.01)\n"
        f"sys.stdout.write({EVENTS!r});sys.stdout.flush();time.sleep(40)"
    )
    repo, value, adapter = await launch(tmp_path, body)
    try:
        await ready(repo, value, adapter)
        os.kill(adapter.pid, signal.SIGTERM)
        await asyncio.sleep(0.02)
        try:
            os.kill(adapter.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await wait_stopped([int(native.read_text()), int(worker.read_text())])
        async with asyncio.timeout(12):
            while (await repo.get(value.invocation_id)).status == "running":
                await asyncio.sleep(0.03)
        assert (await repo.get(value.invocation_id)).status == "interrupted"
        evidence(
            "term-20ms-kill",
            {
                "native_pid": int(native.read_text()),
                "worker_pid": int(worker.read_text()),
                "both_active": False,
                "status": "interrupted",
            },
        )
    finally:
        cleanup_recorded([native, worker])
        if adapter.returncode is None:
            adapter.kill()
        await adapter.communicate()
        await engine_pool.get_engine(repo._db_url).dispose()


async def test_guardian_death_never_turns_guard_exit_into_native_exit_fact(tmp_path):
    native = tmp_path / "native.pid"
    body = (
        f"open({str(native)!r},'w').write(str(os.getpid()))\n"
        f"sys.stdout.write({EVENTS!r});sys.stdout.flush();time.sleep(40)"
    )
    repo, value, adapter = await launch(tmp_path, body)
    try:
        await ready(repo, value, adapter)
        guardians = psutil.Process(adapter.pid).children()
        assert len(guardians) == 1
        guardians[0].kill()  # Only this invocation's recorded controller PID.
        async with asyncio.timeout(8):
            await adapter.communicate()
        stored = await repo.get(value.invocation_id)
        assert adapter.returncode == 75 and stored.exit_code is None
        assert stored.status == "interrupted" and "process_cleanup_unverified" in stored.diagnostics
        assert live(int(native.read_text()))  # Controller loss cannot prove work stopped.
        evidence(
            "guardian-killed",
            {
                "guardian_pid": guardians[0].pid,
                "native_pid": int(native.read_text()),
                "native_active": True,
                "native_exit_code": None,
                "cli_exit_code": 75,
                "cleanup_unverified": True,
            },
        )
    finally:
        cleanup_recorded([native])
        if adapter.returncode is None:
            adapter.kill()
        await adapter.communicate()
        await engine_pool.get_engine(repo._db_url).dispose()


def test_unreadable_command_identity_preserves_entire_unknown_family(tmp_path, monkeypatch):
    from aiteam.services import external_exec_guard as guard

    unknown, family = tmp_path / "unknown.pid", tmp_path / "family.pid"
    leaf = f"import os,time;open({str(family)!r},'w').write(str(os.getpid()));time.sleep(40)"
    middle = (
        "import os,sys,subprocess,time\n"
        f"open({str(unknown)!r},'w').write(str(os.getpid()))\n"
        f"subprocess.Popen([sys.executable,'-c',{leaf!r}],start_new_session=True)\ntime.sleep(40)"
    )
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import sys,subprocess,time;subprocess.Popen([sys.executable,'-c',{middle!r}]);time.sleep(40)",
        ],
        start_new_session=True,
    )
    original = psutil.Process.cmdline
    try:
        until = time.monotonic() + 5
        while not family.exists() and time.monotonic() < until:
            time.sleep(0.01)
        identity = int(unknown.read_text())

        def unreadable(process):
            if process.pid == identity:
                raise psutil.AccessDenied(process.pid)
            return original(process)

        monkeypatch.setattr(psutil.Process, "cmdline", unreadable)
        code, verified = guard._stop(parent, signal.SIGTERM)
        assert code == -signal.SIGKILL and not verified
        assert live(identity) and live(int(family.read_text()))
    finally:
        cleanup_recorded([unknown, family])
        if parent.returncode is None:
            parent.kill()
            parent.wait(timeout=3)


def test_api_names_inside_ordinary_argv_do_not_exempt_work_from_cleanup():
    from types import SimpleNamespace

    from aiteam.services import external_exec_guard as guard

    echo = SimpleNamespace(
        cmdline=lambda: ["/bin/echo", "-m", "uvicorn", "aiteam.api.app:create_app"], exe=lambda: "/bin/echo"
    )
    assert not guard._is_api_process(echo)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(40)", "aiteam.api.app:create_app"],
        start_new_session=True,
    )
    try:
        code, verified = guard._stop(child, signal.SIGINT)
        assert code == -signal.SIGKILL and not verified
    finally:
        if child.returncode is None:
            child.kill()
            child.wait(timeout=3)


def test_stale_snapshot_never_adopts_new_unrelated_birth_as_owned_work(monkeypatch):
    from types import SimpleNamespace

    from aiteam.services import external_exec_guard as guard

    native = subprocess.Popen(["/bin/sleep", "40"], start_new_session=True)
    victim = subprocess.Popen(["/bin/sleep", "40"], start_new_session=True)
    stale = f"{native.pid} {os.getpid()} {native.pid}\n{victim.pid} {native.pid} {victim.pid}\n".encode()
    monkeypatch.setattr(guard.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=stale))
    try:
        assert psutil.Process(victim.pid).ppid() != native.pid and os.getsid(victim.pid) != native.pid
        found = guard._tree(native.pid)
        assert victim.pid not in found
        guard._signal_tree(found, signal.SIGSTOP)
        assert psutil.Process(victim.pid).status() != psutil.STATUS_STOPPED
        assert victim.poll() is None
    finally:
        # This assertion does not rely on a real kernel PID wraparound.
        native.kill()
        native.wait(timeout=3)
        victim.kill()
        victim.wait(timeout=3)


@pytest.mark.parametrize("reverse", [False, True])
def test_late_api_classification_closes_family_before_any_signal(monkeypatch, reverse):
    from types import SimpleNamespace

    from aiteam.services import external_exec_guard as guard

    seen = []

    def process(pid, parent, args):
        return SimpleNamespace(
            pid=pid,
            create_time=lambda: float(pid),
            ppid=lambda: parent,
            cmdline=lambda: args,
            exe=lambda: sys.executable,
            send_signal=lambda signum: seen.append((pid, signum)),
        )

    api = process(1000000010, 1, [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app"])
    family = process(1000000011, api.pid, [sys.executable, "-c", "synthetic api worker"])
    ordinary = process(1000000012, 1, [sys.executable, "-c", "synthetic ordinary"])
    pairs = [(api.pid, api), (family.pid, family), (ordinary.pid, ordinary)]
    if reverse:
        pairs.reverse()
    monkeypatch.setattr(guard, "_PROTECTED", {})
    monkeypatch.setattr(guard, "_PARENTS", {})
    guard._signal_tree(dict(pairs), signal.SIGSTOP)
    assert seen == [(ordinary.pid, signal.SIGSTOP)]
    assert set(guard._PROTECTED) == {api.pid, family.pid}


@pytest.mark.parametrize("launch_mode", ["direct-mixed", "runtime-cold"])
async def test_real_api_and_its_separate_group_family_survive_native_cleanup(tmp_path, launch_mode):
    native, api, owned, ordinary = [tmp_path / name for name in ("native.pid", "api.pid", "owned.pid", "ordinary.pid")]
    counter = tmp_path / "api-family.counter"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    home = tmp_path / "home"
    home.mkdir()
    site = tmp_path / "api-startup"
    site.mkdir()
    family = (
        "import os,time\n"
        f"open({str(owned)!r},'w').write(str(os.getpid()))\n"
        f"while True:\n with open({str(counter)!r},'a') as f:f.write('x')\n time.sleep(.02)\n"
    )
    (site / "sitecustomize.py").write_text(
        "import os,sys,subprocess\n"
        "if os.environ.pop('AITEAM_TEST_API_FAMILY',None):\n"
        f" subprocess.Popen([sys.executable,'-c',{family!r}],start_new_session=True,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
    )
    (home / ".aiteam").mkdir(parents=True, exist_ok=True)  # the API never starts without a data directory
    env = {
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "AITEAM_DB_PATH": str(tmp_path / "api.sqlite"),
        "AITEAM_TEST_API_FAMILY": "1",
        "PYTHONPATH": os.pathsep.join([str(site), str(SRC), *[p for p in sys.path if p.endswith("site-packages")]]),
    }
    api_command = [
        sys.executable,
        "-m",
        "uvicorn",
        "aiteam.api.app:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "critical",
    ]
    record = tmp_path / "api.json"
    barrier, before_save = tmp_path / "release", tmp_path / "before-save"
    if launch_mode == "runtime-cold":
        runtime = tmp_path / "codex_runtime.py"
        original = (SRC.parent / "scripts" / "codex_runtime.py").read_text()
        original = original.replace(
            "ROOT = Path(__file__).resolve().parent.parent", f"ROOT = Path({str(SRC.parent)!r})"
        )
        original = original.replace(
            "    save(record, data)",
            f"    Path({str(before_save)!r}).touch()\n"
            f"    while not Path({str(barrier)!r}).exists(): time.sleep(.01)\n"
            "    save(record, data)",
        )
        runtime.write_text(original)
        api_command = [
            sys.executable,
            str(runtime),
            "_child",
            str(record),
            f"http://127.0.0.1:{port}",
            json.dumps(api_command),
            "[]",
        ]
    other = f"import os,time;open({str(ordinary)!r},'w').write(str(os.getpid()));time.sleep(40)"
    body = (
        "import urllib.request\n"
        f"open({str(native)!r},'w').write(str(os.getpid()))\n"
        f"env=dict(os.environ);env.update({env!r})\n"
        f"p=subprocess.Popen({api_command!r},env=env,stdout=subprocess.DEVNULL,"
        f"stderr=open({str(tmp_path / 'api-startup.log')!r},'wb'))\n"
        f"open({str(api)!r},'w').write(str(p.pid))\n"
        f"subprocess.Popen([sys.executable,'-c',{other!r}],start_new_session=True)\n"
        + (
            f"while not os.path.exists({str(before_save)!r}):time.sleep(.01)\n"
            if launch_mode == "runtime-cold"
            else "for _ in range(200):\n"
            f" try:\n  urllib.request.urlopen('http://127.0.0.1:{port}/api/health',timeout=.2);break\n"
            " except Exception:time.sleep(.05)\n"
        )
        + f"while not os.path.exists({str(owned)!r}) or not os.path.exists({str(ordinary)!r}):time.sleep(.01)\n"
        + f"sys.stdout.write({EVENTS!r});sys.stdout.flush();time.sleep(40)"
    )
    repo, value, adapter = await launch(tmp_path, body)
    try:
        await ready(repo, value, adapter)
        ids = [int(path.read_text()) for path in (native, api, owned, ordinary)]
        assert os.getpgid(ids[0]) == os.getpgid(ids[1])  # Mixed native/API group, including cold shell.
        birth = psutil.Process(ids[1]).create_time()
        async with httpx.AsyncClient(timeout=2) as client:
            if launch_mode == "direct-mixed":
                assert (await client.get(f"http://127.0.0.1:{port}/api/health")).status_code == 200
            else:
                assert before_save.exists() and not record.exists()
            os.kill(adapter.pid, signal.SIGTERM)
            await wait_stopped([ids[0], ids[3]])
            async with asyncio.timeout(12):
                await adapter.communicate()
            before = counter.stat().st_size
            await asyncio.sleep(0.15)
            assert live(ids[1]) and live(ids[2]) and counter.stat().st_size > before
            assert psutil.Process(ids[1]).create_time() == birth
            if launch_mode == "runtime-cold":
                barrier.touch()
                async with asyncio.timeout(15):
                    while True:
                        try:
                            if (await client.get(f"http://127.0.0.1:{port}/api/health")).status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.05)
            assert (await client.get(f"http://127.0.0.1:{port}/api/health")).status_code == 200
            evidence(
                f"api-{launch_mode}",
                {
                    "native_pid": ids[0],
                    "api_pid": ids[1],
                    "family_pid": ids[2],
                    "ordinary_pid": ids[3],
                    "api_birth": birth,
                    "api_status": psutil.Process(ids[1]).status(),
                    "family_counter_delta": counter.stat().st_size - before,
                    "health_http_status": 200,
                    "mixed_group": True,
                    "native_active": False,
                    "ordinary_active": False,
                },
            )
        stored = await repo.get(value.invocation_id)
        assert stored.status == "interrupted" and "process_cleanup_unverified" in stored.diagnostics
    finally:
        cleanup_recorded([native, api, owned, ordinary])
        if adapter.returncode is None:
            adapter.kill()
        await adapter.communicate()
        await engine_pool.get_engine(repo._db_url).dispose()
