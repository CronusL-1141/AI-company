"""Leader 主会话采集的**失败可观测性** —— 2026-08-03 排查事故立的规。

事故实录:三个活会话的 Leader 行 ``tokens_measured_at`` 长期为 NULL,其中一个
会话当日刚发生过 ``PostCompact``(hook 成功返回、``session.compact_completed``
事件也落库了),按设计那一趟是 ``force=True`` 强制定格,必须写库。排查花了两小时
仍**无法判定断在哪一环**,原因不是链条复杂,而是它整条不可证伪:

``_capture_leader_usage`` 把五种截然不同的结局全部塌缩成同一个 ``None`` ——
没有 session_id / 找不到 Leader 行 / 定位不到 transcript / 被节流挡下 /
中途抛异常;唯一的异常分支还是 ``logger.debug`` + ``except BLE001``,而生产
从不开 DEBUG。于是"采集跑了但没数据"和"采集抛异常了"在库里、日志里、回执里
**三处都长得一模一样**。

所以这份测试钉的不是"采集能成功"(那由 test_leader_usage_capture.py 负责),
而是"采集失败时能被看见":

1. 强制定格失手 -> WARNING 级日志,带上具体原因(不是 DEBUG,不是静默);
2. 抛异常 -> 落一条事件,事后一条 SQL 能查到(日志会随进程重启被截断,事件不会);
3. 每一种跳过都有**自己的名字**回执里带出来,不再是一个万能的 None。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from aiteam.api import leader_usage, session_probe
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator

CAPTURE_FAILED_EVENT = "leader.usage_capture_failed"


def _assistant(req: str, *, inp: int, out: int) -> dict:
    return {
        "type": "assistant",
        "requestId": req,
        "message": {
            "role": "assistant",
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": out,
            },
        },
    }


def _transcript(tmp_path: Path, name: str = "sess.jsonl") -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps(_assistant("req_a", inp=11, out=22)) + "\n",
        encoding="utf-8",
    )
    return path


async def _make_leader(repo, session_id: str):
    agent = await repo.create_agent(
        team_id="team-leader",
        name="Leader",
        role="leader",
        source="hook",
        session_id=session_id,
    )
    await repo.update_agent(agent.id, status="busy")
    return agent


def _translator(repo) -> HookTranslator:
    return HookTranslator(repo=repo, event_bus=EventBus(repo=repo))


# ============================================================
# 1. 强制定格失手必须是 WARNING,且说得出原因
# ============================================================


class TestForcedMissIsLoud:
    @pytest.mark.asyncio
    async def test_post_compact_that_cannot_locate_the_transcript_warns(
        self, db_repository, tmp_path: Path, caplog
    ):
        """PostCompact 定位不到 transcript = 这一刻的水位永久丢失,不该只留个 DEBUG。"""
        repo = db_repository
        await _make_leader(repo, "sess-miss")
        with caplog.at_level(logging.WARNING, logger="aiteam.api.hook_translator"):
            await _translator(repo).handle_event(
                {
                    "hook_event_name": "PostCompact",
                    "session_id": "sess-miss",
                    "transcript_path": str(tmp_path / "gone.jsonl"),
                    "trigger": "manual",
                }
            )
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "强制定格没落到库,日志里却一个字都没有"
        blob = " ".join(r.getMessage() for r in warnings)
        assert "no-transcript" in blob, f"原因没写进日志: {blob}"

    @pytest.mark.asyncio
    async def test_throttled_stop_stays_quiet(self, db_repository, tmp_path: Path, caplog):
        """节流是**正常**结局 —— 不许因为上一条就把每轮 Stop 也喊成 WARNING。"""
        repo = db_repository
        await _make_leader(repo, "sess-quiet")
        t = _transcript(tmp_path)
        translator = _translator(repo)
        payload = {
            "hook_event_name": "Stop",
            "session_id": "sess-quiet",
            "transcript_path": str(t),
        }
        await translator.handle_event(payload)  # 首测建立基线
        with caplog.at_level(logging.WARNING, logger="aiteam.api.hook_translator"):
            await translator.handle_event(payload)  # 窗口内,被节流
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ============================================================
# 2. 抛异常必须留下**耐久**痕迹(日志会被截断,事件不会)
# ============================================================


class TestFailureLeavesADurableTrace:
    @pytest.mark.asyncio
    async def test_write_failure_emits_a_queryable_event(self, db_repository, tmp_path: Path):
        repo = db_repository
        await _make_leader(repo, "sess-boom")
        t = _transcript(tmp_path)
        translator = _translator(repo)

        async def boom(*_args, **_kwargs):
            msg = "database is locked"
            raise RuntimeError(msg)

        repo.update_agent = boom  # type: ignore[method-assign]
        result = await translator.handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sess-boom",
                "transcript_path": str(t),
                "trigger": "manual",
            }
        )
        # 主流程绝不被采集拖垮 —— 回执照常返回
        assert result["status"] == "recorded"

        events = await repo.list_events(limit=200)
        failures = [e for e in events if e.type == CAPTURE_FAILED_EVENT]
        assert failures, "采集抛异常却没留下任何可查痕迹"
        data = failures[0].data
        assert data["session_id"] == "sess-boom"
        assert data["forced"] is True
        assert "RuntimeError" in data["reason"]

    @pytest.mark.asyncio
    async def test_ordinary_throttle_does_not_pollute_the_events_table(
        self, db_repository, tmp_path: Path
    ):
        """本批刚把 events 写入砍掉四成 —— 失败事件只许在真失败时出现。"""
        repo = db_repository
        await _make_leader(repo, "sess-noevent")
        t = _transcript(tmp_path)
        translator = _translator(repo)
        payload = {
            "hook_event_name": "Stop",
            "session_id": "sess-noevent",
            "transcript_path": str(t),
        }
        for _ in range(5):
            await translator.handle_event(payload)
        events = await repo.list_events(limit=200)
        assert not [e for e in events if e.type == CAPTURE_FAILED_EVENT]


# ============================================================
# 3. 每种跳过都有自己的名字,回执带得出来
# ============================================================


class TestSkipReasonsAreNamed:
    @pytest.mark.asyncio
    async def test_success_reports_no_skip_reason(self, db_repository, tmp_path: Path):
        repo = db_repository
        await _make_leader(repo, "sess-ok")
        t = _transcript(tmp_path)
        result = await _translator(repo).handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sess-ok",
                "transcript_path": str(t),
                "trigger": "manual",
            }
        )
        assert result["leader_usage"] is not None
        assert result["leader_usage_skip"] is None

    @pytest.mark.asyncio
    async def test_no_leader_row_is_named(self, db_repository, tmp_path: Path):
        repo = db_repository
        t = _transcript(tmp_path)
        result = await _translator(repo).handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sess-nobody",
                "transcript_path": str(t),
                "trigger": "manual",
            }
        )
        assert result["leader_usage"] is None
        assert result["leader_usage_skip"] == "no-leader-row"

    @pytest.mark.asyncio
    async def test_missing_transcript_is_named(self, db_repository, tmp_path: Path):
        repo = db_repository
        await _make_leader(repo, "sess-nofile")
        result = await _translator(repo).handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sess-nofile",
                "transcript_path": str(tmp_path / "nope.jsonl"),
                "trigger": "manual",
            }
        )
        assert result["leader_usage"] is None
        assert result["leader_usage_skip"] == "no-transcript"

    @pytest.mark.asyncio
    async def test_throttled_stop_is_named(self, db_repository, tmp_path: Path):
        """"None" 曾经同时表示成功无数据/被节流/出错 —— 现在节流有自己的名字。"""
        repo = db_repository
        await _make_leader(repo, "sess-thrname")
        t = _transcript(tmp_path)
        translator = _translator(repo)
        payload = {
            "hook_event_name": "Stop",
            "session_id": "sess-thrname",
            "transcript_path": str(t),
        }
        first = await translator.handle_event(payload)
        assert first["leader_usage_skip"] is None
        second = await translator.handle_event(payload)
        assert second["leader_usage"] is None
        assert second["leader_usage_skip"] == "throttled"

    @pytest.mark.asyncio
    async def test_transcript_without_usage_rows_is_named(self, db_repository, tmp_path: Path):
        """no-data ≠ zero:一列都不写,但要说得出"是没数据,不是没跑"。"""
        repo = db_repository
        leader = await _make_leader(repo, "sess-empty")
        empty = tmp_path / "empty.jsonl"
        empty.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
            encoding="utf-8",
        )
        result = await _translator(repo).handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sess-empty",
                "transcript_path": str(empty),
                "trigger": "manual",
            }
        )
        assert result["leader_usage_skip"] == "no-usage-rows"
        fetched = await repo.get_agent(leader.id)
        assert fetched.tokens_measured_at is None  # 依旧一列都不写

    @pytest.mark.asyncio
    async def test_meter_reports_the_same_names(self, tmp_path: Path):
        """采集器自己也要说得出原因,不能只有调用侧靠猜。"""
        meter = leader_usage.SessionUsageMeter()
        t = _transcript(tmp_path)
        snap, reason = meter.capture_or_reason("s1", t, force=True)
        assert snap is not None
        assert reason is None
        snap, reason = meter.capture_or_reason("s1", t, force=False)
        assert snap is None
        assert reason == leader_usage.SKIP_THROTTLED
        snap, reason = meter.capture_or_reason("s2", tmp_path / "absent.jsonl", force=True)
        assert snap is None
        assert reason == leader_usage.SKIP_UNREADABLE


# ============================================================
# 4. 兜底路径的前提条件必须真的到得了服务端
# ============================================================


class TestSlugFallbackPrerequisite:
    def test_stripped_payload_still_carries_cwd(self):
        """``locate_main_transcript`` 的 slug 兜底要 cwd + session_id 才成立。

        它的 docstring 写明兜的就是"超大载荷被整体剥离到必留字段"那一档,可
        ``send_event.ESSENTIAL_FIELDS`` 里偏偏没有 ``cwd`` —— 剥离一发生,兜底
        的前提就跟着一起没了,于是这条唯一的退路在**它专为之而生的场景里**必然
        失效。这是自相矛盾,不是取舍。
        """
        from aiteam.hooks import send_event

        assert "cwd" in send_event.ESSENTIAL_FIELDS

    def test_fallback_locates_transcript_from_cwd_alone(self, tmp_path: Path, monkeypatch):
        """剥离后只剩 session_id + cwd 时,定位仍须成立(路径字段已不可用)。"""
        cwd = "/Users/x/Desktop/proj"
        projects = tmp_path / "projects"
        (projects / session_probe.project_slug(cwd)).mkdir(parents=True)
        real = _transcript(projects / session_probe.project_slug(cwd), name="sid-77.jsonl")
        monkeypatch.setattr(leader_usage, "_projects_dir", lambda: projects)
        truncated = "/x" * 400 + "...(truncated)"
        assert leader_usage.locate_main_transcript(truncated, cwd=cwd, session_id="sid-77") == real
