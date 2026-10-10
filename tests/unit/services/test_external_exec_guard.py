"""Real OS death injection: the guardian, not test cleanup, stops native work."""

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.external_exec import ExternalExecRepository
from aiteam.types import ExternalExecRegistration


def active(pid: int) -> bool:
    status = (
        subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False
        )
        .stdout.decode()
        .strip()
    )
    return bool(status) and not status.startswith("Z")


@pytest.mark.parametrize(
    "mode",
    [
        "pid-kill",
        "pid-hup",
        "group-kill",
        "ignored-term",
        "hup-handler-spawn",
        "term-handler-spawn",
        "stderr-backpressure",
        "stdout-backpressure-hup",
    ],
)
async def test_adapter_death_stops_native_child_and_separate_group_work_without_test_killing_them(tmp_path, mode):
    repository = ExternalExecRepository(f"sqlite+aiosqlite:///{tmp_path / 'guard.sqlite'}")
    await repository.init_db()
    value = ExternalExecRegistration(invocation_id=str(uuid4()), source_namespace="a" * 64)
    child_pid, worker_pid, work = [tmp_path / name for name in ("native.pid", "worker.pid", "work.counter")]
    worker = (
        "import os,signal,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(worker_pid)!r},'w').write(str(os.getpid()))\n"
        f"while True:\n with open({str(work)!r},'a') as f: f.write('x')\n time.sleep(.02)\n"
    )
    fake = tmp_path / "fake-codex"
    late_pid = tmp_path / "late-worker.pid"
    handler = ""
    if mode.endswith("handler-spawn"):
        late_worker = worker.replace(str(worker_pid), str(late_pid))
        handler = (
            "def shutdown(signum,frame):\n"
            f" subprocess.Popen([sys.executable,'-c',{late_worker!r}],start_new_session=True,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            f" while not os.path.exists({str(late_pid)!r}): time.sleep(.01)\n"
            " sys.exit(0)\n"
            f"signal.signal(signal.{'SIGHUP' if mode.startswith('hup') else 'SIGTERM'},shutdown)\n"
        )
    events = (
        "\n".join(
            json.dumps(event)
            for event in [
                {"type": "thread.started", "thread_id": "guarded-thread"},
                {"type": "turn.started"},
                {"type": "turn.completed", "usage": {"input_tokens": 70, "output_tokens": 2}},
            ]
        )
        + "\n"
    )
    fake.write_text(
        f"#!{sys.executable}\nimport os,signal,subprocess,sys,time\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f"{handler}"
        f"open({str(child_pid)!r},'w').write(str(os.getpid()))\n"
        f"subprocess.Popen([sys.executable,'-c',{worker!r}],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"sys.stdout.write({events!r});sys.stdout.flush()\n"
        + ("os.write(2,b'PRIVATE-BLOCKED-STDERR'*800000)\n" if mode == "stderr-backpressure" else "")
        + ("os.write(1,b'PRIVATE-BLOCKED-STDOUT'*800000)\n" if mode == "stdout-backpressure-hup" else "")
        + "time.sleep(30)\n"
    )
    fake.chmod(0o700)
    helper = (
        "import asyncio,sys\n"
        f"sys.path.insert(0,{str(Path(__file__).parents[3] / 'src')!r})\n"
        "from aiteam.storage.external_exec import ExternalExecRepository\n"
        "from aiteam.services.external_exec import run_external_exec\n"
        "from aiteam.types import ExternalExecRegistration\n"
        f"r=ExternalExecRepository({repository._db_url!r})\n"
        f"v=ExternalExecRegistration.model_validate_json({value.model_dump_json()!r})\n"
        f"raise SystemExit(asyncio.run(run_external_exec(r,v,[],executable={str(fake)!r},stdout=sys.stdout.buffer)))\n"
    )
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        helper,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    passed = False
    try:
        async with asyncio.timeout(10):
            for _ in range(3):
                assert await process.stdout.readline()
            while not worker_pid.exists() or not work.exists():
                await asyncio.sleep(0.01)
            while True:
                current = await repository.get(value.invocation_id)
                if current is not None and current.receipts:
                    break
                await asyncio.sleep(0.01)
        pids = [int(child_pid.read_text()), int(worker_pid.read_text())]
        assert all([await asyncio.to_thread(active, pid) for pid in pids])
        if "backpressure" in mode:
            # Leave both outer readers open but unconsumed until work is stopped.
            await asyncio.sleep(0.3)
        if mode == "group-kill":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            os.kill(
                process.pid,
                {
                    "pid-kill": signal.SIGKILL,
                    "pid-hup": signal.SIGHUP,
                    "ignored-term": signal.SIGTERM,
                    "hup-handler-spawn": signal.SIGHUP,
                    "term-handler-spawn": signal.SIGTERM,
                    "stderr-backpressure": signal.SIGKILL,
                    "stdout-backpressure-hup": signal.SIGHUP,
                }[mode],
            )
        async with asyncio.timeout(12):
            if "backpressure" not in mode:
                await process.wait()
            if late_pid.exists():
                pids.append(int(late_pid.read_text()))
            while any([await asyncio.to_thread(active, pid) for pid in pids]):
                await asyncio.sleep(0.03)
            while (await repository.get(value.invocation_id)).status == "running":
                await asyncio.sleep(0.03)
        # No hand-kill precedes these assertions. The independent work group also
        # stopped making progress, even if OS reaping briefly leaves a zombie.
        count = work.stat().st_size
        await asyncio.sleep(0.2)
        assert work.stat().st_size == count
        if "backpressure" in mode:
            # Reading earlier would resolve the very failure this test injects.
            async with asyncio.timeout(5):
                await process.communicate()
        current = await ExternalExecRepository(repository._db_url).get(value.invocation_id)
        assert current.status == "interrupted" and current.receipts["turn:1"].usage.input_tokens == 70
        if mode in ("pid-kill", "group-kill", "stderr-backpressure"):
            assert "parent_lost" in current.diagnostics
        if mode.endswith("handler-spawn"):
            assert not late_pid.exists()
        passed = True
    finally:
        if not passed:
            for path in (child_pid, worker_pid, late_pid):
                if path.exists():
                    try:
                        os.kill(int(path.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        if process.returncode is None:
            process.kill()
            await process.communicate()
        await engine_pool.get_engine(repository._db_url).dispose()


def test_process_discovery_failure_is_bounded_and_never_claims_complete_cleanup(tmp_path, monkeypatch):
    from aiteam.services import external_exec_guard as guard

    worker_pid = tmp_path / "unknown-worker.pid"
    worker = f"import os,time;open({str(worker_pid)!r},'w').write(str(os.getpid()));time.sleep(30)"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,sys,time\n"
            f"subprocess.Popen([sys.executable,'-c',{worker!r}],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "time.sleep(30)",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    victim = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
    worker_id = None
    try:
        import time

        until = time.monotonic() + 5
        while not worker_pid.exists() and time.monotonic() < until:
            time.sleep(0.01)
        worker_id = int(worker_pid.read_text())
        assert guard._trusted_ps().startswith("/")
        monkeypatch.setattr(guard, "_trusted_ps", lambda: "/bin/false")
        began = time.monotonic()
        code, verified = guard._stop(child, signal.SIGHUP)
        assert code == -signal.SIGKILL and not verified and time.monotonic() - began < 5
        assert active(worker_id) and victim.poll() is None
        # This asserts an honest failure boundary, not a no-orphan success.
    finally:
        for pid in ([worker_id] if worker_id else []) + [child.pid, victim.pid]:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait(timeout=3)
        victim.wait(timeout=3)


async def test_large_binary_stdout_and_stderr_are_byte_exact_without_persisting_content(tmp_path):
    repository = ExternalExecRepository(f"sqlite+aiosqlite:///{tmp_path / 'binary.sqlite'}")
    await repository.init_db()
    value = ExternalExecRegistration(invocation_id=str(uuid4()), source_namespace="a" * 64)
    events = b'{"type":"thread.started","thread_id":"binary-thread"}\n'
    events += b'{"type":"turn.started"}\n'
    ending = b'{"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":1}}\n'
    binary_line = b"PRIVATE-BINARY\xff\xfe" * 250000 + b"\n"
    small_line = (b"PRIVATE-LINE\xff" * 300 + b"\n") * 6000
    stdout_expected = events + binary_line + small_line + ending
    stderr_expected = bytes(range(256)) * 1200
    fake = tmp_path / "binary-codex"
    fake.write_text(
        f"#!{sys.executable}\nimport os,sys\n"
        "def send(fd,b):\n while b: b=b[os.write(fd,b):]\n"
        f"send(2,bytes(range(256))*1200)\nsend(1,{events!r})\n"
        "send(1,b'PRIVATE-BINARY\\xff\\xfe'*250000+b'\\n')\n"
        "send(1,(b'PRIVATE-LINE\\xff'*300+b'\\n')*6000)\n"
        f"send(1,{ending!r})\nsys.exit(7)\n"
    )
    fake.chmod(0o700)
    helper = (
        "import asyncio,sys\n"
        f"sys.path.insert(0,{str(Path(__file__).parents[3] / 'src')!r})\n"
        "from aiteam.storage.external_exec import ExternalExecRepository\n"
        "from aiteam.services.external_exec import run_external_exec\n"
        "from aiteam.types import ExternalExecRegistration\n"
        f"r=ExternalExecRepository({repository._db_url!r})\n"
        f"v=ExternalExecRegistration.model_validate_json({value.model_dump_json()!r})\n"
        f"raise SystemExit(asyncio.run(run_external_exec(r,v,[],executable={str(fake)!r},stdout=sys.stdout.buffer)))\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        helper,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(30):
            actual_stdout, actual_stderr = await process.communicate()
        assert process.returncode == 7
        assert len(actual_stdout) == len(stdout_expected)
        assert hashlib.sha256(actual_stdout).digest() == hashlib.sha256(stdout_expected).digest()
        assert hashlib.sha256(actual_stderr).digest() == hashlib.sha256(stderr_expected).digest()
        saved = await repository.get(value.invocation_id)
        assert saved.status == "failed" and saved.exit_code == 7
        assert saved.receipts["turn:1"].usage.input_tokens == 7
        assert set(saved.diagnostics) <= {"oversized_event", "invalid_event", "missing_turn_id"}
        assert b"PRIVATE" not in (tmp_path / "binary.sqlite").read_bytes()
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        await engine_pool.get_engine(repository._db_url).dispose()


def test_nonconverging_process_discovery_stops_after_bounded_rounds(tmp_path, monkeypatch):
    from aiteam.services import external_exec_guard as guard

    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
    calls = []

    def changing(root):
        from types import SimpleNamespace

        import psutil

        calls.append(root)
        fake_pid = 1000000000 + len(calls)
        return {
            root: psutil.Process(root),
            fake_pid: SimpleNamespace(
                pid=fake_pid, create_time=lambda: 0, ppid=lambda: root, cmdline=lambda: [], send_signal=lambda s: None
            ),
        }

    monkeypatch.setattr(guard, "_tree", changing)
    try:
        code, verified = guard._stop(child, signal.SIGTERM)
        assert code == -signal.SIGKILL and not verified and len(calls) == 4
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)


def test_stop_unknown_wait_result_stays_unknown_and_unverified(monkeypatch):
    from aiteam.services import external_exec_guard as guard

    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
    actual_wait = child.wait

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(child.args, 3)

    monkeypatch.setattr(child, "wait", timeout)
    try:
        code, verified = guard._stop(child, signal.SIGHUP)
        assert code is None and not verified
    finally:
        actual_wait(timeout=3)


def test_direct_guard_non_posix_rejects_before_accessing_posix_signals(monkeypatch):
    from types import SimpleNamespace

    from aiteam.services import external_exec_guard as guard

    life_read, life_write = os.pipe()
    status_read, status_write = os.pipe()
    monkeypatch.setattr(guard, "os", SimpleNamespace(name="nt", write=os.write, close=os.close))
    monkeypatch.setattr(guard, "signal", SimpleNamespace())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "guard",
            "--life-fd",
            str(life_read),
            "--status-fd",
            str(status_write),
            "--db-url",
            "unused",
            "--invocation-id",
            "unused",
            "--",
            "must-not-start",
        ],
    )
    try:
        assert guard.main() == 127
        packet = json.loads(os.read(status_read, 4096))
        assert packet["spawn_failed"] and packet["exit_code"] == 127
    finally:
        os.close(life_write)
        os.close(status_read)


