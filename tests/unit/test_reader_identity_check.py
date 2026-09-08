"""I21 机检自身的用例 —— 证明它抓得到问题，不是一条永远绿的装饰。

守的失效模式：CC 侧与 Codex 侧共用同一个信道 reader。后果是一侧读完消息推进水位，
**另一侧的徽章一起消失**，发给它的消息永远不会被提示，而两侧各自的测试全都照常通过。

这里既测"该红时真红"，也测真实仓库当前状态确实通过——只测前者会漏掉机检写错路径
（永远读不到东西自然永远不相交），只测后者则无法证明它有判别力。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "scripts" / "check_reader_identity.py"


def _load():
    spec = importlib.util.spec_from_file_location("_i21_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load()


class TestEvaluate:
    def test_distinct_readers_pass(self):
        assert check.evaluate({"leader-cc"}, {"leader-codex"}) == []

    def test_shared_reader_is_caught(self):
        """核心断言：两侧同名必须变红。"""
        errors = check.evaluate({"leader-cc"}, {"leader-cc"})
        assert len(errors) == 1
        assert "共用 reader" in errors[0]

    def test_partial_overlap_is_caught(self):
        errors = check.evaluate({"leader-cc"}, {"leader-codex", "leader-cc"})
        # 既报共用，也报同侧多身份
        assert any("共用 reader" in e for e in errors)

    def test_empty_reader_is_caught(self):
        """空 reader 会让 hook 静默退出——装了等于没装，必须报。"""
        errors = check.evaluate({""}, {"leader-codex"})
        assert any("空的 reader" in e for e in errors)

    def test_multiple_readers_on_one_side_is_caught(self):
        errors = check.evaluate({"leader-cc", "leader-cc-2"}, {"leader-codex"})
        assert any("个不同 reader" in e for e in errors)

    @pytest.mark.parametrize("bad", ["leader cc", "leader/cc", "leader:cc", "a" * 101])
    def test_malformed_reader_is_caught(self, bad):
        errors = check.evaluate({bad}, {"leader-codex"})
        assert any("不是合法角色标识" in e for e in errors)

    def test_one_side_absent_is_not_a_violation(self):
        """适配器分批交付是既定节奏，缺席不算违规。"""
        assert check.evaluate({"leader-cc"}, set()) == []
        assert check.evaluate(set(), {"leader-codex"}) == []
        assert check.evaluate(set(), set()) == []


class TestAgainstRealRepo:
    def test_cc_side_actually_registers_a_reader(self):
        """防机检写错路径：若 CC 侧读出空集，上面的"不相交"就成了空洞的真。"""
        errors: list[str] = []
        cc = check._cc_readers(errors)
        assert errors == []
        assert cc == {"leader-cc"}, f"CC 侧 reader 实测为 {cc}"

    def test_real_repo_passes(self):
        assert check.main() == 0
