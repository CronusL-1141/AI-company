#!/usr/bin/env python3
"""I13 — 覆盖率同屏红线机检（token 数值不得脱离口径与分母出现）。

红线（token 用量归因 v1 设计 §4.4 / §2.5 / P2）：

    任何呈现面上的 token 数值，若其所在 scope 的 C_measure < 100%，必须在同屏同级
    显示未归因部分。缺失即视为红线违规。

以及它的前置条件——**任何一个 token 数值，脱离口径标签就没有意义**（§0.2）。本库同时
存在两个正交口径，实测差 5~25 倍；把它们并列或相加，就是刚在时间戳上栽过的同类事故。

四条守卫：

1. **口径必标**：注册表里每个 token 量纲字段都要有口径，且口径 ∈ ``aiteam.types``
   认可的封闭集合（TokenMetric 两个成员 + 不参与归因的上下文水位）。
2. **口径同屏**：口径标签必须**出现在字段旁边**（``types.py`` 里该模型的源码块内），
   不是只写在这张注册表里。页面注释是软约束，三个月后的自己会忽略它；写在字段旁边
   的口径，下一个改这行的人躲不开。
3. **aggregate 面必带分母**：跨行聚合的呈现面必须与 ``dispatches_total`` /
   ``unattributed_reasons`` 同层返回，且 ``usage_sum`` 口径的聚合面**不允许**申报缺口
   —— 归因数字的分母没有例外。
4. **row 面 no-data ≠ zero**：一行一条事实的记录面，未采集必须能与"真的是 0"区分
   （列可为 None）；做不到的必须具名申报缺口，机检每次都把它打印出来。

第 3、4 两条今天分别处在"上膛未击发"与"两处已申报缺口"的状态，这是如实的：
阶段 0 只做口径正名，聚合面由阶段 2 落地，前端徽标由阶段 5 落地。

第三臂 —— Codex 桶（`check_codex_bucket`）
------------------------------------------

前两臂（Python schema / 前端文本）是纯静态的：它们问"这个呈现面**写得对不对**"。
第三臂问的是另一件事——"这条归因链**今天还活着吗**"。写得对但采不到，页面上看到的
是同一个 0。

它按 `check_schema_tables.py` 的成例分两半：静态半边永远跑（含 CI 无库环境），实库
半边只在这台机器有库时跑。凡读库一律 `mode=ro`——那是用户的生产数据。

判据五条：

* **I13 活性**（本期 warn）：`agents` 表中 `session_id` 与 `cc_tool_use_id` **同为
  v7 UUID** 的行数 > 0。为 0 = Codex 归因链断。两列同校验不是啰嗦：单列启发式实测
  误命中 291 行。只在本机有 Codex state 目录时断言——没装 Codex 的机器上"链断"是个
  假问题；而装了 Codex 却从未经 OS 派过子 agent 的机器上它同样为 0，所以本期只报不
  拦，P0-3a 归因链接上之后再升 fail。
* **subagent 桶双列**：生产滚动桶（实时算，必带统计时点）与 golden 冻结桶（夹具
  `G11`）**分列同屏、禁相加**。两桶的总体不是同一个：前者随本机新增会话滚动，后者
  钉死在冻结总体上。相加得到的那个比值不对应任何真实总体，所以这里有一条明写的
  反向断言——合并后的那个数字不许出现在输出里。guardian 免检桶不进任何分母。
* **I-CDX-R3**：`state_5.threads` 的那个"有没有用户事件"列（实测 60/60 恒 0）不得
  作任何判据或分母。这一条**只能是黑名单**——白名单管不住"用了一个不该用的列"，
  它本来就在 schema 里。
* **I-CDX-R6**（本期 warn）：双 v7 行的 `model` 与 `transcript_path` 非空。hook 载荷
  里当场就有这两个键，落库丢了就是白丢。0903 实测基线两列均空 ⇒ 本条上线即响，
  这是刻意的：它是 P0-3a 的红→绿判据。
* **I-CDX-R7**（本期 warn）：派工 matcher 的运行期活性。窗口内命中数为 0 才看三项
  前置——"窗口内没人派工"与"matcher 全线失配"在数据上同形，没有前置就是天天误报。

`harness` 列为 NULL 一律视为**未标注**，不排除：本库在 Codex 接入前写下的每一行都
没有这个维度，按 `harness='codex'` 硬过滤会把它们连同真正的 Codex 行一起筛掉。

用法: python3 scripts/check_usage_coverage.py   （仓库根目录执行）
退出码: 0=全过（申报缺口只警告不拦）, 1=有违规。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import typing
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from usage_surface import (  # noqa: E402  — 必须在 sys.path 就位之后
    AGGREGATE_REQUIRED_FIELDS,
    COVERAGE_MARKERS,
    FRONTEND_COVERAGE_GAP,
    FRONTEND_SURFACES,
    PY_SURFACES,
)

TYPES_PY = ROOT / "src" / "aiteam" / "types.py"

# ---------------------------------------------------------------------------
# 第三臂的常量
# ---------------------------------------------------------------------------
# UUID v7：版本位固定 7，变体位 ∈ {8,9,a,b}。Codex 的 session id 与线程 id 都是 v7，
# CC 的是 v4 —— 这正是"两列同校验"能把两个 harness 分开的原因。
UUID_V7 = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z",
    re.IGNORECASE,
)

# 派工工具名的三面形态（§6.3）：模型面 collaboration.spawn_agent、hook 载荷面
# collaborationspawn_agent（无分隔符）、rollout 记录面 spawn_agent。三面互不相等且
# 存在改名而非仅剥前缀，所以这里不做"统一正则剥前缀"，只做一件事：去掉分隔符后看
# 结尾。CC 的 Agent / Task 落不进来，collaborationwait_agent 也落不进来。
DISPATCH_TOOL_SUFFIX = "spawnagent"

# 活性窗口。取 30 天而非全表：全表口径下只要历史上命中过一次就永远绿，而这条断言
# 存在的全部理由是"上游改名后 matcher 会静默失配"——那恰好发生在最近。
DISPATCH_WINDOW_DAYS = 30

# I-CDX-R3 的黑名单。只此一条，且必须写成黑名单：白名单管不住"用了一个不该用的
# 列"，那个列本来就在 schema 里、类型也对，只是它的值恒 0 而没人知道。
# 扫描面 = 可能形成判据的代码（采集器 / 服务层 / 适配器），不含夹具（那是行转储，
# 是数据不是判据）。本文件自身豁免：判据的名字必须写在判据里，否则这条断言无从表达。
FORBIDDEN_JUDGEMENT_COLUMN = "has_user_event"
R3_SCAN_ROOTS = ("src/aiteam", "scripts", "plugin")
R3_SCAN_SUFFIXES = (".py", ".sh", ".ts", ".tsx")
R3_EXEMPT_FILES = ("scripts/check_usage_coverage.py",)

# 两桶合并的痕迹。这几个词不是为了穷举（黑名单穷举不了），真正拦得住的是
# forbid_bucket_merge 里那条"合并后的比值不许出现"——那个数只有相加才算得出来。
BUCKET_MERGE_WORDS = ("合计", "总计")

CODEX_GOLDEN = ROOT / "tests" / "fixtures" / "codex" / "golden.json"
CODEX_STATE_DB = "state_5.sqlite"


def _model_source(model: str) -> str:
    """取 ``types.py`` 里一个模型的源码块（class 行到下一个顶层 class 之前）。"""
    text = TYPES_PY.read_text(encoding="utf-8")
    match = re.search(rf"^class {re.escape(model)}\(", text, re.MULTILINE)
    if not match:
        return ""
    tail = text[match.start() :]
    nxt = re.search(r"\n(?=class )", tail)
    return tail[: nxt.start()] if nxt else tail


def _is_optional(annotation: object) -> bool:
    return type(None) in typing.get_args(annotation)


def check_python() -> tuple[list[str], list[str]]:
    import aiteam.types as t

    allowed_metrics = t.TOKEN_METRIC_LABELS
    problems: list[str] = []
    warnings: list[str] = []

    for surface in PY_SURFACES:
        model = getattr(t, surface.model, None)
        if model is None:
            problems.append(f"注册表声明的模型 aiteam.types.{surface.model} 不存在")
            continue
        if surface.kind not in ("row", "aggregate"):
            problems.append(f"{surface.model}: kind='{surface.kind}' 非法，只能是 row / aggregate")
            continue
        source = _model_source(surface.model)
        fields = model.model_fields
        token_fields = {n: s for n, s in surface.fields.items() if s.dimension == "token"}

        for name, spec in sorted(token_fields.items()):
            # 守卫 1：口径必标
            if not spec.metric:
                problems.append(
                    f"{surface.model}.{name}: token 数值未标口径 —— "
                    f"脱离口径的 token 数没有意义（usage_sum 与 ctx_last 实测差 5~25 倍）"
                )
                continue
            if spec.metric not in allowed_metrics:
                problems.append(
                    f"{surface.model}.{name}: 口径 '{spec.metric}' 不在 aiteam.types."
                    f"TOKEN_METRIC_LABELS（{sorted(allowed_metrics)}）"
                )
                continue
            # 守卫 2：口径同屏（标注要贴在字段旁边，不能只活在注册表里）。
            # 不区分大小写：写字面量 "ctx_last" 与写符号 ``TokenMetric.CTX_LAST``
            # 同等有效——要的是口径在字段旁边看得见，不是要一种特定拼法。
            if spec.metric.lower() not in source.lower():
                problems.append(
                    f"{surface.model}.{name}: 口径 '{spec.metric}' 只写在注册表里，"
                    f"types.py 的 {surface.model} 定义块内看不到 —— 口径标注必须与字段同屏"
                )

        if surface.kind == "aggregate":
            # 守卫 3：聚合面必带分母与未归因分类
            missing = [f for f in AGGREGATE_REQUIRED_FIELDS if f not in fields]
            if missing:
                problems.append(
                    f"{surface.model}: 聚合呈现面缺同层覆盖率字段 {missing} —— "
                    f"数值与分母、未归因必须同生共死，否则就是局部冒充全貌"
                )
            if surface.coverage_gap and any(s.metric == "usage_sum" for s in token_fields.values()):
                problems.append(
                    f"{surface.model}: usage_sum 聚合面申报了覆盖率缺口 "
                    f"{sorted(surface.coverage_gap)} —— 归因数字的分母不接受缺口申报"
                )
            continue

        # 守卫 4：row 面 no-data ≠ zero
        for name in sorted(token_fields):
            info = fields.get(name)
            if info is None:
                continue  # 字段存在性由 I12 双向比对负责
            optional = _is_optional(info.annotation)
            declared = name in surface.coverage_gap
            if not optional and not declared:
                problems.append(
                    f"{surface.model}.{name}: 非 Optional 的 token 列，0 同时表示"
                    f"'未采集'与'真的是 0' —— 要么改成可空，要么在 coverage_gap 具名申报"
                )
            elif optional and declared:
                problems.append(
                    f"{surface.model}.{name}: 字段已可空却仍挂着 coverage_gap 申报 —— 缺口已收口，请清理注册表"
                )
            elif declared:
                warnings.append(f"已申报覆盖率缺口 {surface.model}.{name}: {surface.coverage_gap[name]}")
    return problems, warnings


def check_frontend() -> tuple[list[str], list[str]]:
    """前端：usage_sum 数值一旦上页面，必须同屏带未归因标注。

    今天这条是上膛未击发——四层用量列还没有出现在任何页面上。阶段 5 的 ``/usage``
    页一落地它就自动生效，那正是最容易"先把数字放上去，覆盖率下个版本再说"的时刻。
    """
    import aiteam.types as t

    problems: list[str] = []
    warnings: list[str] = []
    dash = ROOT / "dashboard" / "src"
    if not dash.is_dir():
        warnings.append("dashboard/src 缺失（未构建环境），前端覆盖率检查跳过")
        return problems, warnings

    layers = re.compile("|".join(re.escape(x) for x in t.TOKEN_LAYERS))
    for path in sorted(dash.rglob("*.ts")) + sorted(dash.rglob("*.tsx")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not layers.search(text):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if not any(m in text for m in COVERAGE_MARKERS):
            problems.append(
                f"{rel}: 呈现了 usage_sum 四层用量却没有任何未归因/覆盖率标注 —— "
                f"同屏红线（需出现其一：{'/'.join(COVERAGE_MARKERS)}）"
            )
    warnings.append(f"前端整体缺口: {FRONTEND_COVERAGE_GAP}")
    return problems, warnings


# ---------------------------------------------------------------------------
# 第三臂：Codex 桶
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Bucket:
    """一个 subagent 可达率桶：分子 / 分母 + 它属于哪个总体。

    ``scope`` 不是装饰。两个桶的分母算的是**不同的总体**（一个随本机会话滚动、一个
    钉死在冻结语料上），把总体写在数字旁边，是"这两个数不能比、更不能加"这句话唯一
    躲不开的表达方式。
    """

    label: str
    numerator: int
    denominator: int
    scope: str

    def line(self) -> str:
        return f"{self.label} {self.numerator}/{self.denominator}（{self.scope}）"


@dataclass(frozen=True)
class DualV7Row:
    """``agents`` 里一条 Codex 形状的行：session 与 cc 两个 id 同为 v7。"""

    agent_id: str
    model: str
    transcript_path: str
    cc_tool_use_id: str


def live_db_path() -> Path:
    """OS 用的那个库（与 ``connection._default_db_url`` 同源）。"""
    override = os.environ.get("AITEAM_DB_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def open_readonly(path: Path) -> sqlite3.Connection:
    """严格只读打开 —— 那是用户的生产数据。"""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def open_codex_state(path: Path, tmp: Path) -> sqlite3.Connection:
    """把 Codex 的 state 库连同 WAL 边文件拷一份再只读打开。

    不直接开原件：一个 WAL 库被只读打开时 SQLite 可能要建 ``-shm``，那就是往用户的
    Codex 目录里写东西。拷贝（1~2 MB）比"大概不会写"可靠。活库拷出来可能是撕开的，
    所以拷完 ``quick_check`` 一次，不过就重来。
    """
    last: Exception | None = None
    for attempt in range(3):
        work = tmp / f"{path.name}.{attempt}"
        shutil.copy2(path, work)
        for suffix in ("-wal", "-shm"):
            side = path.with_name(path.name + suffix)
            if side.exists():
                shutil.copy2(side, work.with_name(work.name + suffix))
        try:
            con = sqlite3.connect(f"file:{work}?mode=ro", uri=True)
            con.execute("pragma quick_check(1)").fetchone()
            return con
        except sqlite3.DatabaseError as exc:  # 活库被拷成了撕开的一份
            last = exc
    raise RuntimeError(f"读不到 {path.name} 的一致副本: {last}")


def is_uuid_v7(value: object) -> bool:
    return isinstance(value, str) and bool(UUID_V7.match(value))


def normalize_tool_name(name: str) -> str:
    """三面工具名归一：去掉分隔符后小写。``collaboration.spawn_agent`` /
    ``collaborationspawn_agent`` / ``spawn_agent`` 都归到同一串尾。"""
    return re.sub(r"[^0-9a-z]", "", name.lower())


def is_dispatch_tool(name: str) -> bool:
    return normalize_tool_name(name).endswith(DISPATCH_TOOL_SUFFIX)


def scan_forbidden_judgement_column() -> list[str]:
    """I-CDX-R3：那个恒 0 的列不得出现在任何可能形成判据的代码里。

    只扫代码位，跳过整行注释——写下"禁用 X"这句话本身不该把机检点着。
    """
    problems: list[str] = []
    for root in R3_SCAN_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in R3_SCAN_SUFFIXES:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in R3_EXEMPT_FILES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if FORBIDDEN_JUDGEMENT_COLUMN not in text:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith(("#", "//", "*", "/*")):
                    continue
                if FORBIDDEN_JUDGEMENT_COLUMN in line:
                    problems.append(
                        f"{rel}:{lineno}: 用了 state_5.threads 那个恒 0 的列作判据 —— "
                        f"实测 60/60 全为 0，拿它当判据或分母得到的是一个恒真的答案"
                        f"（I-CDX-R3）\n    {line.strip()}"
                    )
    return problems


def forbid_bucket_merge(lines: list[str], buckets: list[Bucket]) -> list[str]:
    """两桶禁相加的**反向**断言：合并后的那个比值不许出现在输出里。

    正向写法（"请不要相加"）是注释，注释拦不住人。反向写法能拦住：分子和与分母和
    拼出来的那个串，只有真的把两个总体加到一起才算得出来。
    """
    problems: list[str] = []
    blob = "\n".join(lines)
    for line in lines:
        for word in BUCKET_MERGE_WORDS:
            if word in line:
                problems.append(f"subagent 桶输出出现『{word}』—— 两个总体的数相加没有意义：{line}")
    if len(buckets) == 2 and all(b.denominator > 0 for b in buckets):
        merged = f"{sum(b.numerator for b in buckets)}/{sum(b.denominator for b in buckets)}"
        own = {f"{b.numerator}/{b.denominator}" for b in buckets}
        if merged not in own and merged in blob:
            problems.append(
                f"subagent 桶输出里出现了合并比值 {merged} —— 生产滚动桶与 golden "
                f"冻结桶是两个总体，相加得到的数不对应任何真实分母"
            )
    return problems


def golden_frozen_bucket() -> tuple[Bucket | None, str]:
    """夹具 ``G11`` 的冻结桶，外加"读不出来时那句话"。

    冻结值只能从夹具读，不能在这里抄一份——抄一份就是第二真相源，而两份基线迟早会
    各自漂移。两种"读不出来"要分开：

    * 文件不在 = 精简 checkout，本来就没有夹具，静默跳过。
    * 文件在而键不全 = 有人动了 ``G11``，那要说出来。冻结基线静默缺席，与"冻结基线
      恰好等于生产值"在输出上长得一样。
    """
    if not CODEX_GOLDEN.is_file():
        return None, ""
    rel = (
        CODEX_GOLDEN.relative_to(ROOT).as_posix()
        if CODEX_GOLDEN.is_relative_to(ROOT)
        else CODEX_GOLDEN.name
    )
    try:
        doc = json.loads(CODEX_GOLDEN.read_text(encoding="utf-8"))
        item = next(i for i in doc["items"] if str(i.get("name", "")).startswith("G11"))
        values = item["values"]
        bucket = Bucket(
            "Codex subagent 可达率·golden 冻结桶",
            int(values["os_agents_rows_with_matching_cc_tool_use_id"]),
            int(values["explained_by_thread_spawn"]),
            f"冻结总体，见 {rel} G11；与生产桶禁相加",
        )
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        return None, (
            f"Codex 桶: {rel} 里读不出 G11 冻结桶（{type(exc).__name__}）—— "
            f"冻结基线缺席，本次只报生产滚动桶"
        )
    return bucket, ""


def read_codex_threads(state: Path) -> list[tuple[str, str, str, int]]:
    """``(id, source, thread_source, created_at)``，全量总体（不按 cli_version 钉）。"""
    with tempfile.TemporaryDirectory() as td:
        con = open_codex_state(state, Path(td))
        try:
            return [
                (str(tid), source or "", thread_source or "", int(created or 0))
                for tid, source, thread_source, created in con.execute(
                    "select id, source, thread_source, created_at from threads order by id"
                )
            ]
        finally:
            con.close()


def split_dispatch_threads(
    rows: list[tuple[str, str, str, int]],
) -> tuple[dict[str, int], dict[str, int]]:
    """按 ``source`` 的 JSON 串分出派工线程与免检桶线程，各带 ``created_at``。

    ``subagent.thread_spawn`` = 真的被派出去的子线程，进分母。
    ``subagent.other``（实测全是 guardian）= Codex 自己的内务子会话，结构性不可达，
    **不进任何分母**——把它们算进去，分母凭空变大而分子不可能增加。
    """
    spawn: dict[str, int] = {}
    other: dict[str, int] = {}
    for tid, source, _thread_source, created in rows:
        try:
            parsed = json.loads(source)
        except (TypeError, ValueError):
            continue
        sub = parsed.get("subagent") if isinstance(parsed, dict) else None
        if not isinstance(sub, dict):
            continue
        if "thread_spawn" in sub:
            spawn[tid] = created
        elif "other" in sub:
            other[tid] = created
    return spawn, other


def load_dual_v7_rows(con: sqlite3.Connection, columns: set[str]) -> list[DualV7Row]:
    """``agents`` 里 session 与 cc 两个 id 同为 v7 的行。

    ``harness`` 为 NULL 视为**未标注**、不排除——本库在 Codex 接入之前写下的每一行
    都没有这个维度。硬过滤 ``harness='codex'`` 会把它们连同真正的 Codex 行一起筛掉，
    而那正是这条断言要数的东西。
    """
    where = "length(session_id) = 36 and length(cc_tool_use_id) = 36"
    if "harness" in columns:
        where += " and (harness is null or harness = 'codex')"
    rows = con.execute(
        f"select id, session_id, cc_tool_use_id, model, transcript_path from agents where {where}"
    ).fetchall()
    return [
        DualV7Row(str(aid), model or "", transcript or "", str(cc))
        for aid, session, cc, model, transcript in rows
        if is_uuid_v7(session) and is_uuid_v7(cc)
    ]


def count_dispatch_tool_hits(con: sqlite3.Connection, since: str) -> dict[str, int]:
    """窗口内命中派工工具集的活动行，按工具名分组。

    在 SQL 里不做名字过滤：三面形态互不相等且存在改名，用 LIKE 猜会漏。取窗口内的
    工具名分布再在 Python 里归一化，多读几行换判据可解释。
    """
    hits: dict[str, int] = {}
    for name, n in con.execute(
        "select tool_name, count(*) from agent_activities "
        "where timestamp >= ? and tool_name is not null group by tool_name",
        (since,),
    ):
        if is_dispatch_tool(str(name)):
            hits[str(name)] = int(n)
    return hits


def rollout_mentions_dispatch(home: Path, since_epoch: float) -> bool:
    """窗口内是否有 rollout 记到过派工调用（R7 前置②）。

    只在命中数为 0 时才被调用，所以这趟磁盘扫描不出现在健康机器的常规路径上。
    按 mtime 先筛，再按裸名找——rollout 记录面用的就是裸名。
    """
    for sub in ("sessions", "archived_sessions"):
        base = home / sub
        if not base.is_dir():
            continue
        for path in base.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime < since_epoch:
                    continue
                blob = path.read_bytes()
            except OSError:
                continue
            if b'"spawn_agent"' in blob:
                return True
    return False


def check_codex_bucket() -> tuple[list[str], list[str], list[str]]:
    """第三臂。返回 ``(problems, warnings, notes)``。

    ``notes`` 是"要给人看但不是告警"的行（两个桶的数字）。它们在直接跑本脚本时可见，
    在 ``check_invariants.sh`` 里被 I13 的摘要剥离掉——⚠️ 那条通道留给真的出了事的
    东西，桶的数字不该长年顶着一个警告标。
    """
    from aiteam.clock import to_naive_utc, utc_now

    problems: list[str] = scan_forbidden_judgement_column()
    warnings: list[str] = []
    notes: list[str] = []

    frozen, frozen_note = golden_frozen_bucket()
    if frozen_note:
        notes.append(frozen_note)
    state = codex_home() / CODEX_STATE_DB
    has_codex = state.is_file()

    db = live_db_path()
    if not (db.is_file() and db.stat().st_size > 0):
        notes.append("Codex 桶: 实库缺失（CI 环境），只跑 I-CDX-R3 静态半边")
        if frozen:
            notes.append(frozen.line())
        return problems, warnings, notes

    now = utc_now()
    since = str(to_naive_utc(now - timedelta(days=DISPATCH_WINDOW_DAYS)))
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    window_start_epoch = (now - timedelta(days=DISPATCH_WINDOW_DAYS)).timestamp()
    spawn: dict[str, int] = {}
    other: dict[str, int] = {}
    if has_codex:
        spawn, other = split_dispatch_threads(read_codex_threads(state))

    con = open_readonly(db)
    try:
        columns = {row[1] for row in con.execute("pragma table_info(agents)")}
        dual = load_dual_v7_rows(con, columns)

        # ── I13 活性（本期 warn，P0-3a 升 fail）────────────────────────────
        # 门在"本机有 Codex state 目录"上还不够：另一台开发机可能装了 Codex 却从未经
        # OS 派过子 agent，那里 state 目录在而双 v7 行恒为 0。fail 级会让它在那台机器
        # 上必红，而红的成因不是缺陷——那正是"稳定假红把红线机检整条废掉"的形态。
        if has_codex and not dual:
            warnings.append(
                "Codex 归因链断: agents 表里没有一行 session_id 与 cc_tool_use_id 同为 "
                "v7 UUID —— 本机有 Codex state 目录，说明 Codex 在用，那么派工要么没被"
                "记下要么记丢了一半（两列同校验，单列启发式实测误命中 291 行）；"
                "本期 warn，P0-3a 升 fail"
            )
        if not has_codex:
            notes.append(f"Codex 桶: 本机无 {CODEX_STATE_DB}，活性断言与生产滚动桶跳过")

        # ── subagent 桶双列 ────────────────────────────────────────────────
        buckets: list[Bucket] = []
        if has_codex:
            overlap = set(spawn) & set(other)
            if overlap:
                problems.append(
                    f"免检桶线程 {sorted(overlap)} 同时落进了派工分母 —— guardian 一类的"
                    f"内务子会话结构性不可达，进分母只会让分母凭空变大"
                )
            reached = sum(1 for row in dual if row.cc_tool_use_id in spawn)
            buckets.append(
                Bucket(
                    "Codex subagent 可达率·生产滚动桶",
                    reached,
                    len(spawn),
                    f"统计时点 {stamp}；{CODEX_STATE_DB} 全量总体，"
                    f"免检桶 {len(other)} 个线程不进分母",
                )
            )
        if frozen:
            buckets.append(frozen)
        lines = [b.line() for b in buckets]
        notes.extend(lines)
        problems.extend(forbid_bucket_merge(lines, buckets))

        # ── I-CDX-R6（warn）────────────────────────────────────────────────
        if dual:
            no_model = sum(1 for row in dual if not row.model.strip())
            no_transcript = sum(1 for row in dual if not row.transcript_path.strip())
            if no_model or no_transcript:
                warnings.append(
                    f"I-CDX-R6 hook 侧当场可得字段被丢弃: 双 v7 行 {len(dual)} 条中 "
                    f"model 空 {no_model} 条 / transcript_path 空 {no_transcript} 条 —— "
                    f"hook 载荷里这两个键俱全，落库丢了就是白丢"
                    f"（system.warn(hook_side_field_dropped)；本期 warn，P0-3a 升 fail）"
                )

        # ── I-CDX-R7（warn）────────────────────────────────────────────────
        hits = count_dispatch_tool_hits(con, since)
        total_hits = sum(hits.values())
        if total_hits:
            notes.append(
                f"Codex 派工 matcher 活性: 近 {DISPATCH_WINDOW_DAYS} 天命中 {total_hits} 行"
                f"（{'、'.join(f'{k} x{v}' for k, v in sorted(hits.items()))}）"
            )
        else:
            # 三项前置只在零命中时才算：窗口内没人派工与 matcher 全线失配在数据上同
            # 形，没有前置这条断言就是天天误报，而天天误报的机检等于没有机检。
            recent_spawn = any(c >= window_start_epoch for c in spawn.values())
            recent_dual = bool(
                con.execute(
                    "select count(*) from agents where created_at >= ? "
                    "and length(session_id) = 36 and length(cc_tool_use_id) = 36",
                    (since,),
                ).fetchone()[0]
            )
            recent_rollout = has_codex and rollout_mentions_dispatch(
                codex_home(), window_start_epoch
            )
            if recent_spawn or recent_rollout or recent_dual:
                warnings.append(
                    f"I-CDX-R7 派工 matcher 可能已失配: 近 {DISPATCH_WINDOW_DAYS} 天零命中，"
                    f"但同窗口有派工样本（state_5 新线程={recent_spawn} / "
                    f"rollout 见派工调用={recent_rollout} / agents 新双 v7 行={recent_dual}）"
                    f" —— system.warn(codex_dispatch_matcher_dead)；本期 warn，P0-3a 升 fail"
                )
            else:
                notes.append(
                    f"Codex 派工 matcher 活性: 近 {DISPATCH_WINDOW_DAYS} 天无派工样本，不报警"
                    f"（窗口内没人派工与 matcher 全线失配在数据上同形）"
                )
    finally:
        con.close()
    return problems, warnings, notes


def main() -> int:
    py_problems, py_warnings = check_python()
    fe_problems, fe_warnings = check_frontend()
    cx_problems, cx_warnings, cx_notes = check_codex_bucket()
    problems = py_problems + fe_problems + cx_problems

    for w in py_warnings + fe_warnings + cx_warnings:
        print(f"⚠️  {w}")
    for note in cx_notes:
        print(f"   {note}")
    if problems:
        print("❌ 覆盖率同屏红线违规 —— token 数值不得脱离口径与分母出现:")
        for p in problems:
            print(f"  {p}")
        print(f"\n共 {len(problems)} 处。规格见 docs/token-attribution-v1-design.md §4.4 / §2.5")
        return 1

    token_fields = sum(
        1 for s in PY_SURFACES for spec in s.fields.values() if spec.dimension == "token"
    )
    gaps = sum(len(s.coverage_gap) for s in PY_SURFACES)
    print(
        f"✅ 覆盖率同屏红线通过: {token_fields} 个 token 字段全部带口径且口径与字段同屏 · "
        f"{len(FRONTEND_SURFACES)} 个前端呈现面零裸 usage_sum · 已申报缺口 {gaps} 处（见上方警告）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
