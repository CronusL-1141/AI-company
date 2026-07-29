"""token 用量归因 v1 阶段 0 —— 口径正名与两条机检（I12 量纲白名单 / I13 覆盖率同屏）。

立项时引用的核心数字"1.862 亿 token 已经躺在库里，只差展示层"，口径是错的。
``workflow_agents.tokens`` 取的是最后一条 assistant 消息的四字段和 = 一个**瞬时上下文
水位快照**；``token_attribution.parse_transcript_usage`` 走的是跨 requestId 累加 = 真正
的用量。同一批 agent 实测两者差 5~25 倍（117,800 vs 632,504 … 127,089 vs 3,236,088）。
把它们相加、并列、或放进同一个"总 token"，就是本仓刚在时间戳上栽过的同类事故。

所以本批把「口径」提为与「覆盖率」并列的一等维度，并把两条红线做成机器判据：

* **I12 量纲白名单**：用量呈现面只许出现 token 四层 / 次数 / 时长毫秒 / 百分比。
  用白名单而不是禁用词表——黑名单要穷举所有越界写法才生效，漏一个就破防。
* **I13 覆盖率同屏**：任何 token 数值都必须带口径；聚合面必须与分母、未归因同层返回。
  页面标注是软约束，所以钉在类型层：口径要写在字段旁边，不是只写在注册表里。

规格：docs/token-attribution-v1-design.md §0.2 / §1.1 / §2.5 / §2.6 / §4.4。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

from aiteam.storage.connection import COLUMNS_TO_ENSURE, _sqlite_migrate
from aiteam.storage.models import AgentModel
from aiteam.types import (
    CTX_WATERMARK_METRIC,
    TOKEN_LAYERS,
    TOKEN_METRIC_LABELS,
    TOKEN_METRIC_SPECS,
    Agent,
    TokenMetric,
    TokenSource,
)

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # 先登记再执行：dataclass 装饰器要能按 __module__ 反查回自己的命名空间
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


registry = _load("usage_surface")
dimensions = _load("check_usage_dimensions")
coverage = _load("check_usage_coverage")


class TestMetricVocabulary:
    """口径词表本身就是护栏——它必须是封闭的，且不能悄悄多出一个成员。"""

    def test_two_usage_metrics_only(self):
        """TokenMetric 只覆盖"用量"这一个问题域的两个口径。"""
        assert {m.value for m in TokenMetric} == {"usage_sum", "ctx_last"}

    def test_context_watermark_is_deliberately_outside_the_enum(self):
        """第三口径（上下文水位）刻意不是 TokenMetric 成员，但仍必须可标注。

        它与用量归因无关（服务于 agent 复用治理），可页面上的每个 token 数值都得挂
        口径——包括不参与归因的那些。所以它在词表里、不在枚举里。
        """
        assert CTX_WATERMARK_METRIC not in {m.value for m in TokenMetric}
        assert CTX_WATERMARK_METRIC in TOKEN_METRIC_LABELS
        assert TOKEN_METRIC_LABELS == frozenset({"usage_sum", "ctx_last", CTX_WATERMARK_METRIC})

    def test_every_metric_has_a_definition_and_a_producer(self):
        """口径必须说得清是谁产出的——否则"这个数哪来的"事后无从追。"""
        assert set(TOKEN_METRIC_SPECS) == set(TOKEN_METRIC_LABELS)
        for label, (name, producer, definition) in TOKEN_METRIC_SPECS.items():
            assert name and producer and definition, f"{label} 的口径说明不完整"

    def test_four_layers_are_the_unit_of_presentation(self):
        """四层分列是硬要求：实测 95.6% 的量是 cache_read，只报总量等于只报缓存读。"""
        assert TOKEN_LAYERS == (
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
        )


class TestTokensSourceColumn:
    """新增的唯一一列 —— 加列必须四处同步（types / ORM / 双向转换 / 迁移）。"""

    def test_declared_in_columns_to_ensure(self):
        """项目铁律：ORM 加字段必须同步 COLUMNS_TO_ENSURE，否则既有库永远没有这列。"""
        assert ("agents", "tokens_source") in {(t, c) for t, c, _ in COLUMNS_TO_ENSURE}

    def test_legacy_db_gets_the_column(self, tmp_path: Path):
        """对着一个没有该列的老 agents 表跑迁移，列必须被补出来。"""
        db = str(tmp_path / "legacy.db")
        con = sqlite3.connect(db)
        con.execute(
            """CREATE TABLE agents (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at DATETIME NOT NULL
            )"""
        )
        con.commit()
        con.close()

        _sqlite_migrate(db)

        con = sqlite3.connect(db)
        cols = {r[1] for r in con.execute("PRAGMA table_info(agents)")}
        con.close()
        assert "tokens_source" in cols

    def test_round_trips_through_the_orm(self):
        agent = Agent(
            team_id="t1", name="w", role="worker", tokens_source=TokenSource.TRANSCRIPT
        )
        back = AgentModel.from_pydantic(agent).to_pydantic()
        assert back.tokens_source is TokenSource.TRANSCRIPT

    def test_unmeasured_is_none_not_a_guess(self):
        """NULL = 尚未采集，既不是 transcript 定真也不是别名兜底。no-data ≠ zero。"""
        agent = Agent(team_id="t1", name="w", role="worker")
        assert agent.tokens_source is None
        assert AgentModel.from_pydantic(agent).tokens_source is None
        assert AgentModel.from_pydantic(agent).to_pydantic().tokens_source is None


class TestI12Dimensions:
    def test_repo_is_green(self):
        assert dimensions.main() == 0

    def test_forbidden_words_match_by_word_not_substring(self):
        """子串匹配会把 isPending 读成 "spend"、statusDone 读成 "usd"。

        第一版就是这么写的，一跑 64 条假阳性。天天误报的机检等于没有机检，所以
        改成按 camelCase / snake_case 切词再判。
        """
        for innocent in ("isPending", "statusDone", "agentStatusDone", "statusData",
                         "taskStatusPending", "credential", "discount_free"):
            assert dimensions._forbidden_word(innocent) == "", f"{innocent} 被误判"

    def test_forbidden_words_catch_real_cross_unit_conversions(self):
        """金额/工时这类第五类量纲，无论怎么拼都要被抓住。"""
        for guilty in ("cost_usd", "costUsd", "totalCost", "priceCny", "manHour",
                       "man_hour", "estimated_workdays", "monthlyBudget", "credits_used"):
            assert dimensions._forbidden_word(guilty), f"{guilty} 漏网"

    def test_undeclared_numeric_field_is_red(self, monkeypatch):
        """呈现面新增未申报量纲的数值字段 = 红（白名单只有在申报完整时才封闭）。"""
        surface = registry.PySurface(
            model="WorkflowAgent",
            kind="row",
            fields={"tokens": registry.FieldSpec("token", metric="ctx_last")},
        )
        monkeypatch.setattr(dimensions, "PY_SURFACES", (surface,))
        problems = dimensions.check_python_schema()
        assert any("未申报量纲" in p and "tool_calls" in p for p in problems)

    def test_dimension_outside_the_whitelist_is_red(self, monkeypatch):
        surface = registry.PySurface(
            model="WorkflowAgent",
            kind="row",
            fields={
                "phase_index": registry.FieldSpec(registry.NON_USAGE, note="序号"),
                "tokens": registry.FieldSpec("usd", metric="ctx_last"),
                "tool_calls": registry.FieldSpec("count"),
                "duration_ms": registry.FieldSpec("duration_ms"),
            },
        )
        monkeypatch.setattr(dimensions, "PY_SURFACES", (surface,))
        problems = dimensions.check_python_schema()
        assert any("不在白名单" in p for p in problems)

    def test_unnamed_exemption_is_red(self, monkeypatch):
        """申报为"非用量字段"必须写理由——豁免不能是沉默的。"""
        surface = registry.PySurface(
            model="WorkflowAgent",
            kind="row",
            fields={
                "phase_index": registry.FieldSpec(registry.NON_USAGE),
                "tokens": registry.FieldSpec("token", metric="ctx_last"),
                "tool_calls": registry.FieldSpec("count"),
                "duration_ms": registry.FieldSpec("duration_ms"),
            },
        )
        monkeypatch.setattr(dimensions, "PY_SURFACES", (surface,))
        assert any("豁免必须具名" in p for p in dimensions.check_python_schema())

    def test_unregistered_frontend_surface_is_red(self, monkeypatch):
        """页面上新出现 token 数值却没登记 = 红，否则白名单就有了绕过口。"""
        monkeypatch.setattr(
            dimensions, "FRONTEND_SURFACES", ("dashboard/src/api/workflows.ts",)
        )
        problems, _warnings = dimensions.check_frontend()
        assert any("未登记为用量呈现面" in p for p in problems)


class TestI13Coverage:
    def test_repo_is_green(self):
        assert coverage.main() == 0

    def test_token_field_without_metric_is_red(self, monkeypatch):
        """脱离口径的 token 数没有意义——两个口径差 5~25 倍。"""
        surface = registry.PySurface(
            model="WorkflowAgent",
            kind="row",
            fields={"tokens": registry.FieldSpec("token")},
            coverage_gap={"tokens": "历史遗产"},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, _ = coverage.check_python()
        assert any("未标口径" in p for p in problems)

    def test_metric_only_in_the_registry_is_red(self, monkeypatch):
        """口径必须与字段同屏。只写在注册表里 = 下一个改这行的人看不见。"""
        # Agent 定义块里写着 USAGE_SUM 与 CTX_WATERMARK_METRIC，唯独没有 ctx_last——
        # 拿它冒充 ctx_last，机检必须看得出来"这个口径在源码里根本不存在"。
        surface = registry.PySurface(
            model="Agent",
            kind="row",
            fields={"ctx_tokens": registry.FieldSpec("token", metric="ctx_last")},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, _ = coverage.check_python()
        assert any("只写在注册表里" in p for p in problems)

    def test_aggregate_without_denominator_is_red(self, monkeypatch):
        """聚合面缺分母 = 局部冒充全貌。这一条今天上膛未击发，阶段 2 落地即生效。"""
        surface = registry.PySurface(
            model="WorkflowRun",
            kind="aggregate",
            fields={"total_tokens": registry.FieldSpec("token", metric="ctx_last")},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, _ = coverage.check_python()
        assert any("缺同层覆盖率字段" in p for p in problems)

    def test_usage_sum_aggregate_cannot_declare_a_gap(self, monkeypatch):
        """归因数字的分母没有例外——ctx_last 侧的历史缺口不可类推到 usage_sum。"""
        surface = registry.PySurface(
            model="Agent",
            kind="aggregate",
            fields={"input_tokens": registry.FieldSpec("token", metric="usage_sum")},
            coverage_gap={"input_tokens": "以后再说"},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, _ = coverage.check_python()
        assert any("不接受缺口申报" in p for p in problems)

    def test_row_surface_zero_as_no_data_must_be_declared(self, monkeypatch):
        """非 Optional 的 token 列，0 兼表"未采集"——不改就必须具名申报，不能沉默。"""
        surface = registry.PySurface(
            model="WorkflowAgent",
            kind="row",
            fields={"tokens": registry.FieldSpec("token", metric="ctx_last")},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, _ = coverage.check_python()
        assert any("'未采集'与'真的是 0'" in p for p in problems)

    def test_declared_gaps_stay_visible(self, monkeypatch):
        """申报不是豁免：每次机检都把缺口打印出来，不让它变成默认状态。"""
        surface = registry.PySurface(
            model="WorkflowAgent",
            kind="row",
            fields={"tokens": registry.FieldSpec("token", metric="ctx_last")},
            coverage_gap={"tokens": "ctx_last 侧历史遗产"},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, warnings = coverage.check_python()
        assert not problems
        assert any("已申报覆盖率缺口" in w for w in warnings)

    def test_closed_gap_must_be_cleaned_up(self, monkeypatch):
        """字段已可空却还挂着缺口申报 = 注册表腐烂，同样是红。"""
        surface = registry.PySurface(
            model="WorkflowRun",
            kind="row",
            fields={"live_tokens": registry.FieldSpec("token", metric="ctx_last")},
            coverage_gap={"live_tokens": "早就收口了"},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, _ = coverage.check_python()
        assert any("缺口已收口" in p for p in problems)
