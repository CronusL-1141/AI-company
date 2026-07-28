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
            got = await repo.get_agent(agent.id)
            assert got.output_tokens == 77, "流式快照被逐行相加或未归因"
            assert got.input_tokens == 12
            assert got.cache_read_tokens == 900
            assert got.tokens_measured_at is not None
            # 型号由观测回填，不是写死的
            assert got.model == "claude-opus-5"
        finally:
            await close_db()