def test_guard_normalizes_ignored_sigchld_before_native_spawn_without_changing_caller():
    from aiteam.services import external_exec_guard as guard

    life_read, life_write = os.pipe()
    status_read, status_write = os.pipe()
    original = signal.getsignal(signal.SIGCHLD)
    wrapper = (
        "import types,pathlib,time,signal\n"
        "module=types.ModuleType('isolated_guard')\n"
        f"module.__file__={guard.__file__!r}\n"
        "exec(compile(pathlib.Path(module.__file__).read_bytes(),module.__file__,'exec'),module.__dict__)\n"
        "Base=module._ExitObserver\n"
        "class DelayedObserver(Base):\n"
        " def __init__(self,child):\n  time.sleep(.15);super().__init__(child)\n"
        "module._ExitObserver=DelayedObserver\n"
        "assert signal.getsignal(signal.SIGCHLD)==signal.SIG_IGN\n"
        # Darwin preserves the handler across exec but may clear auto-reap's
        # kernel flag. Reinstall the inherited policy in this child only.
        "signal.signal(signal.SIGCHLD,signal.SIG_IGN)\n"
        "raise SystemExit(module.main())\n"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            wrapper,
            "--life-fd",
            str(life_read),
            "--status-fd",
            str(status_write),
            "--db-url",
            "unused",
            "--invocation-id",
            "unused",
            "--",
            "/bin/sh",
            "-c",
            "exit 7",
        ],
        pass_fds=(life_read, status_write),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=lambda: signal.signal(signal.SIGCHLD, signal.SIG_IGN),
    )
    os.close(life_read)
    os.close(status_write)
    try:
        process.communicate(timeout=5)
        packet_raw = os.read(status_read, 4096)
        assert packet_raw, "ignored SIGCHLD must not erase the native exit packet"
        packet = json.loads(packet_raw)
        assert process.returncode == 7 and packet["exit_code"] == 7 and not packet["interrupted"]
        assert signal.getsignal(signal.SIGCHLD) == original
    finally:
        os.close(life_write)
        os.close(status_read)
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)


