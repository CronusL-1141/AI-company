"""hook_core 与冻结的 send_event 等价——差分测试的第一段。

`send_event.py` 被 sha256 钉死不许动，Codex 入口要复用它的加工逻辑，于是那几块被**逐
字复制**进 `hook_core.py`。复制的代价是两份代码会各自漂移，而漂移的方式恰恰是最难看
出来的那种：两边都能跑，只是发出去的载荷不一样了。

所以等价性在这里被做成机检，两道：

1. **行为等价**：同一条 stdin 分别过 (a) 真的 `send_event.py` 子进程、(b) 进程内
   `hook_core` 的共享块 + `send_event` 自己的 CC 专有块，两条路径 POST 出去的 body
   **逐字节相等**。共享块之外的东西两边同源，所以差异只可能来自共享块——这正是要盯
   的地方。inert 早退的载荷两侧都必须"根本没有 body"。
2. **文本等价**：`hook_core` 里每个提取块的源码文本，必须仍原样出现在 `send_event.py`
   里。行为等价管不住注释和常量说明，而下一个改这两个文件的人，读的就是注释。

语料是 `tests/fixtures/cc-hooks/synthetic-*.jsonl`，覆盖 send_event 全部边界分支
（截断、剥离、compact 测长、inert 早退、cwd 补全、CC 团队名注入），分支是否真的走到
由 `scripts/compute_cc_hook_golden.py` 实跑观测校验，不是声明了就算。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOKS_DIR = ROOT / "plugin" / "hooks"
HOOK = HOOKS_DIR / "send_event.py"

_spec = importlib.util.spec_from_file_location(
    "compute_cc_hook_golden", ROOT / "scripts" / "compute_cc_hook_golden.py"
)
ccg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ccg)


def _hook_modules():
    """Import send_event and hook_core straight out of plugin/hooks."""
    sys.path.insert(0, str(HOOKS_DIR))
    try:
        send_event = importlib.import_module("send_event")
        hook_core = importlib.import_module("hook_core")
        return importlib.reload(send_event), importlib.reload(hook_core)
    finally:
        sys.path.pop(0)


send_event, hook_core = _hook_modules()
ROWS = [r for r in ccg.load_corpus() if r.get("stage") == "stdin"]
CASE_IDS = [r["case_id"] for r in ROWS]


class _Recorder(BaseHTTPRequestHandler):
    bodies: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", "0"))
        _Recorder.bodies.append(self.rfile.read(length).decode("utf-8"))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args) -> None:
        return


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    """一台记录用 stub + 一个受控的 HOME/cwd，两条路径共用同一套外部条件。"""
    home = tmp_path_factory.mktemp("cc-home").resolve()
    work = tmp_path_factory.mktemp("cc-cwd").resolve()
    seed_session = next(
        (r["payload"].get("session_id", "") for r in ROWS if "cc_team_injected" in r.get("branches", [])),
        "",
    )
    ccg._seed_home(home, seed_session)

    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield {"api_url": api_url, "home": str(home), "work": str(work)}
    finally:
        server.shutdown()
        server.server_close()


def _normalized(raw: str | None, harness: dict) -> str | None:
    if raw is None:
        return None
    return ccg._normalize_body(raw, harness["work"], harness["home"])


def _subprocess_body(row: dict, harness: dict) -> str | None:
    """旧路径：真的跑一遍 send_event.py，抓它 POST 出去的原文。"""
    env = {**os.environ, "AITEAM_API_URL": harness["api_url"], "HOME": harness["home"]}
    env.pop("CLAUDE_PLUGIN_ROOT", None)  # 别让备份链避让在测试里开火
    env.pop("USERPROFILE", None)
    _Recorder.bodies.clear()
    proc = subprocess.run(
        [sys.executable, str(HOOK), row["argv_event"]],
        input=json.dumps(row["payload"], ensure_ascii=False),
        capture_output=True, text=True, timeout=60, env=env, cwd=harness["work"],
    )
    assert proc.returncode == 0, f"{row['case_id']}: hook 退出码 {proc.returncode}\n{proc.stderr}"
    assert len(_Recorder.bodies) <= 1, f"{row['case_id']}: 一次调用发了 {len(_Recorder.bodies)} 个 body"
    return _normalized(_Recorder.bodies[0] if _Recorder.bodies else None, harness)


def _inprocess_body(row: dict, harness: dict) -> str | None:
    """新路径：共享块取自 hook_core，CC 专有块仍取自 send_event 本身。

    照抄 send_event.main() 的动作顺序：argv 补事件名 → inert 早退 → 注入 CC 团队名
    → 补 cwd → 截断 → POST。顺序错了键序就错了，body 也就不再逐字节相等。
    """
    saved_home, saved_cwd = os.environ.get("HOME"), os.getcwd()
    os.environ["HOME"] = harness["home"]
    os.chdir(harness["work"])
    try:
        payload = json.loads(json.dumps(row["payload"], ensure_ascii=False))
        if "hook_event_name" not in payload:
            payload["hook_event_name"] = row["argv_event"]
        event_name = payload.get("hook_event_name", "")

        if send_event._is_inert(event_name, payload):
            return None

        if event_name in ("SubagentStart", "SubagentStop") and "cc_team_name" not in payload:
            cc_team = send_event._resolve_cc_team_name(payload.get("session_id", ""))
            if cc_team:
                payload["cc_team_name"] = cc_team

        if "cwd" not in payload:
            payload["cwd"] = os.getcwd()

        payload = hook_core._trim_payload(payload)

        _Recorder.bodies.clear()
        state = hook_core.post_event(payload, harness["api_url"])
        assert state == hook_core.HookPostState.POSTED, f"{row['case_id']}: post_event 返回 {state}"
        assert len(_Recorder.bodies) == 1
        return _normalized(_Recorder.bodies[0], harness)
    finally:
        os.chdir(saved_cwd)
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


# ------------------------------------------------------------------ 行为等价


@pytest.mark.parametrize("row", ROWS, ids=CASE_IDS)
def test_post_body_is_byte_identical(row, harness):
    old = _subprocess_body(row, harness)
    new = _inprocess_body(row, harness)
    if old is None or new is None:
        assert old == new, (
            f"{row['case_id']}: 一侧发了 body 另一侧没发（旧={old is not None} 新={new is not None}）"
        )
        return
    assert old == new, f"{row['case_id']}: 两条路径的 POST body 不同"


def test_every_branch_is_actually_exercised(harness):
    """语料声明的覆盖不算数，实跑观测到的才算。"""
    observed: set[str] = set()
    for row in ROWS:
        body = _subprocess_body(row, harness)
        observed |= set(ccg._observe_branches(row["payload"], json.loads(body) if body else None))
    missing = set(ccg.REQUIRED_BRANCHES) - observed
    assert not missing, f"这些 send_event 分支没有任何语料走到：{sorted(missing)}"


def test_inert_rows_reach_neither_path(harness):
    """inert 早退必须是"根本没发"，不是"发了个空的"——发出去就已经付了代价。"""
    inert = [r for r in ROWS if "inert_dropped" in r.get("branches", [])]
    assert inert, "语料里没有 inert 用例，这条纪律就没被测到"
    for row in inert:
        assert _subprocess_body(row, harness) is None
        assert _inprocess_body(row, harness) is None


def test_post_event_reports_an_unreachable_api(harness):
    """OS 没在跑是常态，不是异常：写 stderr、返回状态、绝不抛给宿主。"""
    state = hook_core.post_event({"hook_event_name": "Stop"}, "http://127.0.0.1:9")
    assert state == hook_core.HookPostState.POST_UNREACHABLE


# ------------------------------------------------------------------ 文本等价


def test_extracted_blocks_are_still_verbatim():
    """每个提取块的源码文本必须仍原样出现在 send_event.py 里。

    行为等价管不住注释。而 ESSENTIAL_FIELDS 那几段注释记的是"某个字段为什么必须留
    下"的血泪史（wf_id 提取、主会话 transcript 兜底），漂了就等于把理由弄丢了。
    """
    import inspect

    entry_source = HOOK.read_text(encoding="utf-8")
    for func in (hook_core._get_api_url, hook_core._trim_payload):
        block = inspect.getsource(func)
        assert block in entry_source, f"{func.__name__} 与 send_event.py 里的原件已不同"


def test_extracted_constants_match_the_entry():
    """常量按值对钉——注释可以各自成文，取值不许分家。"""
    assert hook_core._PORT_FILE == send_event._PORT_FILE
    assert hook_core.MAX_FIELD_LEN == send_event.MAX_FIELD_LEN
    assert hook_core.MAX_PAYLOAD_BYTES == send_event.MAX_PAYLOAD_BYTES
    assert hook_core.LARGE_FIELDS == send_event.LARGE_FIELDS
    assert hook_core.ESSENTIAL_FIELDS == send_event.ESSENTIAL_FIELDS


def test_hook_core_stays_harness_neutral():
    """CC 专有的几块不许混进共用核心，否则 Codex 入口会连 CC 的假设一起继承。

    只扫代码，不扫模块 docstring：文件头**必须**点名这几个符号，说明它们是被刻意留在
    各自入口里的。把说明文字也算作违规，等于逼着后人删掉唯一写着理由的那段话。
    """
    import ast

    source = (HOOKS_DIR / "hook_core.py").read_text(encoding="utf-8")
    module_doc = ast.get_docstring(ast.parse(source), clean=False)
    code = source.replace(module_doc, "", 1) if module_doc else source
    for name in ("_INERT_TOOLS", "_is_inert", "_resolve_cc_team_name", "_yield_if_superseded"):
        assert name not in code, f"{name} 是宿主专有的，不该出现在 hook_core.py 的代码里"


def test_hook_core_copies_are_byte_identical():
    """与仓内所有 hook 同规矩：两份副本逐字节一致（I1 也会再钉一次）。"""
    plugin_copy = (HOOKS_DIR / "hook_core.py").read_bytes()
    src_copy = (ROOT / "src" / "aiteam" / "hooks" / "hook_core.py").read_bytes()
    assert plugin_copy == src_copy


def test_send_event_does_not_import_hook_core():
    """一旦 send_event 改成 import，冻结档就守不住"CC 一个字节没动"这句话了。"""
    assert "hook_core" not in HOOK.read_text(encoding="utf-8")
