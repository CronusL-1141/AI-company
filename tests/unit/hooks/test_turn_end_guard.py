"""Unit tests for turn_end_guard Stop hook (唤醒体系 v2 §8).

decide() 纯函数测 7 分支；main() 用 mock stdin + tmp 状态目录 + mock 端点查询
测 Stop/user-prompt 两模式与 fail-open。对齐 batch0-contract-tests.md 测试④。
"""

from __future__ import annotations

import io
import json
import time

import pytest

import aiteam.hooks.turn_end_guard as g


@pytest.fixture(autouse=True)
def _isolate_arm_hint_flag(tmp_path_factory, monkeypatch):
    """把待命守卫开关钉到 tmp，任何用例都不得读到真机的 ~/.claude/…/arm-hint.off。

    没有这道隔离，本文件的判停用例会随「维护者本人有没有 /os-watcher off」变色：
    CI 上没这个文件 -> 全绿，开了开关的机器上 -> 红。绿在 CI、红在本机，是本仓最
    防的那种假绿。要测静默的用例自己 touch 这个 tmp 路径显式开启。
    """
    monkeypatch.setattr(
        g, "_ARM_HINT_OFF_FLAG",
        tmp_path_factory.mktemp("armhint") / "arm-hint.off",
    )


# ---- decide() 7 分支（纯函数）--------------------------------------------
def test_decide_stop_hook_active_allows():
    a, b, _ = g.decide(stop_hook_active=True, manual_active=False,
                        stop_keyword_hit=False, work_in_flight=True,
                        watcher_armed=False, block_count=0)
    assert a == "allow" and b == "stop_hook_active"


def test_decide_manual_allows_even_in_danger():
    a, b, _ = g.decide(stop_hook_active=False, manual_active=True,
                        stop_keyword_hit=False, work_in_flight=True,
                        watcher_armed=False, block_count=0)
    assert a == "allow" and b == "manual"


def test_decide_stop_keyword_allows_even_in_danger():
    a, b, _ = g.decide(stop_hook_active=False, manual_active=False,
                        stop_keyword_hit=True, work_in_flight=True,
                        watcher_armed=False, block_count=0)
    assert a == "allow" and b == "stop_keyword"


def test_decide_no_work_allows():
    a, b, _ = g.decide(stop_hook_active=False, manual_active=False,
                        stop_keyword_hit=False, work_in_flight=False,
                        watcher_armed=False, block_count=0)
    assert a == "allow" and b == "safe"


def test_decide_watcher_armed_allows():
    a, b, _ = g.decide(stop_hook_active=False, manual_active=False,
                        stop_keyword_hit=False, work_in_flight=True,
                        watcher_armed=True, block_count=0)
    assert a == "allow" and b == "watcher_armed"


def test_decide_block_cap_allows():
    a, b, _ = g.decide(stop_hook_active=False, manual_active=False,
                        stop_keyword_hit=False, work_in_flight=True,
                        watcher_armed=False, block_count=g.MAX_BLOCKS)
    assert a == "allow" and b == "block_cap"


def test_decide_danger_zone_blocks():
    a, b, reason = g.decide(stop_hook_active=False, manual_active=False,
                            stop_keyword_hit=False, work_in_flight=True,
                            watcher_armed=False, block_count=0)
    assert a == "block" and b == "danger_zone"
    assert "watcher" in reason


# ---- 停止关键词正则 --------------------------------------------------------
@pytest.mark.parametrize("text", ["先停一下", "收工吧", "ok please stop", "hold on", "暂停"])
def test_stop_keywords_match(text):
    assert g.STOP_KEYWORDS.search(text)


@pytest.mark.parametrize("text", ["继续推进", "keep going", "下一步做什么"])
def test_stop_keywords_no_false_positive(text):
    assert not g.STOP_KEYWORDS.search(text)


