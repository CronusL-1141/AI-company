"""子 agent token 归因 — 观测断链闭合批 ③。

背景:agents 表此前只有"上下文水位"口径(ctx_tokens/ctx_pct),**没有任何计费口径
的采集**。workflow 侧 workflow_agents.tokens 也大面积为空——实测 model='opus' 的
170 行里 170 行 token 为 0/NULL,且分布持续到最近(07-21 就有 123 行),不是早期遗留。

根因不是采集坏了,而是**数据源只有请求规格**:同一份 workflow JSON 里,82 个 agent
只有"要什么模型"(model 原样写着别名 opus、tokens 为 null),只有 24 个带回遥测。
OS 原样落库,于是别名行天然零 token。

关键发现:这些 agent 的 **transcript 完整存在**,且 transcript 里 `message.model`
**永远是完整型号**(实测 "claude-opus-5" / "claude-opus-4-8"),从不是别名。所以真实
型号与 token 都能从 transcript 无损回采——别名映射表只需给 transcript 已灭失的行
兜底,且**只在读侧解析,绝不回写 model 字段**(2026-07-07 铁律:模型字段未知就空着
由观测回填,不写死型号)。

累加算法是本项最容易做错的地方:一次 API 调用会产出**多条** assistant 行(每个
content block 一条),实测 79 行里只有 17 个唯一 requestId;同一 requestId 内
input/cache 恒定不变,而 output_tokens 随流式**递增**。因此正确算法是
**按 requestId 分组、每组取最后一条快照、再跨组累加**。逐行裸加会严重虚高:
实测 input 会从真值 12,185 涨到 72,556,cache_read 从 1,289,742 涨到 5,105,773。
"""

from __future__ import annotations

import json

from aiteam.services.token_attribution import (
    parse_transcript_usage,
    resolve_model_alias,
)


def _write(tmp_path, rows):
    p = tmp_path / "agent-x.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _assistant(request_id, model, usage):
    return {"type": "assistant", "requestId": request_id, "message": {"model": model, "usage": usage}}


class TestUsageParsing:
    def test_streaming_rows_are_not_double_counted(self, tmp_path):
        """同一 requestId 的多行是同一次调用的快照,取最后一条,不是相加。"""
        rows = [
            _assistant("req-1", "claude-opus-5", {"input_tokens": 10, "output_tokens": 3,
                                                  "cache_read_input_tokens": 100,
                                                  "cache_creation_input_tokens": 50}),
            _assistant("req-1", "claude-opus-5", {"input_tokens": 10, "output_tokens": 200,
                                                  "cache_read_input_tokens": 100,
                                                  "cache_creation_input_tokens": 50}),
            _assistant("req-1", "claude-opus-5", {"input_tokens": 10, "output_tokens": 583,
                                                  "cache_read_input_tokens": 100,
                                                  "cache_creation_input_tokens": 50}),
        ]
        got = parse_transcript_usage(_write(tmp_path, rows))
        assert got["output_tokens"] == 583, "流式递增被逐行相加了"
        assert got["input_tokens"] == 10
        assert got["cache_read_tokens"] == 100
        assert got["cache_creation_tokens"] == 50
        assert got["api_calls"] == 1

    def test_multiple_calls_accumulate(self, tmp_path):
        rows = [
            _assistant("req-1", "claude-opus-5", {"input_tokens": 10, "output_tokens": 5}),
            _assistant("req-2", "claude-opus-5", {"input_tokens": 7, "output_tokens": 11}),
        ]
        got = parse_transcript_usage(_write(tmp_path, rows))
        assert got["input_tokens"] == 17
        assert got["output_tokens"] == 16
        assert got["api_calls"] == 2

    def test_model_comes_from_transcript_verbatim(self, tmp_path):
        """transcript 里是完整型号,原样采下来——这就是"观测回填"。"""
        rows = [_assistant("req-1", "claude-opus-4-8", {"input_tokens": 1, "output_tokens": 1})]
        got = parse_transcript_usage(_write(tmp_path, rows))
        assert got["model"] == "claude-opus-4-8"
        assert got["model_source"] == "transcript"

    def test_non_assistant_rows_ignored(self, tmp_path):
        rows = [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "attachment"},
            _assistant("req-1", "claude-opus-5", {"input_tokens": 4, "output_tokens": 2}),
        ]
        got = parse_transcript_usage(_write(tmp_path, rows))
        assert got["input_tokens"] == 4
        assert got["api_calls"] == 1

    def test_missing_file_yields_nothing_not_zero(self, tmp_path):
        """文件不存在 ≠ 用了 0 token。no-data 必须和 zero 区分开。"""
        got = parse_transcript_usage(tmp_path / "nope.jsonl")
        assert got is None

    def test_synthetic_model_line_does_not_become_the_model(self, tmp_path):
        """compact 合成行的 "<synthetic>" 不是型号,不许覆盖真实型号。

        取 model 的写法是"后来者覆盖前者",所以一份 transcript 只要在**末尾**有一条
        合成行,整份归因的 model 就会被写成 "<synthetic>"。子 agent transcript 一般
        不含合成行,所以这个缺陷一直没被咬到;主会话 transcript 一上来就有——实测一份
        35.1 MB 的主会话解析回来的 model 正是 "<synthetic>"。
        """
        rows = [
            _assistant("req-1", "claude-opus-5", {"input_tokens": 10, "output_tokens": 5}),
            _assistant("req-2", "<synthetic>", {"input_tokens": 2, "output_tokens": 1}),
        ]
        got = parse_transcript_usage(_write(tmp_path, rows))
        assert got["model"] == "claude-opus-5", "合成行覆盖了真实型号"
        assert got["model_source"] == "transcript"
        # 只过滤型号,不动用量:两次调用照常累加(改口径不是本次的事)
        assert got["input_tokens"] == 12
        assert got["api_calls"] == 2

    def test_all_synthetic_yields_unknown_model_not_a_fake_one(self, tmp_path):
        """全是合成行时如实返回"型号未知",不编一个出来。"""
        rows = [_assistant("req-1", "<synthetic>", {"input_tokens": 3, "output_tokens": 1})]
        got = parse_transcript_usage(_write(tmp_path, rows))
        assert got["model"] == ""
        assert got["model_source"] == "unknown"
        assert got["input_tokens"] == 3, "过滤的是型号,不是用量"

    def test_synthetic_marker_is_shared_not_copied(self):
        """合成行标记只有一份定义 —— 抄成两份就会有一份忘记跳过。"""
        from aiteam.api import session_probe
        from aiteam.services import token_attribution

        assert token_attribution.SYNTHETIC_MODEL is session_probe.SYNTHETIC_MODEL

    def test_corrupt_lines_are_skipped(self, tmp_path):
        p = tmp_path / "agent-x.jsonl"
        p.write_text(
            "not json\n"
            + json.dumps(_assistant("r1", "claude-opus-5", {"input_tokens": 3, "output_tokens": 1}))
            + "\n",
            encoding="utf-8",
        )
        got = parse_transcript_usage(p)
        assert got["input_tokens"] == 3


