"""inject_subagent_context P0 重接单测（2026-07-14 审计修复）。

核心不变量：
- 触发键直接来自派单 prompt（payload 优先，transcript 首条 user 消息兜底）；
- task_id 只认显式样式，不把 repo_id/deep_review_id 的裸 uuid 误认成任务；
- 死掉的 pipeline 检测已删除（不再存在 _fetch_pipeline_context）。
"""

from __future__ import annotations

import importlib
import json

inject = importlib.import_module("aiteam.hooks.inject_subagent_context")

UUID_A = "1279bdd9-35da-4b20-b44d-7de60282f1c0"
UUID_B = "7f38dbbf-7ea7-48ef-aff9-db5ba53165fa"


class TestExtractTaskContext:
    def test_explicit_task_id_from_prompt(self):
        payload = {"prompt": f"完成后调用 task_memo_add(task_id={UUID_A}) 回写进展"}
        task_id, prompt = inject._extract_task_context(payload)
        assert task_id == UUID_A
        assert "task_memo_add" in prompt

    def test_chinese_task_id_marker(self):
        payload = {"prompt": f"你的任务 ID: {UUID_A}，请先 task_memo_read"}
        task_id, _ = inject._extract_task_context(payload)
        assert task_id == UUID_A

    def test_bare_uuid_not_mistaken_as_task(self):
        # repo_id / deep_review_id 的裸 uuid 不能被当成任务键
        payload = {"prompt": f"repo_id={UUID_B} 的仓库请做浅扫总结"}
        task_id, _ = inject._extract_task_context(payload)
        assert task_id == ""

    def test_empty_payload_gives_empty_context(self):
        task_id, prompt = inject._extract_task_context({})
        assert task_id == ""
        assert prompt == ""

    def test_parent_transcript_is_never_read(self, tmp_path):
        """批 3 ③（改写自旧 test_transcript_fallback，旧断言锁的是错误行为）。

        SubagentStart 载荷里的 transcript_path 指向**父会话**（Leader 的）
        transcript：其首条 user 消息是「用户对 Leader 说的话」，不是本次派单
        prompt。旧兜底拿它当派单文本 → task_id 提取 / memo 拉取 / 模式检索三段
        动态注入全按错对象工作（会把用户随口提到的任务号注到无关 agent 身上）。
        现在彻底不读 transcript。
        """
        transcript = tmp_path / "parent-session.jsonl"
        rec = {
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"总任务 {UUID_A} 已上墙，开始实施"}
                ],
            }
        }
        transcript.write_text(json.dumps(rec) + "\n", encoding="utf-8")
        payload = {"prompt": "", "transcript_path": str(transcript)}
        task_id, prompt = inject._extract_task_context(payload)
        assert task_id == ""
        assert UUID_A not in prompt
        assert "开始实施" not in prompt

    def test_transcript_reader_removed(self):
        assert not hasattr(inject, "_first_user_message")

    def test_missing_transcript_is_silent(self):
        payload = {"transcript_path": "/nonexistent/agent.jsonl"}
        task_id, prompt = inject._extract_task_context(payload)
        assert task_id == ""

    def test_description_used_when_prompt_absent(self):
        payload = {"description": f"修 bug，任务ID: {UUID_A}"}
        task_id, prompt = inject._extract_task_context(payload)
        assert task_id == UUID_A
        assert "修 bug" in prompt

    def test_agent_type_and_cwd_are_the_retrieval_key(self):
        """无派单文本时，检索键 = agent_type + cwd（模式检索仍有意义的最小信号）。"""
        payload = {"agent_type": "testing-bug-fixer", "cwd": "/repo/ai-team-os"}
        task_id, prompt = inject._extract_task_context(payload)
        assert task_id == ""
        assert "testing-bug-fixer" in prompt
        assert "/repo/ai-team-os" in prompt


class TestDeadPipelineRemoved:
    def test_pipeline_fetcher_gone(self):
        assert not hasattr(inject, "_fetch_pipeline_context")

    def test_no_retired_tool_mentions_in_source(self):
        import inspect

        src = inspect.getsource(inject)
        assert "pipeline_advance" not in src

    def test_report_format_block_removed(self):
        import inspect

        src = inspect.getsource(inject)
        assert "## 汇报格式" not in src