# ---- main() 集成 ----------------------------------------------------------
def _run_main(payload, monkeypatch, argv=None):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr("sys.argv", argv or ["turn_end_guard.py"])
    code = 0
    try:
        g.main()
    except SystemExit as e:
        code = e.code if e.code is not None else 0
    return code


@pytest.fixture()
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "_WAKE_STATE_DIR", tmp_path)
    return tmp_path


def test_main_blocks_in_danger_zone(tmp_state, monkeypatch, capsys):
    monkeypatch.setattr(g, "_query_actionable", lambda sid, tid: {"busy_agents": 2, "live_runs": 0})
    monkeypatch.setattr(g, "_last_user_text", lambda p: "继续推进")
    code = _run_main({"session_id": "s1", "stop_hook_active": False, "transcript_path": ""}, monkeypatch)
    out = capsys.readouterr().out
    assert code == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"
    # 计数已落盘
    state = json.loads((tmp_state / "s1.json").read_text())
    assert state["block_count"] == 1


def test_main_allows_when_no_work(tmp_state, monkeypatch, capsys):
    monkeypatch.setattr(g, "_query_actionable", lambda sid, tid: {"busy_agents": 0, "live_runs": 0})
    monkeypatch.setattr(g, "_last_user_text", lambda p: "")
    code = _run_main({"session_id": "s1", "stop_hook_active": False}, monkeypatch)
    assert code == 0
    assert capsys.readouterr().out.strip() == ""  # 无 block 输出


def test_main_stop_hook_active_short_circuits(tmp_state, monkeypatch, capsys):
    # 若 stop_hook_active，绝不查端点也绝不 block
    def _boom(sid, tid):
        raise AssertionError("endpoint must not be queried")
    monkeypatch.setattr(g, "_query_actionable", _boom)
    code = _run_main({"session_id": "s1", "stop_hook_active": True}, monkeypatch)
    assert code == 0
    assert capsys.readouterr().out.strip() == ""


def test_main_stop_keyword_allows_without_endpoint(tmp_state, monkeypatch, capsys):
    def _boom(sid, tid):
        raise AssertionError("keyword exemption must short-circuit before endpoint")
    monkeypatch.setattr(g, "_query_actionable", _boom)
    monkeypatch.setattr(g, "_last_user_text", lambda p: "好了先停")
    code = _run_main({"session_id": "s1", "stop_hook_active": False, "transcript_path": "x"}, monkeypatch)
    assert code == 0
    assert capsys.readouterr().out.strip() == ""


def test_main_watcher_armed_allows(tmp_state, monkeypatch, capsys):
    # 武装文件在有效期 → 即便有活也放行
    (tmp_state / "s1.armed").write_text(str(time.time() + 60))
    monkeypatch.setattr(g, "_query_actionable", lambda sid, tid: {"busy_agents": 5, "live_runs": 1})
    monkeypatch.setattr(g, "_last_user_text", lambda p: "继续")
    code = _run_main({"session_id": "s1", "stop_hook_active": False, "transcript_path": ""}, monkeypatch)
    assert code == 0
    assert capsys.readouterr().out.strip() == ""


def test_main_block_cap_releases(tmp_state, monkeypatch, capsys):
    (tmp_state / "s1.json").write_text(json.dumps({"block_count": g.MAX_BLOCKS, "last_block_at": time.time()}))
    monkeypatch.setattr(g, "_query_actionable", lambda sid, tid: {"busy_agents": 1, "live_runs": 0})
    monkeypatch.setattr(g, "_last_user_text", lambda p: "继续")
    code = _run_main({"session_id": "s1", "stop_hook_active": False, "transcript_path": ""}, monkeypatch)
    assert code == 0
    assert capsys.readouterr().out.strip() == ""  # 达上限，放行不再 block


def test_main_user_prompt_writes_manual_marker(tmp_state, monkeypatch):
    code = _run_main({"session_id": "s1"}, monkeypatch, argv=["turn_end_guard.py", "user-prompt"])
    assert code == 0
    state = json.loads((tmp_state / "s1.json").read_text())
    assert state["manual_until"] > time.time()
    assert state["block_count"] == 0


