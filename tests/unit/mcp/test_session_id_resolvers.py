"""N1 会话身份接口位：解析器表按序试，CC 那条策略必须逐字保留。

本期只注册 CC 一个解析器（Codex 槽位留给后续 PR 填）。这里钉的是**接口契约**，
不是实现：

* CC 环境置位时，新入口与老函数 ``_cc_session_id()`` 的结果必须逐字相等——这个值
  会被当成 ``X-CC-Session-Id`` 发给服务端做归因域解析，差一个空格就绑到别的行上；
* 全落空必须是 None 而不是 ""，调用方靠这个区别判断"根本没有会话域"；
* 注册顺序可断言，免得后来者把贵的解析器插到 CC 前面（CC 只读两个环境变量，是唯一
  零成本判据）。
"""

from __future__ import annotations

import pytest

from aiteam.mcp import _base

SESSION_ID = "80d0cc5e-186a-4948-9e99-39ecfcf17730"
_ENV_NAMES = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


@pytest.fixture(autouse=True)
def _clear_session_env(monkeypatch):
    """真机 CC 会话跑测试时环境里本来就有这两个变量，先摘干净再逐例置位。"""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------------
# 注册面
# ------------------------------------------------------------------


def test_cc_resolver_is_registered_first():
    """CC 排首位，且本期只有它一个（Codex 槽位不注册）。"""
    assert isinstance(_base.SESSION_ID_RESOLVERS, tuple)
    assert _base.SESSION_ID_RESOLVERS[0] is _base._cc_session_id_resolver
    assert len(_base.SESSION_ID_RESOLVERS) == 1


def test_resolvers_take_an_optional_ctx():
    """签名契约：ctx 可省，用不上的解析器忽略即可。"""
    for resolver in _base.SESSION_ID_RESOLVERS:
        assert resolver() is None or isinstance(resolver(), str)
        assert resolver(object()) is None or isinstance(resolver(object()), str)


# ------------------------------------------------------------------
# 与老函数逐字相等
# ------------------------------------------------------------------


@pytest.mark.parametrize("env_name", _ENV_NAMES)
def test_matches_legacy_for_both_env_names(monkeypatch, env_name):
    """两个变量名任一置位，新旧两条路径给出同一个串。"""
    monkeypatch.setenv(env_name, SESSION_ID)

    assert _base.resolve_session_id() == SESSION_ID
    assert _base.resolve_session_id() == _base._cc_session_id()


def test_matches_legacy_including_the_strip(monkeypatch):
    """老函数会 strip，新入口不能少这一步（差一个换行就是另一个 id）。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", f"  {SESSION_ID}\n")

    assert _base._cc_session_id() == SESSION_ID
    assert _base.resolve_session_id() == _base._cc_session_id()


def test_primary_env_wins_over_fallback(monkeypatch):
    """两个都置位时的优先级与老函数一致。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "other-session")

    assert _base.resolve_session_id() == SESSION_ID
    assert _base.resolve_session_id() == _base._cc_session_id()


# ------------------------------------------------------------------
# 落空即 None
# ------------------------------------------------------------------


def test_unset_resolves_to_none():
    """没有任何 harness 认领 → None（老函数在这里返回的是 ""）。"""
    assert _base._cc_session_id() == ""
    assert _base.resolve_session_id() is None


def test_blank_env_resolves_to_none(monkeypatch):
    """只有空白字符也算没认领，不能把 " " 当会话 id 发出去。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "   ")

    assert _base.resolve_session_id() is None


# ------------------------------------------------------------------
# 分发语义
# ------------------------------------------------------------------


def test_first_non_empty_wins_and_stops(monkeypatch):
    """按序试，首个非空即返，后面的解析器不再被问。"""
    calls: list[str] = []

    def _empty(ctx=None):
        calls.append("empty")
        return None

    def _hit(ctx=None):
        calls.append("hit")
        return "codex-thread-42"

    def _never(ctx=None):  # pragma: no cover - 被前一个短路掉才是对的
        calls.append("never")
        return "wrong"

    monkeypatch.setattr(_base, "SESSION_ID_RESOLVERS", (_empty, _hit, _never))

    assert _base.resolve_session_id() == "codex-thread-42"
    assert calls == ["empty", "hit"]


def test_ctx_is_passed_through(monkeypatch):
    """ctx 原样透传——Codex 侧要靠它拿线程标识。"""
    seen: list[object] = []
    ctx = object()

    def _record(value=None):
        seen.append(value)
        return None

    monkeypatch.setattr(_base, "SESSION_ID_RESOLVERS", (_record,))

    assert _base.resolve_session_id(ctx) is None
    assert seen == [ctx]


def test_all_resolvers_empty_resolves_to_none(monkeypatch):
    """全落空显式降级，禁止拿 cwd 之类猜一个出来。"""
    monkeypatch.setattr(_base, "SESSION_ID_RESOLVERS", (lambda ctx=None: None,))

    assert _base.resolve_session_id() is None
