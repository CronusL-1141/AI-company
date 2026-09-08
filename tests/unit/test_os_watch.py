"""Subprocess tests for scripts/os-watch.sh (唤醒体系 v2 §7.4).

用 curl 桩（PATH 前置）+ HOME 覆盖，验证退出码语义与 armed 文件生命周期。
不依赖真实 API/网络。bash 不可用时跳过。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "os-watch.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not SCRIPT.is_file(),
    reason="bash 或 os-watch.sh 不可用",
)


def _make_curl_stub(bindir: Path, body: str, exit_code: int = 0) -> None:
    """在 bindir 放一个 curl 桩，忽略参数、输出 body、按 exit_code 退出。"""
    stub = bindir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"cat <<'JSON'\n{body}\nJSON\n"
        f"exit {exit_code}\n"
    )
    stub.chmod(0o755)


def _run(bindir: Path, home: Path, env_extra: dict | None = None, timeout: int = 15):
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(home)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(SCRIPT), "sess-1", "team-1"],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def test_actionable_exits_zero(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _make_curl_stub(bindir, '{"actionable":true,"busy_agents":1,"watermark":"2026-07-14T12:00:00"}')
    r = _run(bindir, home, {"OS_WATCH_POLL": "1", "OS_WATCH_MAX": "30"})
    assert r.returncode == 0
    assert "ACTIONABLE" in r.stdout


def test_api_unreachable_exits_two(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _make_curl_stub(bindir, "", exit_code=7)  # curl 连接失败码
    r = _run(bindir, home, {"OS_WATCH_POLL": "1", "OS_WATCH_MAX": "30"})
    assert r.returncode == 2
    assert "WATCHER_API_UNREACHABLE" in r.stdout


def test_benign_then_hard_timeout_exits_three(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _make_curl_stub(bindir, '{"actionable":false,"watermark":"2026-07-14T12:00:00"}')
    r = _run(bindir, home, {"OS_WATCH_POLL": "1", "OS_WATCH_MAX": "1"})
    assert r.returncode == 3
    assert "WATCHER_TIMEOUT" in r.stdout
    assert "STATUS benign" in r.stdout  # 良性信号被吸收过至少一轮


def test_armed_file_lifecycle(tmp_path):
    """运行期写 armed 心跳文件；退出(trap)后清除。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _make_curl_stub(bindir, '{"actionable":false,"watermark":"2026-07-14T12:00:00"}')
    armed = home / ".claude" / "data" / "ai-team-os" / "wake-state" / "sess-1.armed"

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(home)
    env["OS_WATCH_POLL"] = "1"
    env["OS_WATCH_MAX"] = "30"
    proc = subprocess.Popen(["bash", str(SCRIPT), "sess-1", "team-1"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # 等它武装
        deadline = time.time() + 6
        while time.time() < deadline and not armed.exists():
            time.sleep(0.2)
        assert armed.exists(), "运行期应写 armed 心跳文件"
        armed_until = float(armed.read_text().strip())
        assert armed_until > time.time()  # 未过期
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    # trap 清理：退出后 armed 文件应被移除
    assert not armed.exists(), "退出后 trap 应清除 armed 文件"


# ── 忙 vs 死：curl 退出码语义（2026-09-08）────────────────────────────
#
# 实测三次同型故障：本机一有负载（跑全量测试、gh 上传发布正文），3 秒的 curl 预算就
# 够不着一次正常响应，watcher 当场判"API 不可达"退出。守望者因为对方忙就走人，正是
# 它最不该做的事——而高负载恰恰是最需要它守着的时候。
#
# curl 把这两种情况用不同退出码分开了，脚本必须跟着分开：
#   7  = Failed to connect  → 服务真的没了，快报
#   28 = Operation timeout  → 服务在忙，继续守
#
# 判据用"因寿命到期退出(3)"而不是"因失败退出(2)"来区分，比数轮次更稳。


def test_timeout_does_not_count_as_a_dead_service(tmp_path):
    """curl 超时（28）必须被当成"忙"：撑到寿命上限，以 3 退出而不是 2。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _make_curl_stub(bindir, "", exit_code=28)
    r = _run(bindir, home, {"OS_WATCH_POLL": "1", "OS_WATCH_MAX": "5"}, timeout=30)
    assert r.returncode == 3, (
        f"超时被当成服务已死而提前退出（rc={r.returncode}）："
        f"高负载下守望者会离岗\n{r.stdout}"
    )
    assert "WATCHER_TIMEOUT" in r.stdout


def test_connection_refused_still_exits_fast(tmp_path):
    """curl 连接被拒（7）是服务真没了：仍要快退，不能被上面的宽容拖住。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _make_curl_stub(bindir, "", exit_code=7)
    start = time.monotonic()
    r = _run(bindir, home, {"OS_WATCH_POLL": "1", "OS_WATCH_MAX": "60"}, timeout=30)
    elapsed = time.monotonic() - start
    assert r.returncode == 2, f"连接被拒应快退 2，实得 {r.returncode}\n{r.stdout}"
    assert elapsed < 20, f"连接被拒退出太慢（{elapsed:.1f}s），故障暴露被延迟"
    assert "WATCHER_API_UNREACHABLE" in r.stdout


def test_timeout_run_reports_the_hiccups(tmp_path):
    """超时期间要留痕，否则"它到底在不在守"无从判断。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _make_curl_stub(bindir, "", exit_code=28)
    r = _run(bindir, home, {"OS_WATCH_POLL": "1", "OS_WATCH_MAX": "4"}, timeout=30)
    assert "api_hiccup" in r.stdout or "api_busy" in r.stdout


# ── 信号选择：只等对端回话时别被别人的 memo 轰下岗 ──────────────


def _make_url_recording_stub(bindir: Path, capture: Path, body: str) -> None:
    """curl 桩：把收到的参数写进 capture，再输出 body。"""
    stub = bindir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {capture}\n'
        f"cat <<'JSON'\n{body}\nJSON\n"
    )
    stub.chmod(0o755)


def test_signals_env_is_passed_through(tmp_path):
    """OS_WATCH_SIGNALS 要真的进到查询串里。

    这一项之所以值得单测：整条链上任何一处漏传都不会报错，只会表现为"过滤没生效"
    ——而没生效恰好就是过滤前的正常表现，肉眼分不出来。
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    capture = tmp_path / "args.txt"
    _make_url_recording_stub(bindir, capture, '{"actionable":true}')
    _run(bindir, home, {"OS_WATCH_POLL": "1", "OS_WATCH_MAX": "5",
                        "OS_WATCH_SIGNALS": "mentions"})
    assert "signals=mentions" in capture.read_text()


def test_signals_absent_by_default(tmp_path):
    """不设就完全不传该参数，让服务端走它自己的缺省（全集）。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    capture = tmp_path / "args.txt"
    _make_url_recording_stub(bindir, capture, '{"actionable":true}')
    _run(bindir, home, {"OS_WATCH_POLL": "1", "OS_WATCH_MAX": "5"})
    assert "signals=" not in capture.read_text()
