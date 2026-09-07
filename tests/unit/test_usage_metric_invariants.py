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
import inspect
import sqlite3
import sys
from pathlib import Path

from aiteam.services.usage_coverage import classify_unattributed
from aiteam.storage.connection import COLUMNS_TO_ENSURE, _sqlite_migrate
from aiteam.storage.models import AgentActivityModel, AgentModel
from aiteam.storage.repository import _TOKEN_LEDGER_COLUMNS
from aiteam.types import (
    CTX_WATERMARK_METRIC,
    LAYER_AVAILABILITY,
    TOKEN_LAYERS,
    TOKEN_METRIC_LABELS,
    TOKEN_METRIC_SPECS,
    TOKEN_SUBSET_LAYERS,
    Agent,
    AgentActivity,
    HarnessId,
    LayerState,
    TokenMetric,
    TokenSource,
    UnattributedReason,
    UsageCoverageRow,
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


class TestHarnessColumnsStayInSync:
    """Codex P0-1 新增五列 —— 加列必须**四处同步**（types / ORM / 双向转换 / 迁移）。

    这四处任缺其一都不会当场炸，只会以不同的方式说谎：types 缺则 API 少一个字段、
    ORM 缺则写不进去、双向转换缺则写进去读不出来（本仓的实锤：内存对象拼出的响应
    "有值"，跨请求查库才发现漏了）、COLUMNS_TO_ENSURE 缺则**只有既有库**没有这一列
    而新库全绿。所以四处一起断言，缺一即红。
    """

    AGENT_COLUMNS = (
        "harness",
        "harness_version",
        "dispatch_call_id",
        "reasoning_output_tokens",
    )

    def test_agent_columns_exist_in_all_four_places(self):
        ensured = {(t, c) for t, c, _ in COLUMNS_TO_ENSURE}
        for column in self.AGENT_COLUMNS:
            assert column in Agent.model_fields, f"types.Agent 缺 {column}"
            assert hasattr(AgentModel, column), f"AgentModel 缺 {column}"
            assert ("agents", column) in ensured, f"COLUMNS_TO_ENSURE 缺 agents.{column}"

    def test_turn_id_exists_in_all_four_places(self):
        ensured = {(t, c) for t, c, _ in COLUMNS_TO_ENSURE}
        assert "turn_id" in AgentActivity.model_fields
        assert hasattr(AgentActivityModel, "turn_id")
        assert ("agent_activities", "turn_id") in ensured

    def test_agent_columns_round_trip_through_the_orm(self):
        """双向转换：写进去的值必须原样读得回来 —— 缺一个方向就是静默丢字段。"""
        agent = Agent(
            team_id="t1",
            name="w",
            role="worker",
            harness=HarnessId.CODEX,
            harness_version="0.153.0-alpha.5",
            dispatch_call_id="call_abc",
            reasoning_output_tokens=42,
        )
        back = AgentModel.from_pydantic(agent).to_pydantic()
        assert back.harness is HarnessId.CODEX
        assert back.harness_version == "0.153.0-alpha.5"
        assert back.dispatch_call_id == "call_abc"
        assert back.reasoning_output_tokens == 42

    def test_activity_turn_id_round_trips(self):
        activity = AgentActivity(
            agent_id="a1", session_id="s1", tool_name="Bash", turn_id="turn-1"
        )
        assert AgentActivityModel.from_pydantic(activity).to_pydantic().turn_id == "turn-1"

    def test_unset_is_none_not_a_guess(self):
        """观测字段默认留空：未标注 ≠ claude-code，未采集 ≠ 0。"""
        agent = Agent(team_id="t1", name="w", role="worker")
        back = AgentModel.from_pydantic(agent).to_pydantic()
        for column in self.AGENT_COLUMNS:
            assert getattr(agent, column) is None, f"Agent.{column} 默认值不是 None"
            assert getattr(back, column) is None, f"往返后 {column} 不是 None"
        assert AgentActivity(agent_id="a", session_id="s", tool_name="Bash").turn_id is None

    def test_new_columns_carry_no_datetime(self):
        """本批新列一律不带 DATETIME —— 绕开 UTC 平移换算面（旧备份恢复的时钟制式陷阱）。"""
        ddl = {
            (t, c): d
            for t, c, d in COLUMNS_TO_ENSURE
            if (t, c) in {("agents", x) for x in self.AGENT_COLUMNS}
            or (t, c) == ("agent_activities", "turn_id")
        }
        assert len(ddl) == 5
        for key, decl in ddl.items():
            assert "DATETIME" not in decl.upper(), f"{key} 带了时间戳列: {decl}"

    def test_reasoning_layer_is_not_part_of_the_token_ledger(self):
        """``reasoning_output_tokens`` **不进** 保留闸的四层账（r5 §6.9 明令）。

        它是 ``output_tokens`` 的**子集**（TOKEN_SUBSET_LAYERS），不是第五层独立账。
        混进 ``_TOKEN_LEDGER_COLUMNS`` 会让保留闸的判据虚增：一行只有 reasoning 非零
        的记录会被当成"携带了不可重建的账"，与四层可加性直接冲突。
        """
        assert "reasoning_output_tokens" not in _TOKEN_LEDGER_COLUMNS
        assert _TOKEN_LEDGER_COLUMNS == TOKEN_LAYERS

    def test_reasoning_layer_is_declared_a_subset_not_a_fifth_layer(self):
        assert TOKEN_SUBSET_LAYERS == {"reasoning_output_tokens": "output_tokens"}
        assert "reasoning_output_tokens" not in TOKEN_LAYERS
        for subset, parent in TOKEN_SUBSET_LAYERS.items():
            assert parent in TOKEN_LAYERS, f"{subset} 声称属于一个不存在的层 {parent}"

    def test_dispatch_call_id_has_no_unique_constraint(self):
        """刻意不加 UNIQUE：来源链可失落亦可重名，加了会在批量失解时打死入库。"""
        constrained = {
            column.name
            for index in AgentModel.__table__.indexes
            if index.unique
            for column in index.columns
        }
        constrained |= {
            column.name
            for constraint in AgentModel.__table__.constraints
            for column in getattr(constraint, "columns", [])
            if constraint.__class__.__name__ == "UniqueConstraint"
        }
        assert "dispatch_call_id" not in constrained
        assert not any(
            index.name == "uq_agents_agent_key" for index in AgentModel.__table__.indexes
        )


class TestLayerAvailability:
    """四层可加性的第三态 —— 0 有三种含义，呈现面上它们长得一模一样。"""

    def test_table_covers_every_harness_and_layer_with_no_hole(self):
        """空格 = 某个 harness 的某一层没人回答过"能不能测"，而页面照样画 0。"""
        expected = {
            (harness.value, layer)
            for harness in HarnessId
            for layer in (*TOKEN_LAYERS, *TOKEN_SUBSET_LAYERS)
        }
        actual = {(str(harness), layer) for harness, layer in LAYER_AVAILABILITY}
        assert actual == expected, f"缺格: {sorted(expected - actual)}"

    def test_every_state_is_a_declared_member(self):
        assert set(LAYER_AVAILABILITY.values()) <= set(LayerState)

    def test_claude_code_four_layers_are_all_available(self):
        """CC 侧四层俱全 —— 这一条钉住"harness 维度的引入没有改变 CC 的口径"。"""
        for layer in TOKEN_LAYERS:
            assert LAYER_AVAILABILITY[(HarnessId.CLAUDE_CODE, layer)] is LayerState.AVAILABLE

    def test_unverified_state_exists_and_is_not_a_plain_zero(self):
        """Codex 的 cache_creation 是"线上有、从未见过非零"——不得当作已定真的 0。"""
        assert (
            LAYER_AVAILABILITY[(HarnessId.CODEX, "cache_creation_tokens")]
            is LayerState.WIRE_PRESENT_UNVERIFIED
        )


class TestNonNumericObservationColumns:
    """字符串观测列的申报表 —— I12 的双向比对够不着它们，所以另立一张并断言对齐。

    I12 只收 int/float 字段（``check_usage_dimensions._numeric_fields``），把字符串列
    塞进 ``PySurface.fields`` 会被判成"注册表申报了不存在的字段"（实测假红）。但漏报
    同样有代价：一个没人申报过的新列，与一个被删掉却忘了清注册表的旧列，事后长得
    一模一样。故这里做与 I12 同型的**双向**比对。
    """

    MODELS = {
        "Agent": Agent,
        "AgentActivity": AgentActivity,
        "UsageCoverageRow": UsageCoverageRow,
    }

    def test_every_declared_column_actually_exists(self):
        for key in registry.NON_NUMERIC_OBSERVATION_COLUMNS:
            model_name, _, field_name = key.partition(".")
            model = self.MODELS.get(model_name)
            assert model is not None, f"{key}: 申报了一个不认识的模型"
            assert field_name in model.model_fields, f"{key}: 申报了不存在的字段"

    def test_every_declaration_carries_a_reason(self):
        """申报必须具名 —— 沉默的豁免等于没有豁免。"""
        for key, note in registry.NON_NUMERIC_OBSERVATION_COLUMNS.items():
            assert note.strip(), f"{key} 申报了却没写理由"

    def test_the_five_new_columns_are_all_declared(self):
        declared = set(registry.NON_NUMERIC_OBSERVATION_COLUMNS)
        assert {
            "Agent.harness",
            "Agent.harness_version",
            "Agent.dispatch_call_id",
            "UsageCoverageRow.harness",
            "AgentActivity.turn_id",
        } <= declared


class TestClassifierStaysCcShaped:
    """harness 维度引入后，CC 那条路上的分类结果必须**一个字都不变**。

    本期刻意没给 ``classify_unattributed`` 加 harness 形参（r5 规划：C3 本期不动）。
    这条测试把"没加"钉成不变量，而不是靠事后 diff —— 加形参本身不会让任何用例变红，
    真正的危险是"加了形参、给了默认值、然后在默认分支里顺手改了返回值"，那种改动
    在 CC 侧表现为覆盖率抽屉里的类目悄悄换了名字，没有任何东西会报错。
    """

    LEGACY_CODES = {
        UnattributedReason.NO_TRANSCRIPT_PATH.value,
        UnattributedReason.TRANSCRIPT_GONE.value,
        UnattributedReason.NOT_YET_MEASURED.value,
    }

    def test_no_harness_parameter_was_added(self):
        params = set(inspect.signature(classify_unattributed).parameters) - {"transcript_path"}
        assert "harness" not in params, "本期不给分类器加 harness 形参（r5 C3 归 P0-2/P0-3a）"

    def test_cc_branches_return_the_original_three_codes(self, tmp_path: Path):
        """三条既有分支逐一走一遍，返回值必须仍在原来的三码之内。"""
        alive = tmp_path / "t.jsonl"
        alive.write_text("{}\n", encoding="utf-8")
        cases = {
            None: UnattributedReason.NO_TRANSCRIPT_PATH.value,
            "": UnattributedReason.NO_TRANSCRIPT_PATH.value,
            "/definitely/not/here.jsonl": UnattributedReason.TRANSCRIPT_GONE.value,
            str(alive): UnattributedReason.NOT_YET_MEASURED.value,
        }
        for path, expected in cases.items():
            got = classify_unattributed(path)
            assert got == expected, f"{path!r} -> {got}，应为 {expected}"
            assert got in self.LEGACY_CODES

    def test_new_reason_codes_never_reach_the_cc_classifier(self):
        """五个新码只在 harness 侧产出，不得从这个分类器里冒出来。"""
        new_codes = {
            UnattributedReason.SOURCE_LACKS_LAYER.value,
            UnattributedReason.THREAD_EPHEMERAL.value,
            UnattributedReason.NO_ROLLOUT_UNKNOWN.value,
            UnattributedReason.SYSTEM_THREAD.value,
            UnattributedReason.DISPATCH_EDGE_UNRESOLVED.value,
        }
        assert not (self.LEGACY_CODES & new_codes)
        for probe in (None, "", "/nope.jsonl"):
            assert classify_unattributed(probe) not in new_codes
