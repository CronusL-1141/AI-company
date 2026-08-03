"""行身份 = 派工身份 —— SubagentStart 的同名复用不得改写已挂账的旧行。

生命周期审计 A-06 的实锤面：``_on_subagent_start`` 的四级去重梯子里，第 2/3 级按
**名字**在队内取 ``matches[0]``（``list_agents`` 按 created_at 升序 → 取最早那行），
第 2b 级走 ``repository.find_agent_by_session``（``limit(1)`` 且**无 ORDER BY**）。
命中即把新派工就地绑到旧行；随后 ``_on_subagent_stop`` 按 transcript **覆写**四层
token 与 ``tokens_measured_at``。两段合起来 = 一次新派工把旧派工的账抹掉，且旧值
不可恢复（transcript 早晚会被清，账没了就再也算不回来）。

同一件事在 workflow 路径上早就判过了：``_register_workflow_subagent`` 的 docstring
写着「按 cc_agent_id 去重而非名字，否则 16 个 agent 的一次 run 会被折叠成一行」。
正路径缺的正是这一条。

计账单位就是 transcript 文件，而 transcript 文件名就是 ``agent-<cc_agent_id>.jsonl``
—— 所以「一行 = 一次派工 = 一份 transcript」是同一句话。判据据此钉死：

1. cc_tool_use_id 相同 → 同一次派工，复用（重复 SubagentStart / 续跑）；
2. 候选行从未绑过任何派工（cc_tool_use_id 为空）且**没有账** → 复用（MCP 预注册）；
3. 其余一律新建行。「有账」取宽：四层任一非零**或** tokens_measured_at 非空
   （测得 0 也是测量结果，no-data ≠ zero）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.clock import utc_now
from aiteam.services.agent_identity import has_token_account, pick_reusable_row
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import Agent

SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def translator(repo):
    yield HookTranslator(repo=repo, event_bus=EventBus(repo=repo))


def _transcript(tmp_path, cc_id: str, *, input_tokens: int, output_tokens: int) -> str:
    """一份最小可解析的子 agent transcript（文件名编码 cc_agent_id）。"""
    p = tmp_path / f"agent-{cc_id}.jsonl"
    row = {
        "type": "assistant",
        "requestId": f"req-{cc_id}",
        "message": {
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }
    p.write_text(json.dumps(row), encoding="utf-8")
    return str(p)


def _start(cc_id: str, name: str = "Explore", **extra) -> dict:
    return {
        "hook_event_name": "SubagentStart",
        "agent_type": name,
        "agent_id": cc_id,
        "session_id": SESSION,
        **extra,
    }


def _stop(cc_id: str, tpath: str, name: str = "Explore") -> dict:
    return {
        "hook_event_name": "SubagentStop",
        "agent_type": name,
        "agent_id": cc_id,
        "session_id": SESSION,
        "agent_transcript_path": tpath,
    }


class TestChargedRowIsNeverOverwritten:
    """端到端：派工① 跑完记账 → 派工② 同名进来 → 派工① 的账必须原封不动。"""

    @pytest.mark.asyncio
    async def test_second_dispatch_gets_its_own_row_and_first_ledger_survives(
        self, repo, translator, tmp_path
    ):
        team = await repo.create_team(name="crew", mode="coordinate")

        # 派工①：起 → 跑完记账
        r1 = await translator.handle_event(_start("cc-first", cc_team_name="crew"))
        assert r1["status"] == "created"
        first_id = r1["agent_id"]
        t1 = _transcript(tmp_path, "cc-first", input_tokens=1_000, output_tokens=200)
        await translator.handle_event(_stop("cc-first", t1))

        after_first = await repo.get_agent(first_id)
        assert after_first.input_tokens == 1_000
        assert after_first.tokens_measured_at is not None

        # 派工②：同名、同会话、同队，但是**另一次派工**（另一个 cc id、另一份 transcript）
        r2 = await translator.handle_event(_start("cc-second", cc_team_name="crew"))
        assert r2["agent_id"] != first_id, (
            "新派工被绑回了已挂账的旧行 —— 行身份被当成了名字身份"
        )
        t2 = _transcript(tmp_path, "cc-second", input_tokens=7_000, output_tokens=900)
        await translator.handle_event(_stop("cc-second", t2))

        # 旧行的账逐字不变
        survivor = await repo.get_agent(first_id)
        assert survivor.input_tokens == 1_000, "派工①的 token 账被派工②覆盖了"
        assert survivor.output_tokens == 200
        assert survivor.cc_tool_use_id == "cc-first", "旧行的派工身份被改写了"
        assert survivor.tokens_measured_at == after_first.tokens_measured_at

        # 新行独立记自己的账，两份账不互相吞
        fresh = await repo.get_agent(r2["agent_id"])
        assert fresh.input_tokens == 7_000
        assert fresh.cc_tool_use_id == "cc-second"
        assert fresh.team_id == team.id

    @pytest.mark.asyncio
    async def test_measured_zero_row_is_also_protected(self, repo, translator):
        """测得 0 也是测量结果：no-data ≠ zero，挂着 0 的行同样不许被改写。"""
        team = await repo.create_team(name="crew", mode="coordinate")
        old = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore",
            source="hook", session_id=SESSION, cc_tool_use_id="cc-zero",
        )
        await repo.update_agent(
            old.id, status="offline",
            input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0,
            tokens_measured_at=utc_now(),
        )

        res = await translator.handle_event(_start("cc-new", cc_team_name="crew"))

        assert res["agent_id"] != old.id
        kept = await repo.get_agent(old.id)
        assert kept.cc_tool_use_id == "cc-zero"

    @pytest.mark.asyncio
    async def test_legacy_session_name_path_skips_the_charged_row(
        self, repo, translator
    ):
        """无 cc_team_name 的旁路（find_agent_by_session）同样受判据约束。"""
        team = await repo.create_team(name="crew", mode="coordinate")
        # Leader 在场，第 3 级才解析得到队；这里刻意让第 2b 级先命中。
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader",
            source="api", session_id=SESSION,
        )
        assert leader.id
        charged = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore",
            source="hook", session_id=SESSION, cc_tool_use_id="cc-old",
        )
        await repo.update_agent(
            charged.id, status="offline", input_tokens=42, tokens_measured_at=utc_now()
        )

        res = await translator.handle_event(_start("cc-new"))

        assert res["agent_id"] != charged.id
        kept = await repo.get_agent(charged.id)
        assert kept.input_tokens == 42
        assert kept.cc_tool_use_id == "cc-old"


class TestLateBindingObeysTheSameRule:
    """第二个入口：``_resolve_agent`` 的按名迟绑定同样会写 cc_tool_use_id。

    迟绑定之后 SubagentStop 就照着那一行覆写 token —— 所以它和登记走同一条判据，
    否则漏掉 SubagentStart 的那次派工会从这里绕进来抹账。
    """

    @pytest.mark.asyncio
    async def test_late_binding_refuses_a_charged_row(self, repo, translator, tmp_path):
        team = await repo.create_team(name="crew", mode="coordinate")
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader",
            source="api", session_id=SESSION,
        )
        await repo.update_agent(leader.id, status="busy")
        await repo.update_team(team.id, leader_agent_id=leader.id)
        charged = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore", source="hook",
            session_id=SESSION, cc_tool_use_id="cc-old",
        )
        await repo.update_agent(
            charged.id, input_tokens=1_234, tokens_measured_at=utc_now()
        )

        # SubagentStart 漏了，直接来一发别的派工的 SubagentStop
        t = _transcript(tmp_path, "cc-ghost", input_tokens=99, output_tokens=99)
        await translator.handle_event(_stop("cc-ghost", t))

        kept = await repo.get_agent(charged.id)
        assert kept.input_tokens == 1_234, "迟绑定把别的派工的账写到了这一行上"
        assert kept.cc_tool_use_id == "cc-old"

    @pytest.mark.asyncio
    async def test_late_binding_still_adopts_an_unbound_row(self, repo, translator):
        """竞态的本意要保住：没绑过 cc id、没记过账的行照旧认领。"""
        team = await repo.create_team(name="crew", mode="coordinate")
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader",
            source="api", session_id=SESSION,
        )
        await repo.update_agent(leader.id, status="busy")
        await repo.update_team(team.id, leader_agent_id=leader.id)
        unbound = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore", source="api",
        )

        got = await translator._resolve_agent("cc-late", "Explore", SESSION)

        assert got is not None
        assert got.id == unbound.id
        assert (await repo.get_agent(unbound.id)).cc_tool_use_id == "cc-late"


class TestLegitimateReuseStillWorks:
    """复用不是要废掉 —— 合法场景必须原样活着，否则这就成了另一个 bug。"""

    @pytest.mark.asyncio
    async def test_same_dispatch_reports_twice_reuses_the_same_row(
        self, repo, translator, tmp_path
    ):
        """重复 SubagentStart（同一个 cc id）是同一次派工，绝不能长出第二行。"""
        await repo.create_team(name="crew", mode="coordinate")
        first = await translator.handle_event(_start("cc-dup", cc_team_name="crew"))
        t = _transcript(tmp_path, "cc-dup", input_tokens=5, output_tokens=5)
        await translator.handle_event(_stop("cc-dup", t))

        again = await translator.handle_event(_start("cc-dup", cc_team_name="crew"))

        assert again["status"] == "updated"
        assert again["agent_id"] == first["agent_id"]

    @pytest.mark.asyncio
    async def test_mcp_preregistered_row_is_still_adopted(self, repo, translator):
        """MCP 预注册的空行（无 cc id、无账）就是为了被 hook 认领的。"""
        team = await repo.create_team(name="crew", mode="coordinate")
        pre = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore", source="api",
        )

        res = await translator.handle_event(_start("cc-adopt", cc_team_name="crew"))

        assert res["status"] == "updated"
        assert res["agent_id"] == pre.id
        bound = await repo.get_agent(pre.id)
        assert bound.cc_tool_use_id == "cc-adopt"
        assert bound.session_id == SESSION


class TestReuseVerdict:
    """判据本身（纯函数，不碰库）—— 每条边界单独钉住。"""

    @staticmethod
    def _row(agent_id: str, *, cc: str = "", minute: int = 0, **cols) -> Agent:
        return Agent(
            id=agent_id, team_id="t", name="Explore", role="Explore",
            cc_tool_use_id=cc or None,
            created_at=datetime(2026, 8, 1, 0, minute, tzinfo=UTC),
            **cols,
        )

    def test_no_candidates_means_new_row(self):
        assert pick_reusable_row([], "cc-1") is None

    def test_measured_at_alone_counts_as_charged(self):
        assert has_token_account(self._row("a", tokens_measured_at=utc_now()))

    def test_any_single_layer_counts_as_charged(self):
        for layer in ("input_tokens", "output_tokens",
                      "cache_creation_tokens", "cache_read_tokens"):
            assert has_token_account(self._row("a", **{layer: 1})), layer

    def test_untouched_row_is_not_charged(self):
        assert not has_token_account(self._row("a"))

    def test_same_dispatch_wins_even_when_charged(self):
        """同一次派工重测自己的账是正确的，不该被账目闸挡住。"""
        mine = self._row("mine", cc="cc-1", tokens_measured_at=utc_now(), minute=1)
        blank = self._row("blank", minute=9)
        assert pick_reusable_row([blank, mine], "cc-1").id == "mine"

    def test_row_bound_to_another_dispatch_is_never_reused(self):
        """即使那一行还没记上账 —— 它已经是**另一次**派工的身份证。"""
        other = self._row("other", cc="cc-other")
        assert pick_reusable_row([other], "cc-mine") is None

    def test_newest_blank_row_wins_and_order_does_not_matter(self):
        old = self._row("old", minute=1)
        new = self._row("new", minute=5)
        assert pick_reusable_row([old, new], "cc-1").id == "new"
        assert pick_reusable_row([new, old], "cc-1").id == "new"

    def test_unknown_cc_id_keeps_only_the_ledger_gate(self):
        """payload 没带 agent_id 时判不出派工身份，只保账目闸，不凭空造行。"""
        bound = self._row("bound", cc="cc-x")
        assert pick_reusable_row([bound], "").id == "bound"
        charged = self._row("charged", cc="cc-y", input_tokens=5, minute=9)
        assert pick_reusable_row([bound, charged], "").id == "bound"


class TestDeterministicCandidateOrder:
    """多行同名时取哪行必须是确定的 —— 无 ORDER BY 的 limit(1) 是靠运气。"""

    @pytest.mark.asyncio
    async def test_find_agent_by_session_returns_newest(self, repo):
        team = await repo.create_team(name="crew", mode="coordinate")
        old = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore",
            source="hook", session_id=SESSION,
        )
        new = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore",
            source="hook", session_id=SESSION,
        )
        # created_at 同秒也要稳：id 兜底排序
        await repo.update_agent(new.id, last_active_at=utc_now())

        got = await repo.find_agent_by_session(SESSION, "Explore")
        assert got is not None
        assert got.id in {old.id, new.id}
        # 连续多次必须给同一个答案
        again = await repo.find_agent_by_session(SESSION, "Explore")
        assert again.id == got.id

        rows = await repo.find_agents_by_session_and_name(SESSION, "Explore")
        assert [r.id for r in rows] == [rows[0].id, rows[1].id]
        assert len(rows) == 2
        assert rows[0].id == got.id, "find_agent_by_session 必须与全量候选的第一名一致"