def test_reaped_native_never_signals_reused_pid_or_its_family(monkeypatch):
    from aiteam.services import external_exec_guard as guard

    child = subprocess.Popen(["/bin/sleep", ".01"], start_new_session=True)
    child.wait(timeout=3)
    victim = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    child.pid = victim.pid  # Deterministic kernel-reuse surrogate after reap.
    discover = guard._tree
    calls = []

    def observed(pid):
        calls.append(pid)
        return discover(pid)

    monkeypatch.setattr(guard, "_tree", observed)
    try:
        code, verified = guard._stop(child, signal.SIGINT)
        assert code == 0 and not verified and victim.poll() is None and not calls
    finally:
        victim.kill()
        victim.wait(timeout=3)


def test_native_exit_observer_keeps_dead_root_unreaped_until_explicit_cleanup():
    import psutil

    from aiteam.services import external_exec_guard as guard

    child = subprocess.Popen(["/bin/sleep", ".02"], start_new_session=True)
    observer = guard._ExitObserver(child)
    try:
        until = time.monotonic() + 3
        while not observer.exited() and time.monotonic() < until:
            time.sleep(0.01)
        assert observer.exited() and child.returncode is None
        assert psutil.Process(child.pid).status() == psutil.STATUS_ZOMBIE
    finally:
        observer.close()
        child.wait(timeout=3)


