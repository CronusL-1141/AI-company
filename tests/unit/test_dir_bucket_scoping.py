"""目录指纹临时桶 — 纯函数 + hook 一致性 + 未注册简报极简化单元测试.

对应 2026-07-21 串线事故根治的产品层三处改动中的：
- aiteam.memory.scoping.dir_bucket_scope_id 纯函数语义；
- 两个 hook 未注册目录下把同一 cwd 编码成同一 X-Project-Dir 头（指纹权威唯一在
  API 侧，hook 只传 cwd——本测试钉死两 hook 传参一致，杜绝静默漂移）；
- session_bootstrap 未注册分支只输出注册引导 + 合法全局记忆，不灌工作台内容。
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

from aiteam.memory.scoping import DIR_BUCKET_PREFIX, dir_bucket_scope_id

# ============================================================
# dir_bucket_scope_id 纯函数
# ============================================================


def test_empty_cwd_returns_empty() -> None:
    assert dir_bucket_scope_id("") == ""
    assert dir_bucket_scope_id("   ") == ""


def test_prefix_and_length() -> None:
    bucket = dir_bucket_scope_id("/tmp/some-unregistered-dir")
    assert bucket.startswith(DIR_BUCKET_PREFIX)
    # "dir:" + 16 hex chars
    assert len(bucket) == len(DIR_BUCKET_PREFIX) + 16
    hexpart = bucket[len(DIR_BUCKET_PREFIX):]
    assert all(c in "0123456789abcdef" for c in hexpart)


def test_deterministic() -> None:
    p = "/tmp/aiteam-determinism"
    assert dir_bucket_scope_id(p) == dir_bucket_scope_id(p)


def test_idempotent_under_realpath() -> None:
    """传原始 cwd 与传其 realpath 结果一致（realpath 幂等，写读两侧不会因规范化分叉）."""
    p = "/tmp/aiteam-realpath-x"
    assert dir_bucket_scope_id(p) == dir_bucket_scope_id(os.path.realpath(p))


def test_different_dirs_different_buckets() -> None:
    assert dir_bucket_scope_id("/tmp/dir-one") != dir_bucket_scope_id("/tmp/dir-two")


# ============================================================
# 两个 hook 的 X-Project-Dir 编码一致性（钉死一致性）
# ============================================================


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _make_capturing_urlopen(captured: list):
    def _fake(req, timeout=None):  # noqa: ANN001, ANN202
        captured.append(req)
        return _FakeResp({"data": []})

    return _fake


def test_both_hooks_encode_same_cwd_identically(monkeypatch) -> None:
    """session_bootstrap 与 inject_subagent_context 对同一 cwd 发出相同 X-Project-Dir 头."""
    from aiteam.hooks import inject_subagent_context as inj
    from aiteam.hooks import session_bootstrap as sb

    cwd = "/tmp/aiteam-consistency-dir"
    captured: list = []
    monkeypatch.setattr(urllib.request, "urlopen", _make_capturing_urlopen(captured))

    # session_bootstrap 未注册分支：project_dir=cwd（无 project_id → 发 X-Project-Dir）
    sb._fetch_direction_memories(project_dir=cwd)
    # inject_subagent_context：X-Project-Dir 来自 CLAUDE_PROJECT_DIR or cwd
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", cwd)
    inj._fetch_direction_memories()

    assert len(captured) == 2
    # urllib 以 key.capitalize() 存头：X-Project-Dir → "X-project-dir"
    h_sb = captured[0].headers.get("X-project-dir")
    h_inj = captured[1].headers.get("X-project-dir")
    assert h_sb is not None
    assert h_sb == h_inj


def test_session_bootstrap_uses_project_id_when_registered(monkeypatch) -> None:
    """已注册项目走 X-Project-Id（不发 X-Project-Dir）——注册路径不受 dir 分支影响."""
    from aiteam.hooks import session_bootstrap as sb

    captured: list = []
    monkeypatch.setattr(urllib.request, "urlopen", _make_capturing_urlopen(captured))
    sb._fetch_direction_memories(project_id="proj-abc")

    assert len(captured) == 1
    assert captured[0].headers.get("X-project-id") == "proj-abc"
    assert captured[0].headers.get("X-project-dir") is None


# ============================================================
# 未注册简报极简化
# ============================================================

# 精确到完整简报里独有的段头/行，避免误伤注册引导正文里对功能的说明性提及
# （如"注册后可用任务墙/会议/报告"是合法引导文案，不是工作台段）。
_WORKBENCH_MARKERS = [
    "=== Leader核心规则",
    "任务墙Top5",
    "个活跃",  # 团队状态行 "团队: N个活跃, M个已完成"
    "=== 自动唤醒",
    "=== 可用Skills",
    "=== 可用Agent模板",
    "=== 进行中任务",
]


def test_unregistered_briefing_is_minimal(monkeypatch) -> None:
    """未注册简报含注册引导，不含团队列表/Leader规则/任务墙/唤醒/skills 等工作台内容."""
    from aiteam.hooks import session_bootstrap as sb

    monkeypatch.setattr(sb, "_fetch_direction_memories", lambda **kw: [])
    out = sb._build_unregistered_briefing("/tmp/aiteam-unreg", is_dismissed=False)

    # 含注册/忽略引导
    assert "project_create(" in out
    assert "dismiss_project_registration(" in out
    # 不含任何工作台段
    for marker in _WORKBENCH_MARKERS:
        assert marker not in out, f"极简简报不应含工作台段: {marker}"


def test_unregistered_briefing_dismissed_suppresses_prompt(monkeypatch) -> None:
    """已忽略注册的目录：不再展示注册引导，仍保持极简（不灌工作台）."""
    from aiteam.hooks import session_bootstrap as sb

    monkeypatch.setattr(sb, "_fetch_direction_memories", lambda **kw: [])
    out = sb._build_unregistered_briefing("/tmp/aiteam-unreg", is_dismissed=True)

    assert "project_create(" not in out
    for marker in _WORKBENCH_MARKERS:
        assert marker not in out


def test_unregistered_briefing_renders_global_memories(monkeypatch) -> None:
    """未注册简报会渲染取回的 global/dir 桶方向记忆（合法全局纪律照常继承）."""
    from aiteam.hooks import session_bootstrap as sb

    monkeypatch.setattr(
        sb,
        "_fetch_direction_memories",
        lambda **kw: [{"content": "所有输出使用中文", "kind": "constraint"}],
    )
    out = sb._build_unregistered_briefing("/tmp/aiteam-unreg", is_dismissed=False)
    assert "所有输出使用中文" in out


def test_build_briefing_routes_unregistered_to_minimal(monkeypatch) -> None:
    """_build_briefing 在未注册时路由到极简简报（不产出 Leader 工作台）."""
    from aiteam.hooks import session_bootstrap as sb

    monkeypatch.setattr(sb, "_api_get", lambda *a, **kw: None)
    monkeypatch.setattr(
        sb, "_check_project_registration", lambda *a, **kw: (False, False, {})
    )
    monkeypatch.setattr(sb, "_fetch_direction_memories", lambda **kw: [])

    out = sb._build_briefing()
    assert "极简简报" in out
    assert "Leader核心规则" not in out


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
