"""CC 零漂移差分——Codex 改造期间的守门件。

这套改造要往共享面（types / ORM / 迁移 / hook 入口）动手，而 CC 是这个仓库唯一在生产
里跑着的宿主。「我们没弄坏 CC」这句话必须有人能验，否则它只是一句话。

验法分两段，缺一段就漏掉半个可漂移面：

* **段 1（`test_hook_core_equivalence.py`）**：hook 侧加工。同一条 stdin 过冻结的
  `send_event.py` 与进程内 `hook_core`，POST body 逐字节比对。
* **段 2（本文件）**：服务端落库。把段 1 抓到的 body 序列按录制顺序灌进
  `HookTranslator`，导出 `agents` / `agent_activities` / `events` 三表的规范化快照，
  与 golden 逐项比对。

基线怎么来的：`scripts/compute_cc_hook_golden.py` 在 **master** 的临时 worktree 上跑
出 golden，再拿到分支上回放。分支上复算出同一份，才叫零漂移。

「零漂移」的准确含义
--------------------
三表规范化快照等于 golden，且 Codex 新增列恒为 NULL。**不是**"一个字节都不许变"：
P0-2 里那些自觉的核心修复（比如 `tool_response` 改成类型无关截断）本来就会动这份
golden，走的是"重算 golden + PR 内人审 golden diff"这条路。本测试要挡的是**无声的**
改变——red 之后你可以决定接受它，但你不能不知道它发生了。

新列为什么断言 NULL 而不是忽略
------------------------------
`harness` / `harness_version` / `dispatch_call_id` / `reasoning_output_tokens` /
`turn_id` 这几列是给 Codex 用的。纯 CC 回放要是往里写了值，说明采集侧已经开始按 CC
的形状猜 Codex 的字段——那种脏数据事后分不出真假。列还没落地的分支上，本测试跳过并打
印原因，不假绿。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "cc-hooks"
SCRIPT = ROOT / "scripts" / "compute_cc_hook_golden.py"

_spec = importlib.util.spec_from_file_location("compute_cc_hook_golden", SCRIPT)
ccg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ccg)

MANIFEST_TEXT = (FIXTURES / "MANIFEST.json").read_text(encoding="utf-8")
MANIFEST = json.loads(MANIFEST_TEXT)
GOLDEN = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))

NEW_COLUMN_IDS = [f"{t}.{c}" for t, cols in ccg.NEW_COLUMNS.items() for c in cols]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def replay():
    """整条流水线跑一次，全模块共用——每条用例各跑一次要开 27 个子进程。"""
    manifest, golden, problems, extras = ccg.build_baseline()
    return {"manifest": manifest, "golden": golden, "problems": problems, "extras": extras}


# --------------------------------------------------------------- 夹具完整性


def test_corpus_files_match_the_manifest():
    """夹具被手改一个字节就红——语料变了基线必须跟着重算。"""
    on_disk = {p.name for p in ccg.corpus_files()}
    assert on_disk == set(MANIFEST["files"]), "语料目录与 MANIFEST 登记不符"
    for name, meta in MANIFEST["files"].items():
        text = (FIXTURES / name).read_text(encoding="utf-8")
        assert sha256_text(text) == meta["sha256"], f"{name} 与 MANIFEST 登记的 sha256 不符"
        assert len(text.encode("utf-8")) == meta["bytes"]


def test_corpus_is_pure_ascii():
    """夹具不许带任何非 ASCII——脱敏闸的第二道，也是"零私有内容"的可见证据。"""
    for path in ccg.corpus_files():
        text = path.read_text(encoding="utf-8")
        assert text.isascii(), f"{path.name} 含非 ASCII 字符"


def test_golden_is_bound_to_its_generator_and_manifest():
    """基线必须能说清它是谁、用什么、在什么语料上算出来的。

    三条绑定缺一条，golden 就可能是某个已经不在树里的脚本留下的遗物。
    """
    assert GOLDEN["generator"]["script"] == "scripts/compute_cc_hook_golden.py"
    assert GOLDEN["generator"]["sha256"] == sha256_text(SCRIPT.read_text(encoding="utf-8")), (
        "生成器已改动，golden 却没重算——跑 python3 scripts/compute_cc_hook_golden.py --write"
    )
    assert GOLDEN["manifest_sha256"] == sha256_text(MANIFEST_TEXT)
    assert MANIFEST["hook_entry"]["sha256"] == hashlib.sha256(
        (ROOT / "plugin" / "hooks" / "send_event.py").read_bytes()
    ).hexdigest()


def test_manifest_covers_every_required_branch():
    covered = {b for entry in MANIFEST["coverage"].values() for b in entry["branches"]}
    missing = set(MANIFEST["required_branches"]) - covered
    assert not missing, f"MANIFEST 的覆盖矩阵缺分支：{sorted(missing)}"


# ------------------------------------------------------------------- 段 2 回放


def test_replay_reports_no_problems(replay):
    assert replay["problems"] == []


@pytest.mark.parametrize("table", ccg.TABLES)
def test_table_snapshot_matches_golden(replay, table):
    """先按表比，红了能定位到表；再逐行比，红了能定位到行。"""
    got = replay["golden"]["tables"][table]
    want = GOLDEN["tables"][table]
    assert got["columns"] == want["columns"], f"{table}: 比对列集变了"
    assert got["row_count"] == want["row_count"], f"{table}: 行数 {got['row_count']} ≠ {want['row_count']}"
    for index, (a, b) in enumerate(zip(got["rows"], want["rows"], strict=True)):
        assert a == b, f"{table}: 第 {index} 行与 golden 不同"
    assert got["sha256"] == want["sha256"]


def test_hook_bodies_match_golden(replay):
    """段 1 的产物也进基线：body 变了而三表没变，同样是漂移。"""
    for got, want in zip(replay["golden"]["bodies"], GOLDEN["bodies"], strict=True):
        assert got["case_id"] == want["case_id"]
        assert got["branches"] == want["branches"], f"{got['case_id']}: 走到的分支变了"
        assert got["body"] == want["body"], f"{got['case_id']}: POST body 与 golden 不同"


def test_handler_status_matches_golden(replay):
    """哪些事件被 translator 处理、哪些落 ignored，本身就是要冻住的行为。"""
    assert replay["golden"]["handler_status"] == GOLDEN["handler_status"]


def test_whole_baseline_reproduces(replay):
    """兜底一条：整份 MANIFEST 与 golden 逐字节复现，前面漏比的字段也跑不掉。"""
    manifest_text, golden_text = ccg.render(replay["manifest"], replay["golden"])
    assert manifest_text == MANIFEST_TEXT
    assert golden_text == (FIXTURES / "golden.json").read_text(encoding="utf-8")


# ----------------------------------------------------------------- 新列恒 NULL


@pytest.mark.parametrize("column_id", NEW_COLUMN_IDS)
def test_codex_columns_stay_null_on_a_cc_replay(replay, column_id):
    count = replay["extras"]["new_columns"][column_id]
    if count is None:
        pytest.skip(f"{column_id} 尚未落到 schema（另一簇并行加列中），本分支无从断言")
    assert count == 0, f"{column_id} 在纯 CC 回放里被写了 {count} 个非空值"


# ------------------------------------------------------------------- 真机录制


def test_real_capture_arm():
    """真机录制这条臂本批不可得，跳过并打印原因——不假绿。"""
    capture = MANIFEST["capture"]
    if capture["status"] == "pending":
        pytest.skip(f"真机语料待录：{capture['reason']}")
    assert capture["files"], "capture 标了 present 却没有文件"
    for name in capture["files"]:
        assert (FIXTURES / name).exists()
        assert name in MANIFEST["files"], f"{name} 未登记进 MANIFEST"
