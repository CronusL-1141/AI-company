#!/usr/bin/env python3
"""用量呈现面注册表 —— I12（量纲白名单）与 I13（覆盖率同屏红线）共用的真相源。

为什么要有一张手写的注册表，而不是让机检自己去猜"哪些是呈现面"：

* 猜不准。``tokens`` 这个词在 ingest、parser、repository 里到处都是，那些是**计算面**，
  与"给人看的数字"是两回事。把计算面也扫进来，机检就会变成一个天天误报的东西，而
  天天误报的机检等于没有机检。
* 更重要的是，**注册表本身就是那道闸**。四类量纲的白名单只有在"没有未申报的呈现面"
  这个前提下才是封闭的（§4.4：白名单只需确认四个合法值，新增量纲必须显式过审）。
  所以两个检查都强制双向比对：漏申报 = 红，申报了却不存在 = 也红。

规格：docs/token-attribution-v1-design.md §4.4 / §1.1 / §2.5。
本模块只有数据，没有副作用，供 check_usage_dimensions.py 与 check_usage_coverage.py 导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 量纲白名单 —— 封闭集合，新增必须显式过审（P1）
# ---------------------------------------------------------------------------
# 一切呈现只以 token 用量表达，不做任何跨量纲换算（不换算成金额、不换算成人力工时、
# 不换算成"相当于多少次会议"）。合法量纲穷举如下，共四类：
ALLOWED_DIMENSIONS: dict[str, str] = {
    "token": "token 数（四层分列：input / output / cache_creation / cache_read）",
    "count": "次数（派工数、工具调用数、agent 数……）",
    "duration_ms": "时长毫秒",
    "percent": "百分比",
}

# 非用量的数值字段（序号、字节游标、信任分……）不适用四类量纲，必须**逐个具名豁免**
# 并写明理由——豁免是显式的，不存在"看着不像用量所以自动跳过"这条路。
NON_USAGE = ""

# ---------------------------------------------------------------------------
# 第五类量纲的安全网（次要机制，不是白名单本身）
# ---------------------------------------------------------------------------
# 白名单的封闭性由"注册表必须完整"保证；下面这张词表只是额外一道网，用来在
# 一个**尚未登记**的文件里抓住金额/工时这类换算量纲。它是网，不是判据——判据永远
# 是上面那四个值。命中即红，要放行必须先把该量纲加进 ALLOWED_DIMENSIONS（即过审）。
#
# 匹配按**标识符切词**而非子串：``isPending`` 里藏着 "spend"、``statusDone`` 里藏着
# "usd"，子串匹配一跑就是几十条假阳性，而天天误报的机检等于没有机检。
FORBIDDEN_UNIT_WORDS: frozenset[str] = frozenset({
    "cost", "price", "usd", "cny", "rmb", "dollar", "yuan", "fee",
    "billing", "spend", "spent", "budget", "quota", "money", "credit",
    "manhour", "workday", "manday", "fte",
})


@dataclass(frozen=True)
class FieldSpec:
    """一个数值字段的量纲与口径申报。

    ``dimension`` 为 :data:`NON_USAGE` 时表示"申报为非用量字段"，此时 ``note`` 必填。
    ``metric`` 只对 token 量纲有意义且必填——token 数脱离口径没有意义（§0.2）。
    """

    dimension: str
    metric: str = ""
    note: str = ""


@dataclass(frozen=True)
class PySurface:
    """API schema 侧的一个呈现面（``aiteam.types`` 里的一个 Pydantic 模型）。

    ``kind``:

    * ``row``：一行一条事实的记录面。覆盖率在这里的正确形态是 **no-data 可与 zero
      区分**（列可为 None）。
    * ``aggregate``：跨行聚合面。数值必与分母、未归因同层返回，否则就是"局部冒充
      全貌"（§2.5 / §4.4 红线）。
    """

    model: str
    kind: str
    fields: dict[str, FieldSpec]
    # row 面上"0 兼表未采集"的已知缺口：字段 -> 理由（含何时以何种方式收口）。
    # 申报不是豁免——每次机检都会把它打印出来，让缺口保持可见。
    coverage_gap: dict[str, str] = field(default_factory=dict)
    # aggregate 面必须具备的同层覆盖率字段（分子分母 + 未归因分类）。
    coverage_fields: tuple[str, ...] = ()


# 四个 token 呈现面：三个 row 面（既有）+ 一个 aggregate 面（TokenAttribution，阶段 2）。
PY_SURFACES: tuple[PySurface, ...] = (
    PySurface(
        # 唯一的聚合面。它与三个 row 面的根本区别：row 面一行一条事实，聚合面把多行
        # 揉成一个数——而"揉"这个动作正是局部冒充全貌的发生现场。所以这里的守卫比
        # row 面严：必须与分母、未归因分类同层返回（AGGREGATE_REQUIRED_FIELDS），
        # 且**不允许申报 coverage_gap**——归因数字的分母没有例外。
        model="TokenAttribution",
        kind="aggregate",
        fields={
            # 四层恒为 usage_sum，且这不是"暂时如此"：ctx_last 在结构上进不来。
            # workflow_agents.tokens 在 ingest 时就把四字段加成了一个数，四层分解
            # 从未被保存过，而本结构强制四层分列、刻意无合计字段——要把 ctx_last
            # 塞进来只能挑一层硬塞或凭空造四层。所以 ctx_last 侧只报覆盖率
            # （UsageCoverageRow，零 token 字段），metric 字段仍必填以钉死口径。
            "input_tokens": FieldSpec("token", metric="usage_sum"),
            "output_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_creation_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_read_tokens": FieldSpec("token", metric="usage_sum"),
            "dispatches_attributed": FieldSpec("count", note="分子：本 scope 内已测到用量的派工数"),
            "dispatches_total": FieldSpec("count", note="分母：本 scope 内的派工总数（含没数据的行）"),
            # 原因码 -> 派工数的映射。申报为 count 不是将就：这里要记的正是"它的值与
            # 分母同单位"——分子加上这个 dict 的各项之和必须等于分母（契约测试钉住）。
            # 工具调用级的 by_design 因此进不来：52,119 条活动与派工不是同一个单位。
            "unattributed_reasons": FieldSpec("count", note="未归因派工按原因码分类计数，值与分母同单位"),
        },
        coverage_fields=("dispatches_attributed", "dispatches_total", "unattributed_reasons"),
    ),
    PySurface(
        model="Agent",
        kind="row",
        fields={
            "trust_score": FieldSpec(NON_USAGE, note="信任分 0~1，agent 治理域，与用量无关"),
            "ctx_tokens": FieldSpec("token", metric="ctx_watermark"),
            "ctx_window": FieldSpec("token", metric="ctx_watermark"),
            "ctx_pct": FieldSpec("percent", metric="ctx_watermark"),
            "input_tokens": FieldSpec("token", metric="usage_sum"),
            "output_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_creation_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_read_tokens": FieldSpec("token", metric="usage_sum"),
        },
    ),
    PySurface(
        model="WorkflowRun",
        kind="row",
        fields={
            "planned_agent_count": FieldSpec("count"),
            "dynamic_nodes": FieldSpec("count"),
            "agent_count": FieldSpec("count"),
            "total_tokens": FieldSpec("token", metric="ctx_last"),
            "total_tool_calls": FieldSpec("count"),
            "duration_ms": FieldSpec("duration_ms"),
            "journal_offset": FieldSpec(NON_USAGE, note="journal.jsonl 已消费字节水位，是内部游标不是呈现量纲"),
            "live_tokens": FieldSpec("token", metric="ctx_last"),
        },
        coverage_gap={
            "total_tokens": "非 Optional，0 兼表'未采到'与'真的 0'（ctx_last 侧历史遗产）。"
                            "阶段 0 只正名不改数据；未归因的如实呈现由阶段 2 的覆盖率结构 + 阶段 5 的未归因抽屉承担。",
        },
    ),
    PySurface(
        model="WorkflowAgent",
        kind="row",
        fields={
            "phase_index": FieldSpec(NON_USAGE, note="阶段序号，不是数量"),
            "tokens": FieldSpec("token", metric="ctx_last"),
            "tool_calls": FieldSpec("count"),
            "duration_ms": FieldSpec("duration_ms"),
        },
        coverage_gap={
            "tokens": "非 Optional，0 兼表'未采到'与'真的 0'（实测 3,182 行里 273 行为 0）。"
                      "同上：阶段 0 不改数据，未归因由阶段 2/5 如实呈现。",
        },
    ),
)

# ---------------------------------------------------------------------------
# 前端呈现面
# ---------------------------------------------------------------------------
# 前端没有类型内省可用，只能按文本扫。规则同样双向：dashboard/src 下任何出现 token
# 标识符的文件都必须在这张表里；表里列了却不存在的文件也是红（改名/删文件后注册表
# 腐烂，会让机检的覆盖面无声缩水）。
FRONTEND_SURFACES: tuple[str, ...] = (
    "dashboard/src/api/projects.ts",
    "dashboard/src/api/workflows.ts",
    "dashboard/src/components/shared/ContextWatermarkBar.tsx",
    "dashboard/src/i18n/en.ts",
    "dashboard/src/i18n/zh.ts",
    "dashboard/src/pages/AgentLivePage.tsx",
    "dashboard/src/pages/ProjectDetailPage.tsx",
    "dashboard/src/pages/TeamDetailPage.tsx",
    "dashboard/src/pages/WorkflowsPage.tsx",
    "dashboard/src/types/index.ts",
)

# 前端允许出现的 token 标识符（含展示用的 i18n 键与字面量）→ 量纲。
# 未登记的标识符即红：新加一个 token 数值到页面上，必须在这里说清它是什么量纲。
FRONTEND_IDENTIFIERS: dict[str, str] = {
    "tokens": "token",
    "Tokens": "token",
    "ctx_tokens": "token",
    "total_tokens": "token",
    "totalTokens": "token",
    "live_tokens": "token",
    "colTokens": "token",
    "fmtTokens": "token",
}

# ---------------------------------------------------------------------------
# 覆盖率同屏（I13）用的标记词
# ---------------------------------------------------------------------------
# "任何呈现面上的 token 数值，若其所在 scope 的 C_measure < 100%，必须在同屏同级
# 显示未归因部分"（§4.4 红线）。机检认得的"同屏未归因标注"就是下面这几个词——
# 出现其一即视为该呈现面把未归因这件事说出来了。
COVERAGE_MARKERS: tuple[str, ...] = (
    "dispatches_total", "dispatches_attributed", "unattributed", "coverage",
    "未归因", "覆盖率",
)

# aggregate 面必须同层具备的两样东西：分母，以及未归因的分类计数（§2.5 的
# TokenAttribution 结构）。少任何一样，数值就能脱离分母被单独渲染。
AGGREGATE_REQUIRED_FIELDS: tuple[str, ...] = ("dispatches_total", "unattributed_reasons")

# 前端目前**整体**缺口径徽标：页面上的 token 数字没有一处标出自己是 ctx_last 还是
# usage_sum。这是设计里明确排给阶段 5 的活（§5.3「既有页面不动，v1 只做一件事——
# 给它们加口径徽标」）。在那之前如实登记为缺口，每次机检打印一次，不让它变成默认状态。
FRONTEND_COVERAGE_GAP = (
    "前端 token 数值尚无口径徽标与未归因标注；按 §5.3 由阶段 5 统一补齐"
    "（WorkflowsPage 的 ctx_last 徽标 + /usage 页的覆盖率矩阵与未归因抽屉）。"
)
