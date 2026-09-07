"""send_event.py 是冻结的 CC 入口——改一个字节就必须是一次自觉的决定。

Codex 适配器要复用 send_event 里那几块与宿主无关的加工逻辑（API URL 解析、载荷截
断、POST 与失败记账）。做法有两条：让 send_event `import` 一个共用模块，或者把那几
块**逐字复制**进 `hook_core.py`、两边各留一份。

选了后者，理由只有一条：`import` 一行也是改字节。而"CC 这次一个字节都没被动过"这句
话，只有在能机检的时候才值钱——人工 diff review 说不出这个强度的话。于是 send_event
的 sha256 被钉进 `scripts/hook_entry_freeze.json`，本测试比对它。

所以两份重复代码不是失误，是这条纪律的代价，`hook_core.py` 文件头写明了禁止"去重"。
等价性由 `test_hook_core_equivalence.py` 逐字节保证，不靠自觉。

**解冻的正当路径**（不是绕过本测试）：改动走核心 PR，同批重算
`tests/fixtures/cc-hooks/golden.json`，并在 PR 里人审 golden diff——那份 diff 就是
"已批准的行为变更清单"。改完把新 sha256 写回冻结档。偷偷改常量让测试转绿，等于把这
条审查链整条拆掉。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FREEZE_FILE = ROOT / "scripts" / "hook_entry_freeze.json"
FROZEN: dict[str, str] = json.loads(FREEZE_FILE.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("rel_path", sorted(FROZEN))
def test_frozen_entry_still_matches_its_recorded_hash(rel_path: str):
    path = ROOT / rel_path
    assert path.exists(), f"冻结档登记了不存在的文件：{rel_path}"
    assert sha256(path) == FROZEN[rel_path], (
        f"{rel_path} 已被修改，与 scripts/hook_entry_freeze.json 不符。\n"
        "若这是一次有意的核心修复：重算 tests/fixtures/cc-hooks/golden.json，"
        "在 PR 里人审 golden diff，然后把新 sha256 写回冻结档。"
    )


def test_freeze_file_covers_the_cc_entry():
    """冻结档是单一真相源，CC 入口必须在册——否则这条纪律等于没有。"""
    assert "plugin/hooks/send_event.py" in FROZEN


def test_mirror_copy_is_covered_by_the_i1_pairing():
    """只钉一份就够：I1 保证 plugin/hooks 与 src/aiteam/hooks 同名文件逐字节一致。

    这条断言把那个隐含前提摆到明面上。哪天 I1 的配对关系变了，冻结的覆盖面会跟着缩
    水，而缩水本身是无声的——这里出声。
    """
    plugin_copy = ROOT / "plugin" / "hooks" / "send_event.py"
    src_copy = ROOT / "src" / "aiteam" / "hooks" / "send_event.py"
    assert sha256(plugin_copy) == sha256(src_copy)