def test_main_manual_marker_allows_stop(tmp_state, monkeypatch, capsys):
    (tmp_state / "s1.json").write_text(json.dumps({"manual_until": time.time() + 300}))

    def _boom(sid, tid):
        raise AssertionError("manual exemption must short-circuit before endpoint")
    monkeypatch.setattr(g, "_query_actionable", _boom)
    monkeypatch.setattr(g, "_last_user_text", lambda p: "继续推进")
    code = _run_main({"session_id": "s1", "stop_hook_active": False, "transcript_path": ""}, monkeypatch)
    assert code == 0
    assert capsys.readouterr().out.strip() == ""


def test_main_fail_open_on_bad_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json{{{"))
    monkeypatch.setattr("sys.argv", ["turn_end_guard.py"])
    code = 0
    try:
        g.main()
    except SystemExit as e:
        code = e.code if e.code is not None else 0
    assert code == 0


# ── 待命提醒（2026-09-08）──────────────────────────────────────────────
#
# 这个守卫的代码和测试早就在，却一直**零注册面**——本该拦住"忘记武装 watcher"的
# 东西自己没上岗，于是 Leader 漏武装了两次，都是缔造者发现的。注册它的同时补一条
# 更弱但覆盖面更广的提醒：Stop 分支只在"有活在飞"时才拦，而消息是随时来的，空闲
# 时没武装照样收不到信。
#
# 提醒放在 UserPromptSubmit 而不是 Stop：Stop 的 allow 分支没有能进模型上下文的
# 输出通道（stderr 不进），而 UserPromptSubmit 的 stdout 会被注入。


class TestStandbyHint:
    def test_unarmed_session_gets_a_hint(self, tmp_path, monkeypatch, capsys):
        """未武装时提醒一句，且提醒里要带得起手的命令。"""
        monkeypatch.setattr(g, "_WAKE_STATE_DIR", tmp_path)
        with pytest.raises(SystemExit):
            g._handle_user_prompt({"session_id": "sess-unarmed"})
        out = capsys.readouterr().out
        assert "watcher 未武装" in out
        assert "os-watch.sh" in out

    def test_armed_session_stays_quiet(self, tmp_path, monkeypatch, capsys):
        """已武装就一个字都不说——每轮都刷一行等于没提醒。"""
        monkeypatch.setattr(g, "_WAKE_STATE_DIR", tmp_path)
        armed = tmp_path / "sess-armed.armed"
        armed.write_text(str(time.time() + 600))
        with pytest.raises(SystemExit):
            g._handle_user_prompt({"session_id": "sess-armed"})
        assert capsys.readouterr().out == ""

    def test_expired_armed_marker_counts_as_unarmed(self, tmp_path, monkeypatch, capsys):
        """心跳过期即视为未武装——watcher 死了标记还留着的情况必须被看穿。"""
        monkeypatch.setattr(g, "_WAKE_STATE_DIR", tmp_path)
        stale = tmp_path / "sess-stale.armed"
        stale.write_text(str(time.time() - 60))
        with pytest.raises(SystemExit):
            g._handle_user_prompt({"session_id": "sess-stale"})
        assert "watcher 未武装" in capsys.readouterr().out

    def test_hint_never_blocks(self, tmp_path, monkeypatch, capsys):
        """提醒绝不能变成拦截：输出里不得出现 decision 字样。"""
        monkeypatch.setattr(g, "_WAKE_STATE_DIR", tmp_path)
        with pytest.raises(SystemExit):
            g._handle_user_prompt({"session_id": "sess-x"})
        assert "decision" not in capsys.readouterr().out


# ---- 待命守卫开关（/os-watcher）-------------------------------------------
# 一个开关管两件事：每轮提醒 + 收工拦截。合并是 2026-09-17 用户裁定。
# 这里钉死三件事：静默要真的放行、放行必须单列分支可审计、开关故障要回到有保护的一侧。


class TestStandbyGuardMute:
    def test_muted_releases_the_danger_zone_block(self):
        """静默后本该 block 的那一格必须放行——只关嘴不关手等于没关。"""
        action, branch, _ = g.decide(
            stop_hook_active=False, manual_active=False, stop_keyword_hit=False,
            work_in_flight=True, watcher_armed=False, block_count=0, hint_muted=True)
        assert action == "allow"
        assert branch == "hint_muted"

    def test_mute_is_not_the_default(self):
        """不传 hint_muted 时必须维持原行为：危险区照拦。"""
        action, branch, _ = g.decide(
            stop_hook_active=False, manual_active=False, stop_keyword_hit=False,
            work_in_flight=True, watcher_armed=False, block_count=0)
        assert (action, branch) == ("block", "danger_zone")

    @pytest.mark.parametrize("kwargs,expected_branch", [
        ({"stop_hook_active": True}, "stop_hook_active"),
        ({"manual_active": True}, "manual"),
        ({"stop_keyword_hit": True}, "stop_keyword"),
        ({"work_in_flight": False}, "safe"),
        ({"watcher_armed": True}, "watcher_armed"),
        ({"block_count": g.MAX_BLOCKS}, "block_cap"),
    ])
    def test_mute_never_masks_a_more_specific_allow(self, kwargs, expected_branch):
        """静默只接管真正会拦的那一格。别的放行各有更准的理由，不能被它盖成 hint_muted
        ——否则日志里「没活在飞」和「用户关了守卫」长得一样，正是本仓要防的形态。"""
        base = dict(stop_hook_active=False, manual_active=False, stop_keyword_hit=False,
                    work_in_flight=True, watcher_armed=False, block_count=0)
        action, branch, _ = g.decide(**{**base, **kwargs}, hint_muted=True)
        assert action == "allow"
        assert branch == expected_branch

    def test_muted_session_gets_no_standby_hint(self, tmp_path, monkeypatch, capsys):
        """静默时每轮那句提醒也要闭嘴（同一个开关的另一半）。"""
        monkeypatch.setattr(g, "_WAKE_STATE_DIR", tmp_path)
        monkeypatch.setattr(g, "_ARM_HINT_OFF_FLAG", tmp_path / "arm-hint.off")
        (tmp_path / "arm-hint.off").touch()
        with pytest.raises(SystemExit):
            g._handle_user_prompt({"session_id": "sess-muted"})
        assert capsys.readouterr().out == ""

    def test_main_stop_allows_when_muted(self, tmp_state, monkeypatch, capsys):
        """端到端：有活在飞 + 未武装 + 已静默 -> 不得输出 decision:block。"""
        monkeypatch.setattr(g, "_query_actionable",
                            lambda sid, tid: {"busy_agents": 2, "live_runs": 0})
        monkeypatch.setattr(g, "_last_user_text", lambda p: "继续推进")
        monkeypatch.setattr(g, "_ARM_HINT_OFF_FLAG", tmp_state / "arm-hint.off")
        (tmp_state / "arm-hint.off").touch()
        code = _run_main({"session_id": "s-mute", "stop_hook_active": False,
                          "transcript_path": ""}, monkeypatch)
        assert code == 0
        assert capsys.readouterr().out.strip() == ""
        assert not (tmp_state / "s-mute.json").exists() or \
            json.loads((tmp_state / "s-mute.json").read_text()).get("block_count", 0) == 0

    def test_unreadable_flag_falls_back_to_protected(self, monkeypatch):
        """开关读不出来时当作"没关"——守卫故障必须倒向有保护的一侧，不能默默放行。"""
        class _Boom:
            def exists(self):
                raise OSError("permission denied")
        monkeypatch.setattr(g, "_ARM_HINT_OFF_FLAG", _Boom())
        assert g._hint_muted() is False
