"""工具渐进式加载 P1 — alwaysLoad 动态轮换核心逻辑（纯 Python，无 I/O）。

会话启动期给近期高频 MCP 工具打 ``_meta {"anthropic/alwaysLoad": true}`` 豁免
CC 的 defer。取数（一条 SQL）在 repository 层，本模块只负责归一化 + 迟滞防抖 +
硬顶裁决，全部为可单测的纯函数。设计规格见 docs/tool-loading-design.md 的 P1 节。

复杂度封顶（规格红线）：不做衰减曲线 / 实时计数 / 后台重算 / ML 打分。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 客户端侧工具全名前缀（库中 agent_activities.tool_name 带此前缀，裸名去之）。
TOOL_PREFIX = "mcp__ai-team-os__"

# alwaysLoad 常驻上下文成本固定，故门槛从严：硬顶 5，目标 3（用户 2026-07-13 裁定）。
ALWAYSLOAD_HARD_CAP = 5
ALWAYSLOAD_TARGET = 3

# 迟滞防抖：挑战者当期频次需 > 在位者 × 该系数才换入，否则在位者留任（防边界抖动）。
HYSTERESIS_FACTOR = 1.2

# 跨天门槛：入选工具的活动须覆盖 ≥ 该天数，挡单日爆发式调用。
CROSS_DAY_THRESHOLD = 2

# 轮换审计事件类型；每次计算落一行，该行同时是下期迟滞基线（状态与审计合一）。
ROTATION_EVENT_TYPE = "tool.alwaysload.rotation"


@dataclass(frozen=True)
class Candidate:
    """一个合格候选：裸工具名 + 当期频次 + 跨天数。"""

    name: str
    count: int
    days: int


@dataclass
class RotationResult:
    """轮换裁决结果。"""

    tools: list[Candidate] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.tools]


def normalize_tool_name(full_name: str) -> str:
    """去掉 ``mcp__ai-team-os__`` 前缀得裸工具名；无前缀原样返回。"""
    if full_name.startswith(TOOL_PREFIX):
        return full_name[len(TOOL_PREFIX):]
    return full_name


def build_candidates(
    rows: list[tuple[str, int, int]],
    registered: set[str] | None,
) -> list[Candidate]:
    """把 SQL 行归一化成裸名候选，并按当前实际注册工具名过滤（防已删工具入选）。

    Args:
        rows: repository 返回的 ``[(full_name, count, days), ...]``，已按频次降序。
        registered: 当前 MCP server 实际注册的裸工具名集合；``None`` 表示不做注册过滤
            （如手动 curl 未传 registered 参数时）。

    Returns:
        频次降序的候选列表。同一裸名若多次出现（理论上不会）取首个（频次最高）。
    """
    seen: set[str] = set()
    out: list[Candidate] = []
    for full_name, count, days in rows:
        bare = normalize_tool_name(full_name)
        if bare in seen:
            continue
        if registered is not None and bare not in registered:
            continue
        seen.add(bare)
        out.append(Candidate(name=bare, count=int(count), days=int(days)))
    # SQL 已按频次降序，这里再稳定排序一次以防调用方乱序传入。
    out.sort(key=lambda c: c.count, reverse=True)
    return out


def compute_rotation(
    candidates: list[Candidate],
    incumbents: list[str],
    *,
    target: int = ALWAYSLOAD_TARGET,
    hard_cap: int = ALWAYSLOAD_HARD_CAP,
    hysteresis_factor: float = HYSTERESIS_FACTOR,
) -> RotationResult:
    """迟滞轮换裁决（纯 Python）。

    规则：
    - 只有满足跨天门槛的候选（``candidates`` 已在上游过滤）才有资格；上期在位者若
      不在候选中（跌破门槛或已删）自动出局。
    - 目标 ``target`` 个槽位（受 ``hard_cap`` 封顶）。空槽由最强挑战者直接补入，无需迟滞。
    - 槽位占满且全为在位者时，挑战者需当期频次 > 最弱在位者 × ``hysteresis_factor``
      才顶替，否则在位者留任（防边界抖动）。挑战者随频次递减，一旦最强挑战者顶不动即停。
    - 数据不足（合格 < target）不凑数，返回合格的几个或空。

    Args:
        candidates: 频次降序的合格候选。
        incumbents: 上期名单（裸名），来自最近一条轮换审计事件。

    Returns:
        RotationResult：名单 + 换入 / 换出。
    """
    slots = min(target, hard_cap)
    if slots <= 0 or not candidates:
        return RotationResult(tools=[], added=[], removed=list(incumbents))

    incumbent_set = set(incumbents)
    # 在位者按候选内的频次降序（仍合格者才在此列）。
    retained = [c for c in candidates if c.name in incumbent_set]
    # 挑战者按频次降序。
    challengers = [c for c in candidates if c.name not in incumbent_set]

    selected: list[Candidate] = retained[:slots]
    # 空槽由最强挑战者直接补入（不涉及顶替，无需迟滞）。
    ci = 0
    while len(selected) < slots and ci < len(challengers):
        selected.append(challengers[ci])
        ci += 1

    # 迟滞顶替：仅当仍有未用挑战者、且槽位已满时触发。
    # 每轮取最强未用挑战者 vs 当前最弱在位者；顶得动则换，顶不动即停（挑战者只会更弱）。
    while ci < len(challengers) and len(selected) >= slots:
        challenger = challengers[ci]
        incumbents_in = [c for c in selected if c.name in incumbent_set]
        if not incumbents_in:
            break
        weakest = min(incumbents_in, key=lambda c: c.count)
        if challenger.count > weakest.count * hysteresis_factor:
            selected.remove(weakest)
            selected.append(challenger)
            ci += 1
        else:
            break

    # 输出按频次降序稳定呈现。
    selected.sort(key=lambda c: c.count, reverse=True)
    final_names = {c.name for c in selected}
    added = [c.name for c in selected if c.name not in incumbent_set]
    removed = [n for n in incumbents if n not in final_names]
    return RotationResult(tools=selected, added=added, removed=removed)


def parse_registered_param(registered: str) -> set[str] | None:
    """把逗号分隔的 registered 查询参数解析成裸名集合；空串 → ``None``（不过滤）。"""
    if not registered:
        return None
    names = {n.strip() for n in registered.split(",") if n.strip()}
    return names or None
