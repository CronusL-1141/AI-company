"""单次实测卡的取数单测 —— 归因 v1 §5.2 ④。

这张卡**显式豁免于覆盖率闸**，所以它是全页面唯一一处"没有分母的数字"。豁免能成立
只因为它交了代价：数据只能来自现场解析一份 transcript。下面每一条测的都是那个代价
还在不在 —— 代价一旦失效，豁免就变成了绕过闸门的后门。

最重要的一条是 :class:`TestNoBareTotal`：解析器内部会算一个 ``total_tokens``（它自己
要用），而呈现面上不许出现任何合计。这两件事之间只隔着一次"顺手透传"。
"""

from __future__ import annotations

import json

import pytest

from aiteam.api import usage_probe
from aiteam.types import TOKEN_LAYERS


def _write_transcript(path, rows) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def _assistant(request_id: str, usage: dict, text: str = "", ts: str = "2026-07-29T00:00:00Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "requestId": request_id,
        "message": {
            "model": "claude-opus-5",
            "usage": usage,
            "content": [{"type": "text", "text": text}] if text else [],
        },
    }


def _user(text: str, ts: str = "2026-07-29T00:00:00Z") -> dict:
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


USAGE = {
    "input_tokens": 10,
    "output_tokens": 20,
    "cache_creation_input_tokens": 30,
    "cache_read_input_tokens": 40,
}


class TestNoBareTotal:
    """呈现面上不许出现合计 —— 解析器内部那个 total_tokens 必须被丢掉。"""

    def test_payload_has_four_layers_and_no_total(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [_user("do it"), _assistant("r1", USAGE, "done")])

        payload = usage_probe.probe_dispatch(str(p))

        assert payload is not None
        for layer in TOKEN_LAYERS:
            assert layer in payload
        # 实测 cache_read 占 95.6%，任何"总量"都是"缓存读取量"的同义词（§1.2）
        assert "total_tokens" not in payload
        assert not any("total" in key for key in payload), f"出现了合计字段: {sorted(payload)}"

    def test_metric_is_always_stamped(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [_assistant("r1", USAGE)])
        payload = usage_probe.probe_dispatch(str(p))
        assert payload["metric"] == "usage_sum", "一个 token 数脱离口径就没有意义"


class TestNoDataIsNotZero:
    """文件不在 ≠ 用了 0 —— 前者必须让卡片整个不出现，而不是显示一排 0。"""

    def test_missing_file_returns_none(self, tmp_path):
        assert usage_probe.probe_dispatch(str(tmp_path / "nope.jsonl")) is None

    def test_transcript_without_usage_rows_returns_none(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [_user("hi"), {"type": "system", "timestamp": "2026-07-29T00:00:00Z"}])
        assert usage_probe.probe_dispatch(str(p)) is None


class TestSameArithmeticAsTheLedger:
    """数值算法只有一份 —— 卡片与台账必须逐字用同一个累加口径。"""

    def test_same_request_id_takes_last_snapshot_not_sum(self, tmp_path):
        # 一次 API 调用产出多条 assistant 行，output 随流式递增；逐行裸加会虚高数倍
        p = tmp_path / "t.jsonl"
        _write_transcript(
            p,
            [
                _assistant("r1", {**USAGE, "output_tokens": 3}),
                _assistant("r1", {**USAGE, "output_tokens": 300}),
                _assistant("r2", USAGE),
            ],
        )
        payload = usage_probe.probe_dispatch(str(p))

        assert payload["output_tokens"] == 320  # 300 + 20，不是 3 + 300 + 20
        assert payload["input_tokens"] == 20  # 两个 requestId 各 10
        assert payload["api_calls"] == 2


class TestSampleExcerpts:
    def test_first_user_and_last_assistant_text_are_excerpted(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(
            p,
            [
                _user("第一条派工 prompt"),
                _assistant("r1", USAGE, "中途结论"),
                _user("工具结果回灌"),
                _assistant("r2", USAGE, "最终产出"),
            ],
        )
        payload = usage_probe.probe_dispatch(str(p))

        assert payload["prompt_excerpt"] == "第一条派工 prompt"
        assert payload["result_excerpt"] == "最终产出"

    def test_excerpts_are_clipped(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [_user("x" * 5000), _assistant("r1", USAGE, "y" * 5000)])
        payload = usage_probe.probe_dispatch(str(p))

        assert len(payload["prompt_excerpt"]) == usage_probe.EXCERPT_LIMIT + 1  # +1 = 省略号
        assert payload["result_excerpt"].endswith("…")

    def test_duration_comes_from_transcript_timestamps(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(
            p,
            [
                _user("go", ts="2026-07-29T00:00:00Z"),
                _assistant("r1", USAGE, "done", ts="2026-07-29T00:02:30Z"),
            ],
        )
        payload = usage_probe.probe_dispatch(str(p))
        assert payload["duration_ms"] == 150_000

    def test_broken_line_does_not_kill_the_whole_sample(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(
            "\n".join([json.dumps(_user("go")), "{not json", json.dumps(_assistant("r1", USAGE, "ok"))]),
            encoding="utf-8",
        )
        payload = usage_probe.probe_dispatch(str(p))
        assert payload["result_excerpt"] == "ok"


class TestModelIsObserved:
    def test_model_read_from_transcript_not_guessed(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [_assistant("r1", USAGE)])
        payload = usage_probe.probe_dispatch(str(p))
        # transcript 里永远是完整型号；模板里的 `model: opus` 是层级别名，两者不可互换
        assert payload["model"] == "claude-opus-5"
        assert payload["model_source"] == "transcript"

    def test_synthetic_model_is_not_reported_as_a_real_model(self, tmp_path):
        p = tmp_path / "t.jsonl"
        rows = [_assistant("r1", USAGE)]
        rows[0]["message"]["model"] = "<synthetic>"
        _write_transcript(p, rows)
        payload = usage_probe.probe_dispatch(str(p))
        assert payload["model"] == ""
        assert payload["model_source"] == "unknown"


@pytest.mark.parametrize("field", ["prompt_excerpt", "result_excerpt", "duration_ms", "transcript_path"])
def test_card_fields_present(tmp_path, field):
    p = tmp_path / "t.jsonl"
    _write_transcript(p, [_user("go"), _assistant("r1", USAGE, "done")])
    assert field in usage_probe.probe_dispatch(str(p))
