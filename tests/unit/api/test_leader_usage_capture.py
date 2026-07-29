"""Leader 主会话 token 用量采集测试（归因 v1 阶段 4）。

这份测试的骨架就是设计 §3.3 点名的三个陷阱 —— 每一个都是"代码看着能跑、数字却
是假的"那一类，所以它们各有一组针对性用例，而不是混在端到端用例里顺带覆盖：

* 陷阱① **快照覆写**：主会话 transcript 是累计文件，做成累加会随轮次成倍虚高。
  钉子 = 强制触发 10 次数值不变（设计 R4 的验收原话）+ 文件增长后取新的累计值。
* 陷阱② **节流**：Stop 每轮都触发，全量解析随会话线性变贵。
  钉子 = 节流窗内解析次数为 0（不是"少一点"，是 0）。
* 陷阱③ **合成行**：compact 留下的 ``model="<synthetic>"`` 行会污染 model。
  钉子 = 任何路径下 Leader 行的 model 都不会变成占位符。

see docs/token-attribution-v1-design.md §3.3 / §7 阶段 4
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from aiteam.api import leader_usage, session_probe
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.clock import utc_now
from aiteam.services import token_attribution

SYNTHETIC = session_probe.SYNTHETIC_MODEL


# ============================================================
# Fixtures / helpers
# ============================================================


def _assistant(req: str, *, model: str, inp: int, cache_c: int, cache_r: int, out: int) -> dict:
    return {
        "type": "assistant",
        "requestId": req,
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": cache_c,
                "cache_read_input_tokens": cache_r,
                "output_tokens": out,
            },
        },
    }


def _write_lines(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _append_lines(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _main_transcript(tmp_path: Path, name: str = "sess.jsonl") -> Path:
    """两次 API 调用的主会话 transcript：合计 in=3 / out=600 / cc=2000 / cr=90000。"""
    path = tmp_path / name
    _write_lines(
        path,
        [
            {"type": "user", "message": {"role": "user", "content": "go"}},
            _assistant("req_a", model="claude-opus-5", inp=1, cache_c=1000, cache_r=40000, out=5),
            # 同一 requestId 的流式后续行：output 是递增快照而非增量，必须只算最后一条
            _assistant("req_a", model="claude-opus-5", inp=1, cache_c=1000, cache_r=40000, out=100),
            _assistant("req_b", model="claude-opus-5", inp=2, cache_c=1000, cache_r=50000, out=500),
        ],
    )
    return path


EXPECTED = {
    "input_tokens": 3,
    "output_tokens": 600,
    "cache_creation_tokens": 2000,
    "cache_read_tokens": 90000,
}


def _touch(path: Path, *, seconds_ahead: float) -> None:
    """把 mtime 往前推 —— 模拟"这一轮又写进去了一些内容"。"""
    ts = path.stat().st_mtime + seconds_ahead
    os.utime(path, (ts, ts))


async def _make_leader(repo, session_id: str, *, name: str = "Leader"):
    agent = await repo.create_agent(
        team_id="team-leader",
        name=name,
        role="leader",
        source="hook",
        session_id=session_id,
    )
    await repo.update_agent(agent.id, status="busy")
    return agent


def _translator(repo) -> HookTranslator:
    return HookTranslator(repo=repo, event_bus=EventBus(repo=repo))


class _ParseCounter:
    """包一层真解析器，只为数"到底解析了几次"。"""

    def __init__(self) -> None:
        self.calls = 0
        self._real = token_attribution.parse_transcript_usage

    def __call__(self, path):
        self.calls += 1
        return self._real(path)


def _count_parses(monkeypatch) -> _ParseCounter:
    """把解析器换成计数版（monkeypatch 负责还原，断言失败也不会污染后续用例）。"""
    counter = _ParseCounter()
    monkeypatch.setattr(token_attribution, "parse_transcript_usage", counter)
    return counter


# ============================================================
# 陷阱② 的判据本身：节流决策是纯函数，逐分支钉死
# ============================================================


class TestShouldMeasure:
    def _meter(self) -> leader_usage.SessionUsageMeter:
        return leader_usage.SessionUsageMeter(
            min_interval_seconds=300, mtime_advance_seconds=600
        )

    def test_first_time_always_measures(self):
        meter = self._meter()
        now = utc_now()
        assert meter.should_measure("s1", mtime=now, now=now) is True

    def test_force_bypasses_every_gate(self):
        meter = self._meter()
        now = utc_now()
        meter._remember("s1", measured_at=now, mtime=now)
        # 窗口没到、文件也没动 —— 唯一还能通过的理由只有 force
        assert meter.should_measure("s1", mtime=now, now=now) is False
        assert meter.should_measure("s1", mtime=now, now=now, force=True) is True

    def test_unchanged_file_is_never_reparsed(self):
        """文件一个字节没动 -> 不测，哪怕窗口早就过了。

        重复解析同一份文件必然得到同一份快照（解析器是纯函数），这一趟纯属白花。
        """
        meter = self._meter()
        t0 = utc_now()
        meter._remember("s1", measured_at=t0, mtime=t0)
        assert meter.should_measure("s1", mtime=t0, now=t0 + timedelta(hours=3)) is False

    def test_inside_window_with_fresh_bytes_is_skipped(self):
        meter = self._meter()
        t0 = utc_now()
        meter._remember("s1", measured_at=t0, mtime=t0)
        assert (
            meter.should_measure(
                "s1", mtime=t0 + timedelta(seconds=30), now=t0 + timedelta(seconds=30)
            )
            is False
        )

    def test_past_min_interval_measures(self):
        meter = self._meter()
        t0 = utc_now()
        meter._remember("s1", measured_at=t0, mtime=t0)
        assert (
            meter.should_measure(
                "s1", mtime=t0 + timedelta(seconds=301), now=t0 + timedelta(seconds=301)
            )
            is True
        )

    def test_long_turn_escapes_the_window_via_mtime_advance(self):
        """窗口还没到，但这一轮已经写了 11 分钟 —— 提前测一次比死等窗口更贴近真实。"""
        meter = self._meter()
        t0 = utc_now()
        meter._remember("s1", measured_at=t0, mtime=t0)
        assert (
            meter.should_measure(
                "s1", mtime=t0 + timedelta(seconds=660), now=t0 + timedelta(seconds=120)
            )
            is True
        )


# ============================================================
# 定位：payload 优先，slug 反查兜底
# ============================================================


class TestLocateMainTranscript:
    def test_payload_path_wins_when_it_exists(self, tmp_path: Path):
        t = _main_transcript(tmp_path)
        assert leader_usage.locate_main_transcript(str(t)) == t

    def test_falls_back_to_slug_lookup_when_payload_path_unusable(
        self, tmp_path: Path, monkeypatch
    ):
        """hook 侧会截断超长路径、超限时还会整条剥离 —— 两种情况都得能兜住。"""
        cwd = "/Users/x/Desktop/proj"
        slug = session_probe.project_slug(cwd)
        projects = tmp_path / "projects"
        (projects / slug).mkdir(parents=True)
        real = _main_transcript(projects / slug, name="sid-1234.jsonl")
        monkeypatch.setattr(leader_usage, "_projects_dir", lambda: projects)

        truncated = "/some/very/long/path...(truncated)"
        assert (
            leader_usage.locate_main_transcript(truncated, cwd=cwd, session_id="sid-1234") == real
        )
        assert leader_usage.locate_main_transcript("", cwd=cwd, session_id="sid-1234") == real

    def test_returns_none_when_nothing_resolves(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(leader_usage, "_projects_dir", lambda: tmp_path)
        assert leader_usage.locate_main_transcript("", cwd="/nope", session_id="sid") is None


# ============================================================
# 陷阱③ 合成行过滤
# ============================================================


class TestSyntheticFilter:
    def test_real_model_passes_through(self, tmp_path: Path):
        t = _main_transcript(tmp_path)
        assert leader_usage._resolve_model("claude-opus-5", t) == "claude-opus-5"

    def test_synthetic_placeholder_is_replaced_by_the_last_real_model(self, tmp_path: Path):
        t = _main_transcript(tmp_path)
        _append_lines(
            t,
            [_assistant("req_c", model=SYNTHETIC, inp=0, cache_c=0, cache_r=0, out=0)],
        )
        assert leader_usage._resolve_model(SYNTHETIC, t) == "claude-opus-5"

    def test_capture_never_reports_the_placeholder_as_model(self, tmp_path: Path):
        """compact 合成行在主会话里是常态（实测 100% 复现），采集侧必须自己扛住。

        刻意不断言四层数值：合成行的 usage 实测全为 0，阶段 0 修好过滤前后总量都
        一样，但断言数值会把这份测试绑死在某一侧的实现上。这里只钉 model。
        """
        t = _main_transcript(tmp_path)
        _append_lines(
            t,
            [_assistant("req_syn", model=SYNTHETIC, inp=7, cache_c=7, cache_r=7, out=7)],
        )
        meter = leader_usage.SessionUsageMeter()
        snap = meter.capture("s1", t, force=True)
        assert snap is not None
        assert snap.model != SYNTHETIC
        assert snap.model == "claude-opus-5"

    @pytest.mark.asyncio
    async def test_leader_row_never_gets_the_placeholder_model(
        self, db_repository, tmp_path: Path
    ):
        repo = db_repository
        leader = await _make_leader(repo, "sess-syn")
        t = _main_transcript(tmp_path)
        _append_lines(
            t,
            [_assistant("req_syn", model=SYNTHETIC, inp=0, cache_c=0, cache_r=0, out=0)],
        )
        await _translator(repo).handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sess-syn",
                "transcript_path": str(t),
                "trigger": "manual",
            }
        )
        fetched = await repo.get_agent(leader.id)
        assert fetched.model == "claude-opus-5"
        for field, value in EXPECTED.items():
            assert getattr(fetched, field) == value


# ============================================================
# 陷阱① 快照覆写（绝不累加）
# ============================================================


class TestSnapshotOverwrite:
    def test_agent_updates_are_absolute_values(self, tmp_path: Path):
        t = _main_transcript(tmp_path)
        snap = leader_usage.SessionUsageMeter().capture("s1", t, force=True)
        assert snap is not None
        updates = snap.as_agent_updates()
        for field, value in EXPECTED.items():
            assert updates[field] == value
        assert updates["transcript_path"] == str(t)
        assert updates["tokens_measured_at"] is not None
        # 四层分列、无合计 —— "总量"实际上等于"缓存读取量"，单独呈现会失真（§1.2）
        assert "total_tokens" not in updates

    @pytest.mark.asyncio
    async def test_ten_forced_captures_do_not_accumulate(
        self, db_repository, tmp_path: Path, monkeypatch
    ):
        """设计 R4 的验收原话：重复触发 10 次数值不变。

        走 PostCompact（强制路径），确保每一次都真的解析了一遍 —— 如果靠节流把后
        九次挡掉，这条测试就退化成"什么都没测"。
        """
        repo = db_repository
        leader = await _make_leader(repo, "sess-ten")
        t = _main_transcript(tmp_path)
        translator = _translator(repo)
        counter = _count_parses(monkeypatch)
        for _ in range(10):
            await translator.handle_event(
                {
                    "hook_event_name": "PostCompact",
                    "session_id": "sess-ten",
                    "transcript_path": str(t),
                    "trigger": "manual",
                }
            )
            fetched = await repo.get_agent(leader.id)
            for field, value in EXPECTED.items():
                assert getattr(fetched, field) == value, f"{field} drifted — 累加了？"
        assert counter.calls == 10  # 10 次全都真解析了，不是被节流糊弄过去的

    @pytest.mark.asyncio
    async def test_growing_transcript_yields_the_new_cumulative_total(
        self, db_repository, tmp_path: Path
    ):
        """文件长大后，落库的是**新的累计值**，不是新旧两次之和。"""
        repo = db_repository
        leader = await _make_leader(repo, "sess-grow")
        t = _main_transcript(tmp_path)
        translator = _translator(repo)
        payload = {
            "hook_event_name": "PostCompact",
            "session_id": "sess-grow",
            "transcript_path": str(t),
        }
        await translator.handle_event(payload)
        _append_lines(
            t,
            [_assistant("req_c", model="claude-opus-5", inp=4, cache_c=10, cache_r=20, out=30)],
        )
        await translator.handle_event(payload)
        fetched = await repo.get_agent(leader.id)
        assert fetched.input_tokens == EXPECTED["input_tokens"] + 4
        assert fetched.output_tokens == EXPECTED["output_tokens"] + 30
        assert fetched.cache_creation_tokens == EXPECTED["cache_creation_tokens"] + 10
        assert fetched.cache_read_tokens == EXPECTED["cache_read_tokens"] + 20


# ============================================================
# 陷阱② 节流（端到端：Stop 路径）
# ============================================================


class TestStopThrottle:
    @pytest.mark.asyncio
    async def test_inside_the_window_stop_parses_zero_times(
        self, db_repository, tmp_path: Path, monkeypatch
    ):
        repo = db_repository
        leader = await _make_leader(repo, "sess-thr")
        t = _main_transcript(tmp_path)
        translator = _translator(repo)
        payload = {
            "hook_event_name": "Stop",
            "session_id": "sess-thr",
            "transcript_path": str(t),
        }

        first = await translator.handle_event(payload)
        assert first["leader_usage"] is not None  # 首测建立基线
        after_first = await repo.get_agent(leader.id)

        counter = _count_parses(monkeypatch)
        for _ in range(5):
            # 每轮都让文件"又写了一点"，否则不改 mtime 也会被挡下，测不出窗口本身
            _touch(t, seconds_ahead=20)
            result = await translator.handle_event(payload)
            assert result["leader_usage"] is None

        assert counter.calls == 0  # 节流窗内一次都没解析
        again = await repo.get_agent(leader.id)
        assert again.tokens_measured_at == after_first.tokens_measured_at

    @pytest.mark.asyncio
    async def test_forced_events_bypass_the_window(
        self, db_repository, tmp_path: Path, monkeypatch
    ):
        repo = db_repository
        await _make_leader(repo, "sess-force")
        t = _main_transcript(tmp_path)
        translator = _translator(repo)
        base = {"session_id": "sess-force", "transcript_path": str(t)}

        await translator.handle_event({**base, "hook_event_name": "Stop"})
        counter = _count_parses(monkeypatch)
        _touch(t, seconds_ahead=20)
        assert (
            await translator.handle_event({**base, "hook_event_name": "Stop"})
        )["leader_usage"] is None
        compacted = await translator.handle_event({**base, "hook_event_name": "PostCompact"})
        ended = await translator.handle_event({**base, "hook_event_name": "SessionEnd"})
        assert compacted["leader_usage"] is not None
        assert ended["leader_usage"] is not None
        assert counter.calls == 2  # 被节流挡下的那次没解析，两次强制定格各解析一次


# ============================================================
# 挂载点与边界
# ============================================================


class TestMountPoints:
    @pytest.mark.asyncio
    async def test_session_start_backfills_five_columns_and_path(
        self, db_repository, tmp_path: Path
    ):
        """Leader 行此前 0/117 有 transcript_path、token 五列全空 —— 首测就把两样都补上。"""
        repo = db_repository
        leader = await _make_leader(repo, "sess-start")
        t = _main_transcript(tmp_path)
        await _translator(repo).handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "sess-start",
                "transcript_path": str(t),
                "cwd": str(tmp_path),
            }
        )
        fetched = await repo.get_agent(leader.id)
        for field, value in EXPECTED.items():
            assert getattr(fetched, field) == value
        assert fetched.tokens_measured_at is not None
        assert fetched.transcript_path == str(t)

    @pytest.mark.asyncio
    async def test_post_compact_locates_transcript_without_a_payload_path(
        self, db_repository, tmp_path: Path, monkeypatch
    ):
        """PostCompact 的载荷最容易被裁剪 —— 路径没了也要能按 slug 反查定位。"""
        repo = db_repository
        leader = await _make_leader(repo, "sid-9999")
        cwd = "/Users/x/Desktop/proj"
        projects = tmp_path / "projects"
        (projects / session_probe.project_slug(cwd)).mkdir(parents=True)
        _main_transcript(projects / session_probe.project_slug(cwd), name="sid-9999.jsonl")
        monkeypatch.setattr(leader_usage, "_projects_dir", lambda: projects)

        await _translator(repo).handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sid-9999",
                "cwd": cwd,
                "compact_summary_chars": 12000,
            }
        )
        fetched = await repo.get_agent(leader.id)
        assert fetched.output_tokens == EXPECTED["output_tokens"]

    @pytest.mark.asyncio
    async def test_missing_transcript_writes_nothing(self, db_repository, tmp_path: Path):
        """no-data ≠ zero：文件不在就一列都不写，绝不落 0。"""
        repo = db_repository
        leader = await _make_leader(repo, "sess-gone")
        await _translator(repo).handle_event(
            {
                "hook_event_name": "SessionEnd",
                "session_id": "sess-gone",
                "transcript_path": str(tmp_path / "not-here.jsonl"),
            }
        )
        fetched = await repo.get_agent(leader.id)
        assert fetched.input_tokens is None
        assert fetched.output_tokens is None
        assert fetched.cache_read_tokens is None
        assert fetched.tokens_measured_at is None

    @pytest.mark.asyncio
    async def test_sub_agent_rows_are_never_touched_by_the_leader_path(
        self, db_repository, tmp_path: Path
    ):
        """会话里没有 Leader 行时，用量绝不能落到同会话的子 agent 头上。

        呈现层要把 Leader 与子 agent 分列（量级差两个数量级），采集层先保证这两类
        行不会互相串味。
        """
        repo = db_repository
        worker = await repo.create_agent(
            team_id="team-x",
            name="researcher",
            role="researcher",
            source="hook",
            session_id="sess-noleader",
        )
        t = _main_transcript(tmp_path)
        await _translator(repo).handle_event(
            {
                "hook_event_name": "Stop",
                "session_id": "sess-noleader",
                "transcript_path": str(t),
            }
        )
        fetched = await repo.get_agent(worker.id)
        assert fetched.input_tokens is None
        assert fetched.tokens_measured_at is None