class TestAliasLedger:
    def test_alias_resolves_with_effective_date(self):
        """别名按生效日期解析——同一个 opus 在不同时期指向不同型号。"""
        entry = resolve_model_alias("opus", at="2026-07-21")
        assert entry is not None
        assert entry["resolved"].startswith("claude-opus")
        assert entry["evidence"], "映射必须带证据来源,不能是拍脑袋"

    def test_unknown_alias_returns_none(self):
        assert resolve_model_alias("no-such-alias", at="2026-07-21") is None

    def test_before_any_window_returns_none(self):
        """生效日期之前不瞎猜。"""
        assert resolve_model_alias("opus", at="2020-01-01") is None

    def test_ledger_is_append_only_shaped(self):
        """每条都必须带生效起点与证据,否则事后无法复原当时跑的是什么。"""
        from aiteam.services.token_attribution import MODEL_ALIAS_LEDGER

        for entry in MODEL_ALIAS_LEDGER:
            assert entry["effective_from"]
            assert entry["evidence"]
            assert entry["resolved"] != entry["alias"]


class TestEndToEndAttribution:
    """演示路径:派子 agent → SubagentStop → token 归因到 agent 行。"""

    async def test_subagent_stop_attributes_tokens_to_agent(self, tmp_path):
        import pytest_asyncio  # noqa: F401

        from aiteam.api.hook_translator import HookTranslator
        from aiteam.storage.connection import close_db
        from aiteam.storage.repository import StorageRepository
        from aiteam.types import EventType

        class _Bus:
            async def emit(self, event_type, source, data, **_kw):
                EventType(event_type)

        repo = StorageRepository(db_url="sqlite+aiosqlite://")
        await repo.init_db()
        try:
            team = await repo.create_team(name="d1-token", mode="coordinate")
            # 生产形态:agents.cc_tool_use_id 存的就是 CC 的 agentId,
            # 也是 transcript 文件名中缀——演示路径的关联键。
            agent = await repo.create_agent(
                team_id=team.id,
                name="worker-1",
                role="worker",
                session_id="sess-1",
                cc_tool_use_id="aworker-1-deadbeef",
            )
            tpath = _write(
                tmp_path,
                [
                    _assistant("r1", "claude-opus-5", {"input_tokens": 12, "output_tokens": 3,
                                                       "cache_read_input_tokens": 900,
                                                       "cache_creation_input_tokens": 40}),
                    _assistant("r1", "claude-opus-5", {"input_tokens": 12, "output_tokens": 77,
                                                       "cache_read_input_tokens": 900,
                                                       "cache_creation_input_tokens": 40}),
                ],
            )
            tr = HookTranslator(repo=repo, event_bus=_Bus())
            await tr.handle_event(
                {
                    "hook_event_name": "SubagentStop",
                    "session_id": "sess-1",
                    "agent_id": "aworker-1-deadbeef",
                    "agent_type": "worker-1",
                    "agent_transcript_path": str(tpath),
                }
            )
            # 子 agent transcript 的解析与记账在后台跑（stop 响应不等它），读前先等它落库
            await tr.drain()
            got = await repo.get_agent(agent.id)
            assert got.output_tokens == 77, "流式快照被逐行相加或未归因"
            assert got.input_tokens == 12
            assert got.cache_read_tokens == 900
            assert got.tokens_measured_at is not None
            # 型号由观测回填，不是写死的
            assert got.model == "claude-opus-5"
        finally:
            await close_db()


