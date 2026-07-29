"""TokenAttribution 契约测试 —— 归因 v1 §2.5 的三条，属于验收条件不是可选项。

设计把这三条**刻意钉在类型层而不是视图层**，理由已在会议上确认：页面标注是软
约束，三个月后的自己会忽略它。所以这里测的不是"页面上有没有写覆盖率"，而是
"页面物理上能不能拿到一个孤立总量"——答案必须是不能。

三条原文（§2.5）：

1. ``TokenAttribution`` **不存在** ``total`` / ``total_tokens`` 字段或属性；
2. 任何 API 响应体中，token 数值字段必与 ``dispatches_total``、``metric`` 同层
   出现，缺一即测试失败；
3. ``metric`` 无默认值——不标注口径就构造不出对象。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aiteam.types import (
    TOKEN_LAYERS,
    AttributionMethod,
    AttributionScope,
    TokenAttribution,
    TokenMetric,
)


def _sample(**overrides) -> TokenAttribution:
    kwargs = {
        "scope": AttributionScope.PROJECT,
        "scope_id": "p1",
        "metric": TokenMetric.USAGE_SUM,
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_creation_tokens": 3,
        "cache_read_tokens": 4,
        "dispatches_attributed": 1,
        "dispatches_total": 2,
        "unattributed_reasons": {"not_yet_measured": 1},
        "measured_window": None,
        "method": AttributionMethod.TRANSCRIPT_PARSE,
    }
    kwargs.update(overrides)
    return TokenAttribution(**kwargs)


class TestNoBareTotal:
    """契约 ①：拿不到孤立总量 —— 不是"页面不显示"，是结构上没有。"""

    @pytest.mark.parametrize("banned", ["total", "total_tokens"])
    def test_no_total_field(self, banned):
        assert banned not in TokenAttribution.model_fields

    @pytest.mark.parametrize("banned", ["total", "total_tokens"])
    def test_no_total_attribute_or_property(self, banned):
        # 字段没有还不够：加一个 @property 就能绕过第一条断言，而那正是"补个合计
        # 方便前端"最自然的写法。
        assert not hasattr(TokenAttribution, banned)
        assert not hasattr(_sample(), banned)

    def test_no_attribute_sums_the_four_layers(self):
        """任何可访问成员都不得恰好等于四层之和 —— 换个名字叫 sum/aggregate 也不行。"""
        att = _sample()
        four_sum = sum(getattr(att, layer) for layer in TOKEN_LAYERS)
        for name in dir(att):
            # model_* 是 Pydantic 自己的内省接口，从实例上取会告警且与本条无关
            if name.startswith(("_", "model_")) or name in TOKEN_LAYERS:
                continue
            value = getattr(att, name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                assert value != four_sum, (
                    f"{name} 恰好等于四层之和 —— 这就是一个改了名字的 total。"
                    f"实测四层里 95.6% 是 cache_read，只报合计等于只报缓存读取量（§1.2）"
                )

    def test_dump_carries_no_total(self):
        dumped = _sample().model_dump()
        assert "total" not in dumped
        assert "total_tokens" not in dumped
        assert set(TOKEN_LAYERS) <= set(dumped)


class TestMetricHasNoDefault:
    """契约 ③：不标注口径就构造不出对象。"""

    def test_metric_is_required(self):
        assert TokenAttribution.model_fields["metric"].is_required()

    def test_construction_without_metric_fails(self):
        with pytest.raises(ValidationError) as exc:
            TokenAttribution(
                scope=AttributionScope.PROJECT,
                scope_id="p1",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                dispatches_attributed=0,
                dispatches_total=0,
                unattributed_reasons={},
                measured_window=None,
                method=AttributionMethod.TRANSCRIPT_PARSE,
            )
        assert "metric" in str(exc.value)

    def test_denominator_is_required_too(self):
        """分母与口径同级：两者缺一，数值就能被单独渲染。"""
        for field in ("dispatches_total", "dispatches_attributed", "unattributed_reasons"):
            assert TokenAttribution.model_fields[field].is_required()

    def test_frozen(self):
        """构造后不可改 —— 否则可以先合法构造再把分母改成 0。"""
        att = _sample()
        with pytest.raises(ValidationError):
            att.dispatches_total = 0


class TestNumeratorPlusUnattributedEqualsDenominator:
    """分子 + 未归因 == 分母。这条是 R2 的机检形态。

    覆盖率被"优化"成 100% 的做法是把没数据的行移出分母；这条不变量让那种做法
    立刻失衡 —— 移走的行既不在分子里也不在未归因里，等式当场不成立。
    """

    def test_holds_for_a_well_formed_value(self):
        att = _sample(
            dispatches_attributed=3,
            dispatches_total=10,
            unattributed_reasons={"no_transcript_path": 5, "not_yet_measured": 2},
        )
        assert att.dispatches_attributed + sum(att.unattributed_reasons.values()) == (
            att.dispatches_total
        )

    def test_by_design_never_enters_the_reason_dict(self):
        """``by_design`` 说的是工具调用级（52,119 条活动），与派工不是同一个单位。

        它是 §3.4 的第四码、是呈现面上的一行正式标签，但混进这个 dict 就会破坏
        上面那条等式，而且是混量纲 —— 本设计最反对的那件事。
        """
        from aiteam.types import UnattributedReason

        assert UnattributedReason.BY_DESIGN.value == "by_design"
        att = _sample(
            dispatches_attributed=1,
            dispatches_total=2,
            unattributed_reasons={"not_yet_measured": 1},
        )
        assert UnattributedReason.BY_DESIGN.value not in att.unattributed_reasons


class TestRegisteredAsAggregateSurface:
    """阶段 0 的交接项：不登记为 aggregate 面，I13 就该红。"""

    def test_registered_in_usage_surface(self):
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / "scripts"))
        from usage_surface import AGGREGATE_REQUIRED_FIELDS, PY_SURFACES

        surface = next((s for s in PY_SURFACES if s.model == "TokenAttribution"), None)
        assert surface is not None, "TokenAttribution 未登记 —— I13 会红（阶段 0 交接项）"
        assert surface.kind == "aggregate"
        for field in AGGREGATE_REQUIRED_FIELDS:
            assert field in TokenAttribution.model_fields
        assert not surface.coverage_gap, "usage_sum 聚合面不接受 coverage_gap 申报"