def test_fast_separate_session_spawner_stops_all_recorded_work(tmp_path, monkeypatch):
    import psutil

    from aiteam.services import external_exec_guard as guard

    pid_log, ready, window = [tmp_path / name for name in ("spawned-pids", "ready", "kill-window")]
    # Each worker also records itself: failed withdrawal cleanup cannot miss a
    # spawn whose parent was killed between posix_spawn and its PID-log append.
    worker = (
        "import os\n"
        f"fd=os.open({str(pid_log)!r},os.O_CREAT|os.O_WRONLY|os.O_APPEND,0o600)\n"
        "os.write(fd,(str(os.getpid())+'\\n').encode());os.close(fd)\n"
        "os.execv('/bin/sleep',['sleep','40'])\n"
    )
    script = (
        "import os,sys,time\n"
        f"f=open({str(pid_log)!r},'a',buffering=1)\n"
        "for index in range(600):\n"
        " if index==40:\n"
        f"  open({str(ready)!r},'w').close()\n"
        f"  while not os.path.exists({str(window)!r}):time.sleep(.002)\n"
        f" pid=os.posix_spawn(sys.executable,[sys.executable,'-c',{worker!r}],os.environ,setsid=True)\n"
        " f.write(str(pid)+'\\n');time.sleep(.002)\n"
        "time.sleep(40)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    discover, signal_scope = guard._tree, guard._signal_tree
    snapshots, first = [], {}

    def observe(root):
        if first:
            snapshots.append(psutil.Process(root).status())  # Evidence, never a pass/fail proxy.
        found = discover(root)
        if not first:
            first.update(found)
        return found

    def kill_window(tree, signum):
        if signum == signal.SIGKILL:
            before = len(set(pid_log.read_text().splitlines()))
            window.touch()
            # Test-only scheduling: unfrozen native really forks new setsid work
            # after the last ownership snapshot. Frozen native cannot do so.
            until = time.monotonic() + 0.4
            if psutil.Process(child.pid).status() == psutil.STATUS_STOPPED:
                time.sleep(0.05)
            else:
                while len(set(pid_log.read_text().splitlines())) < before + 5 and time.monotonic() < until:
                    time.sleep(0.005)
        return signal_scope(tree, signum)

    monkeypatch.setattr(guard, "_tree", observe)
    monkeypatch.setattr(guard, "_signal_tree", kill_window)
    passed = False
    all_pids = []
    try:
        until = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert ready.exists()
        code, verified = guard._stop(child, signal.SIGKILL)
        assert code == -signal.SIGKILL and not verified
        # Allow already-created worker bootstraps to record their own PID.
        time.sleep(0.1)
        all_pids = sorted({int(pid) for pid in pid_log.read_text().splitlines()})
        assert 40 <= len(all_pids) <= 600
        until = time.monotonic() + 3
        while any(active(pid) for pid in all_pids) and time.monotonic() < until:
            time.sleep(0.02)
        survivors = []
        for pid in all_pids:
            if active(pid):
                process = psutil.Process(pid)
                survivors.append({"pid": pid, "birth": process.create_time(), "status": process.status()})
        evidence_dir = os.environ.get("AITEAM_EXTERNAL_EVIDENCE_DIR")
        if evidence_dir:
            directory = Path(evidence_dir)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "fast-spawner.json").write_text(
                json.dumps(
                    {
                        "native_pid": child.pid,
                        "spawned_pids": all_pids,
                        "survivors": survivors,
                        "observed_rediscovery_status": snapshots,
                        "cleanup_unverified": True,
                    },
                    indent=2,
                )
                + "\n"
            )
        assert not survivors, f"real separate-session survivors after cleanup: {survivors}"
        passed = True
    finally:
        if child.returncode is None:
            # Failure cleanup only, after every assertion. Freeze our spawner
            # before capturing exact descendants, then kill recorded identities.
            os.kill(child.pid, signal.SIGSTOP)
            extra = psutil.Process(child.pid).children(recursive=True)
            child.kill()
            child.wait(timeout=3)
            for process in extra:
                try:
                    process.kill()
                except psutil.NoSuchProcess:
                    pass
        if not passed:
            time.sleep(0.1)
            all_pids = sorted({int(pid) for pid in pid_log.read_text().splitlines()}) if pid_log.exists() else []
            for pid in all_pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