class TestStopRacesTheLastWrite:
    """SubagentStop 到达时 CC 还没把最后几行写完（W1）。

    09-15 以来 641 行已测 CC 子 agent 里 142 行库值小于整份重算；135 行文件 mtime 比
    测量完成时刻晚 0.3s 以内。形态就是 wf-afe5126c61：库里 output 22，整份 9,424 ——
    解析读到了本次调用的第一条流式快照，最后一条快照在解析完之后才落盘。
    """

    @staticmethod
    def _request(output_tokens):
        return _assistant("req_01", "claude-opus-5", {
            "input_tokens": 6, "output_tokens": output_tokens,
            "cache_read_input_tokens": 41_000, "cache_creation_input_tokens": 2_300,
        })

    @staticmethod
    async def _setup(tmp_path):
        from aiteam.api.hook_translator import HookTranslator
        from aiteam.storage.repository import StorageRepository
        from aiteam.types import EventType

        class _Bus:
            async def emit(self, event_type, source, data, **_kw):
                EventType(event_type)

        repo = StorageRepository(db_url="sqlite+aiosqlite://")
        await repo.init_db()
        team = await repo.create_team(name="workflow-wf_afe5126c61", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="wf-afe5126c61", role="workflow-subagent",
            session_id="sess-w1", cc_tool_use_id="afe5126c61aaaa",
        )
        tpath = tmp_path / "agent-afe5126c61aaaa.jsonl"
        tpath.write_text(
            json.dumps({"type": "user", "message": {"content": "go"}}) + "\n"
            + json.dumps(TestStopRacesTheLastWrite._request(22)) + "\n",
            encoding="utf-8",
        )
        return repo, agent, tpath, HookTranslator(repo=repo, event_bus=_Bus())

    @staticmethod
    def _stop(tpath):
        return {
            "hook_event_name": "SubagentStop",
            "session_id": "sess-w1",
            "agent_id": "afe5126c61aaaa",
            "agent_type": "workflow-subagent",
            "agent_transcript_path": str(tpath),
        }

    async def test_final_snapshot_written_after_the_parse_is_captured(self, tmp_path, monkeypatch):
        import asyncio
        import threading

        from aiteam.services import token_attribution
        from aiteam.storage.connection import close_db

        repo, agent, tpath, tr = await self._setup(tmp_path)
        first_parse_done = threading.Event()
        real_parse = token_attribution.parse_transcript_usage

        def parse(path):
            try:
                return real_parse(path)
            finally:
                first_parse_done.set()

        monkeypatch.setattr(token_attribution, "parse_transcript_usage", parse)
        try:
            await tr.handle_event(self._stop(tpath))
            # CC flushes the final snapshot right after the first parse has finished
            assert await asyncio.to_thread(first_parse_done.wait, 5)
            with tpath.open("a", encoding="utf-8") as f:
                f.write(json.dumps(self._request(9_424)) + "\n")
            await tr.drain()

            got = await repo.get_agent(agent.id)
            assert got.output_tokens == 9_424, "解析早于最后一条快照落盘，测少了"
            assert got.output_tokens == real_parse(tpath)["output_tokens"]
            assert got.cache_read_tokens == 41_000
        finally:
            await close_db()

    async def test_a_file_that_keeps_growing_is_parsed_a_bounded_number_of_times(
        self, tmp_path, monkeypatch
    ):
        """一直在写的文件（比如被唤醒续跑）不会让作业无限重解析：轮数有上限，库里是最后一轮的值。"""
        from aiteam.api import hook_translator
        from aiteam.services import token_attribution
        from aiteam.storage.connection import close_db

        monkeypatch.setattr(hook_translator, "SUBAGENT_USAGE_SETTLE_SECONDS", 0.05)
        repo, agent, tpath, tr = await self._setup(tmp_path)
        real_parse = token_attribution.parse_transcript_usage
        outputs = iter(range(100, 10_000, 100))
        calls = []

        def parse(path):
            result = real_parse(path)
            calls.append(result["output_tokens"])
            with open(path, "a", encoding="utf-8") as f:  # still being written
                f.write(json.dumps(self._request(next(outputs))) + "\n")
            return result

        monkeypatch.setattr(token_attribution, "parse_transcript_usage", parse)
        try:
            await tr.handle_event(self._stop(tpath))
            await tr.drain()

            assert len(calls) == hook_translator.SUBAGENT_USAGE_MAX_PARSES
            got = await repo.get_agent(agent.id)
            assert got.output_tokens == calls[-1]
        finally:
            await close_db()
