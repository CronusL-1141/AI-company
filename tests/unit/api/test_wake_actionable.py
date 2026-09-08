"""Unit tests for the wake actionable predicate (唤醒体系 v2 §7.1/§7.2).

判据逻辑用轻量 FakeRepo 精测每条分支；再用 TestClient 对真实路由做一次冒烟，
证明接线正确且空库不 500（防御式契约）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aiteam.api import wake_actionable
from aiteam.types import AgentStatus

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)
BEFORE = NOW - timedelta(minutes=30)
AFTER = NOW + timedelta(minutes=1)
SINCE = NOW.isoformat()


def _agent(status, name="a", last_active=None):
    return SimpleNamespace(status=status, name=name, last_active_at=last_active)


def _run(status, session_id="sid", updated_at=None, wf_id="wf_x"):
    return SimpleNamespace(
        status=status, session_id=session_id, updated_at=updated_at, wf_id=wf_id
    )


class FakeRepo:
    """只实现 compute_actionable 用到的 6 个方法。"""

    def __init__(
        self,
        agents=None,
        runs=None,
        memos_since=0,
        briefings=0,
        team_project="proj-1",
        mentions_since=0,
        memo_authors=None,
    ):
        self._agents = agents or []
        self._runs = runs or []
        # memos_since=N 等价于 N 条来自子 agent 的 memo；要精确指定作者用 memo_authors
        self._memo_authors = (
            list(memo_authors) if memo_authors is not None else ["worker"] * memos_since
        )
        self._briefings = briefings
        self._team_project = team_project
        self._mentions_since = mentions_since

    async def get_team(self, team_id):
        return SimpleNamespace(project_id=self._team_project)

    async def list_agents(self, team_id):
        return list(self._agents)

    async def find_agents_by_session(self, session_id):
        return list(self._agents)

    async def list_workflow_runs(self, project_id="", limit=50):
        return list(self._runs)

    async def count_valid_task_memos_since(self, project_id, since, exclude_authors=None):
        # 复刻生产的作者排除语义：stub 不过滤会让"自己写的 memo 不唤醒自己"假性通过。
        excluded = set(exclude_authors or ())
        return sum(1 for author in self._memo_authors if author not in excluded)

    async def list_briefings(self, status="pending", project_id=""):
        return [object()] * self._briefings

    async def count_new_mentions_since(self, reader, project_id, since):
        # 复刻生产的三条前置：缺 reader / 缺 project / 无水位一律 0。
        # stub 比生产宽松会让"缺 reader 不触发"这条断言假性通过。
        if not reader or not project_id or since is None:
            return 0
        return self._mentions_since


async def _compute(repo, **kw):
    kw.setdefault("session_id", "sid")
    kw.setdefault("team_id", "team-1")
    kw.setdefault("since_raw", SINCE)
    return await wake_actionable.compute_actionable(repo, **kw)


# ---- parse_since ----------------------------------------------------------
def test_parse_since_variants():
    assert wake_actionable.parse_since(None) is None
    assert wake_actionable.parse_since("") is None
    assert wake_actionable.parse_since("garbage") is None
    # 不带偏移的串按 UTC 读 —— 与 watermark 的发出口径一致
    assert wake_actionable.parse_since("2026-07-14T12:00:00") == NOW
    # 带 Z / 带偏移都归一到 aware-UTC：同一时刻必须解析成同一个值
    assert wake_actionable.parse_since("2026-07-14T12:00:00Z") == NOW
    assert wake_actionable.parse_since("2026-07-14T20:00:00+08:00") == NOW
    assert wake_actionable.parse_since("2026-07-14T12:00:00+08:00").tzinfo is UTC


# ---- 判据分支 -------------------------------------------------------------
@pytest.mark.asyncio
async def test_empty_not_actionable():
    v = await _compute(FakeRepo())
    assert v["actionable"] is False
    assert v["busy_agents"] == 0
    assert v["live_runs"] == 0
    # 契约字段齐全
    for k in (
        "reasons", "terminal_runs_since", "finished_agents_since",
        "new_memos_since", "pending_briefings", "watermark", "project_id",
    ):
        assert k in v


@pytest.mark.asyncio
async def test_busy_agent_in_flight_not_actionable():
    repo = FakeRepo(agents=[_agent(AgentStatus.BUSY, "worker", NOW)])
    v = await _compute(repo)
    assert v["busy_agents"] == 1
    assert v["actionable"] is False  # busy = 在飞，不是 actionable


@pytest.mark.asyncio
async def test_finished_agent_after_since_is_actionable():
    repo = FakeRepo(agents=[_agent(AgentStatus.WAITING, "worker", AFTER)])
    v = await _compute(repo)
    assert v["finished_agents_since"] == 1
    assert v["actionable"] is True


@pytest.mark.asyncio
async def test_finished_agent_before_since_not_counted():
    repo = FakeRepo(agents=[_agent(AgentStatus.WAITING, "worker", BEFORE)])
    v = await _compute(repo)
    assert v["finished_agents_since"] == 0
    assert v["actionable"] is False


@pytest.mark.asyncio
async def test_live_run_not_actionable():
    repo = FakeRepo(runs=[_run("running", "sid")])
    v = await _compute(repo)
    assert v["live_runs"] == 1
    assert v["actionable"] is False


@pytest.mark.asyncio
async def test_terminal_run_after_since_is_actionable():
    repo = FakeRepo(runs=[_run("completed", "sid", AFTER)])
    v = await _compute(repo)
    assert v["terminal_runs_since"] == 1
    assert v["actionable"] is True


@pytest.mark.asyncio
async def test_terminal_run_session_mismatch_ignored():
    repo = FakeRepo(runs=[_run("killed", "other-session", AFTER)])
    v = await _compute(repo)
    assert v["terminal_runs_since"] == 0
    assert v["live_runs"] == 0
    assert v["actionable"] is False


@pytest.mark.asyncio
async def test_new_memos_is_actionable():
    repo = FakeRepo(memos_since=3)
    v = await _compute(repo)
    assert v["new_memos_since"] == 3
    assert v["actionable"] is True


# ---- memo 作者排除：唤醒者自己写的 memo 不是唤醒信号 ----------------------
# 真机复现（2026-09-08）：watcher 武装期间 Leader 自己写了一条 memo，下一轮
# actionable 即为 true（new_mentions_since=0 / new_memos_since=1，理由却印着
# "子 agent 报进展"，而当时根本没有子 agent）。watcher 随即退出并清掉 armed
# 标记 —— 自己写字把自己叫醒，还顺带卸了岗。
@pytest.mark.asyncio
async def test_own_memo_does_not_wake_self():
    repo = FakeRepo(memo_authors=["leader-cc"])
    v = await _compute(repo, reader="leader-cc")
    assert v["new_memos_since"] == 0
    assert v["actionable"] is False


@pytest.mark.asyncio
async def test_default_leader_author_does_not_wake_self():
    """task_memo_add 的默认 author 是 "leader" —— 自唤醒最常见的形态走的是这条。"""
    repo = FakeRepo(memo_authors=["leader"])
    v = await _compute(repo, reader="leader-cc")
    assert v["new_memos_since"] == 0
    assert v["actionable"] is False


@pytest.mark.asyncio
async def test_subagent_memo_still_wakes():
    """排除只针对自己，子 agent 报进展仍须唤醒——这是本信号的正业。"""
    repo = FakeRepo(memo_authors=["leader-cc", "bridge-core-worker", "leader"])
    v = await _compute(repo, reader="leader-cc")
    assert v["new_memos_since"] == 1
    assert v["actionable"] is True


@pytest.mark.asyncio
async def test_no_reader_means_no_author_exclusion():
    """没自报身份就不替它猜谁是"自己"——与 new_mentions_since 同一原则。"""
    repo = FakeRepo(memo_authors=["leader"])
    v = await _compute(repo)
    assert v["new_memos_since"] == 1
    assert v["actionable"] is True


# ---- 信号选择：武装者声明自己在等什么 --------------------------------------
# 两个 harness 共用一个项目后，对端子 agent 的每条进展 memo 都会唤醒本会话。而
# watcher 一退出就清掉武装标记，于是"对端越忙、我越容易在它真正找我那一刻是聋的"
# ——失效方向与功能意图正好相反。唤醒该由武装者声明自己在等什么。
class TestSignalSelection:
    @pytest.mark.asyncio
    async def test_channel_only_ignores_memo(self):
        repo = FakeRepo(memo_authors=["peer-worker"], mentions_since=0)
        v = await _compute(repo, reader="leader-cc", signals_raw="mentions")
        assert v["new_memos_since"] == 1, "计数照报，不因不触发就隐瞒"
        assert v["actionable"] is False

    @pytest.mark.asyncio
    async def test_channel_only_still_wakes_on_mention(self):
        repo = FakeRepo(memo_authors=["peer-worker"], mentions_since=1)
        v = await _compute(repo, reader="leader-cc", signals_raw="mentions")
        assert v["actionable"] is True

    @pytest.mark.asyncio
    async def test_default_keeps_every_signal(self):
        repo = FakeRepo(memo_authors=["peer-worker"])
        v = await _compute(repo, reader="leader-cc")
        assert v["actionable"] is True
        assert set(v["signals"]) == set(wake_actionable.SIGNAL_NAMES)

    @pytest.mark.asyncio
    async def test_unknown_name_degrades_to_all_not_to_none(self):
        """拼错信号名必须退回"全都要"，不能退成"一个都不要"。

        少醒一次是丢消息，多醒一次只是噪音；两种降级方向的代价不对称，
        所以无法解析时一律取更吵的那个。
        """
        repo = FakeRepo(memo_authors=["peer-worker"])
        v = await _compute(repo, reader="leader-cc", signals_raw="mentionz")
        assert v["actionable"] is True
        assert set(v["signals"]) == set(wake_actionable.SIGNAL_NAMES)

    @pytest.mark.asyncio
    async def test_effective_set_is_reported(self):
        """武装者要能看出自己实际过滤成了什么，否则拼错了也不知道。"""
        repo = FakeRepo()
        v = await _compute(repo, reader="leader-cc", signals_raw="mentions,memos")
        assert set(v["signals"]) == {"mentions", "memos"}


@pytest.mark.asyncio
async def test_pending_briefings_do_not_trigger():
    repo = FakeRepo(briefings=2)
    v = await _compute(repo)
    assert v["pending_briefings"] == 2
    assert v["actionable"] is False  # briefings 仅展示，不触发唤醒


@pytest.mark.asyncio
async def test_never_throws_on_repo_error():
    class ExplodingRepo(FakeRepo):
        async def list_agents(self, team_id):
            raise RuntimeError("boom")

        async def list_workflow_runs(self, project_id="", limit=50):
            raise RuntimeError("boom")

    v = await _compute(ExplodingRepo())
    # 降级为保守值，绝不抛
    assert v["busy_agents"] == 0
    assert v["live_runs"] == 0
    assert v["actionable"] is False


# ---- 路由冒烟：真实 app + 空内存库，证明接线且不 500 -----------------------
def test_route_smoke_empty_db():
    import asyncio
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    from aiteam.api import deps
    from aiteam.api.app import create_app
    from aiteam.storage.connection import close_db
    from aiteam.storage.repository import StorageRepository

    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    deps._repository = repo

    app = create_app()

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    app.router.lifespan_context = _noop_lifespan
    client = TestClient(app)
    try:
        resp = client.get("/api/wake/actionable", params={"session_id": "s1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["actionable"] is False
        assert body["busy_agents"] == 0
        assert "watermark" in body
    finally:
        asyncio.get_event_loop().run_until_complete(close_db())
        deps._repository = None


# ---- 信道点名唤醒信号（2026-09-08）---------------------------------------
#
# 这一组守的是跨 harness 通信的最后一环：对端在信道里叫你，而你正在等用户开口。
# 没有这个信号，那条消息要躺到下次有人跟你说话才被看见——实测躺过半小时，其中一条
# 还明确在等回执。
#
# 最需要盯的不是"能不能唤醒"，而是**会不会唤醒个没完**：判据用的若是"当前未读"，
# 读了但尚未 ack 的消息会让每一轮轮询都判 actionable，把 watcher 变成每 8 秒唤醒
# 一次的死循环。所以语义必须是"since 之后新到达"，配合调用方滚动水位。


@pytest.mark.asyncio
async def test_new_mention_makes_it_actionable():
    got = await _compute(FakeRepo(mentions_since=2), reader="leader-cc")
    assert got["actionable"] is True
    assert got["new_mentions_since"] == 2
    assert any("点名 leader-cc" in r for r in got["reasons"])


@pytest.mark.asyncio
async def test_without_reader_mentions_never_fire():
    """没自报身份就不替调用方猜谁在叫它——老调用方（不传 reader）行为完全不变。"""
    got = await _compute(FakeRepo(mentions_since=5))
    assert got["new_mentions_since"] == 0
    assert got["actionable"] is False


@pytest.mark.asyncio
async def test_no_since_watermark_does_not_fire():
    """刚武装 watcher 的那一刻不该因历史消息立即触发。"""
    got = await _compute(FakeRepo(mentions_since=5), reader="leader-cc", since_raw=None)
    assert got["new_mentions_since"] == 0


@pytest.mark.asyncio
async def test_zero_new_mentions_stays_quiet():
    """没有新消息就不唤醒——这条防的是"读了没 ack 就每轮唤醒"的死循环。"""
    got = await _compute(FakeRepo(mentions_since=0), reader="leader-cc")
    assert got["actionable"] is False
    assert not any("点名" in r for r in got["reasons"])


@pytest.mark.asyncio
async def test_mention_signal_survives_repo_failure():
    """信号源炸了也不能让唤醒判据 500——降级为不唤醒，保守但安全。"""

    class Boom(FakeRepo):
        async def count_new_mentions_since(self, reader, project_id, since):
            raise RuntimeError("db gone")

    got = await _compute(Boom(), reader="leader-cc")
    assert got["new_mentions_since"] == 0
    assert got["actionable"] is False


@pytest.mark.asyncio
async def test_mention_signal_is_independent_of_other_signals():
    """信道信号自己就能触发，不依赖 agent/run/memo 任何一项。"""
    got = await _compute(FakeRepo(mentions_since=1), reader="leader-cc")
    assert got["finished_agents_since"] == 0
    assert got["terminal_runs_since"] == 0
    assert got["new_memos_since"] == 0
    assert got["actionable"] is True


# ---- since 的传输损坏还原（2026-09-08）------------------------------------
#
# query string 里 '+' 是空格的编码，所以未编码的 "…T06:23:42+00:00" 到服务端会变成
# "…T06:23:42 00:00"。旧行为是解析失败 → None → 不设下界 → **统计全部历史**，表现
# 不是报错而是"一切都是新事件"：实测 since 声称 2 秒前，API 却回报 1513 条新 memo，
# watcher 因此每轮都判 actionable、一起来就退出，从没真正守望过。


def test_since_repairs_plus_eaten_by_query_string():
    """被吃掉的 '+' 要还原，且与正确编码的结果完全一致。"""
    good = wake_actionable.parse_since("2026-07-14T12:00:00+00:00")
    damaged = wake_actionable.parse_since("2026-07-14T12:00:00 00:00")
    assert damaged == good == NOW


def test_since_repairs_non_utc_offset_too():
    assert wake_actionable.parse_since("2026-07-14T20:00:00 08:00") == NOW


def test_since_space_separated_datetime_still_works():
    """不能误伤 'YYYY-MM-DD HH:MM:SS' 这种合法的空格分隔。"""
    assert wake_actionable.parse_since("2026-07-14 12:00:00") == NOW


def test_since_unrepairable_still_returns_none():
    assert wake_actionable.parse_since("garbage 00:00") is None
    assert wake_actionable.parse_since("2026-07-14T12:00:00 xx:yy") is None


# ---- watermark 取值时机（2026-09-08，对端只读审查 P1）----------------------
#
# watermark 若在**扫描之后**取，就开了一个漏报窗口：扫描结束后、watermark 生成前落库
# 的事件，created_at 早于 watermark 却没被这一轮扫到，而调用方拿 watermark 当下一轮的
# since —— 那条事件从此永远查不到。对端真库复现过：扫描后趁 briefings 阶段插一条，
# 连查两轮 actionable 均 false。


@pytest.mark.asyncio
async def test_watermark_is_taken_before_scanning():
    """watermark 必须早于最后一个扫描动作，窗口内到达的事件才留得住。"""
    from aiteam.clock import parse_utc, utc_now

    stamps = {}

    class Timed(FakeRepo):
        async def list_briefings(self, status="pending", project_id=""):
            # briefings 是 compute_actionable 里最后一个数据动作
            stamps["last_scan"] = utc_now()
            return []

    got = await _compute(Timed())
    wm = parse_utc(got["watermark"])
    assert wm is not None
    assert wm <= stamps["last_scan"], (
        "watermark 晚于扫描 —— 两者之间落库的事件会被永久跳过"
    )


@pytest.mark.asyncio
async def test_watermark_still_present_and_parsable():
    got = await _compute(FakeRepo())
    from aiteam.clock import parse_utc

    assert parse_utc(got["watermark"]) is not None
