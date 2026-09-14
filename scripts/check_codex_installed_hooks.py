#!/usr/bin/env python3
"""只读比对：Codex 侧已安装的 hook 副本 vs 仓内源码。

这些副本是手工 copy 或安装器放下的，之后再没有更新路径：仓里改了源码，装侧不会跟着动；
装侧被就地改过，仓里也看不见。漂移不会报错、不会崩，只会让 Codex 会话在一份没人记得
的旧规则下干活，事后无法分辨当时跑的是哪一版 —— 唯一的解法是先把漂移看见。

安装面不止一个目录（`--installed` 可重复给，默认下面这两个）：
- `<codex-home>/hooks/ai-team-os/`：早期手工 copy 的 CC 脚本镜像，来路混杂，与源码或
  适配器任一逐字节相等都算同源；
- `<codex-home>/hooks/ai-team-os-observer/`：Codex 适配器的安装位，内容来自
  `plugin/harness/codex/hooks/`，**只认适配器副本当基准** —— 它们本来就是适配器，碰巧
  等于 `src/aiteam/hooks/` 里的同名文件不能算对上了（`hook_core.py` 两处都有且内容不同，
  放宽成"任一相等"就会把真漂移洗成同源）。

被比对的两处仓内源 + 一份清单：
- 源码：  `<repo>/src/aiteam/hooks/`
- 适配器：`<repo>/plugin/harness/codex/hooks/`（`--repo-root` 可覆盖，默认本脚本所在仓）
- 注册清单：`<codex-home>/hooks.json`，用来回答"这个副本到底有没有人在调它"。清单里的
  command 写的是绝对路径，按路径对到具体某个安装目录 —— 同名文件装在两个目录里各算各
  的注册，不会互相顶替。

状态四值：`同源`（与该目录的基准逐字节相等）/ `漂移`（基准侧有对应文件但不等）/
`仅装侧`（基准侧没有，来路不明）/ `仅源侧`（仓里有、所有安装目录都没有）。
`装了没人调` 只数**装侧存在、清单里没有调用者、且不是已登记配套模块**的行，三个限定各有
各的虚报要挡：仅源侧的行天然没有注册，算进来会把"该装没装"和"装了没人调"混成一个数；
配套模块（`hook_core.py` 这种被入口 import 的实现件）本来就不该出现在清单里，把它们算
成漏注册会让这个数常年不为零，人看两次就不看了。登记从仓里读，不在本脚本里另立一份名单：
- `plugin/harness/codex/surface.py` 的 `CODEX_SUPPORT_MODULES` —— 适配器入口的伴生模块
  （I20 与 `scripts/check_codex_isolation.py` 读的是同一份）；
- `scripts/check_codex_isolation.py` 的 `VERBATIM_COPIES` —— 两个 harness 共用核心的逐字节
  副本。`hook_core.py` 不在 surface.py 里是对的：它不是适配器私有件，是 I1 三方对钉的共用核心。
两处登记各自独立降级：读不到就当空 tuple，头部打一行说明，这一轮的计数会偏大 —— 宁可报多
也不能因为读不到登记就悄悄少报。
表里的 mtime 一律 UTC（本库只有一个时钟，I11 红线），列头已标注，别按本地墙钟读。
mtime 只是线索不是判据：git checkout / worktree 新建会把源码侧 mtime 全刷成签出时刻，
看着像"今天刚改"其实没动过 —— 判漂移只认 sha8，mtime 用来推谁比谁旧。
退出码：`漂移` 或 `仅装侧` ≥1 → 1；否则 0（`未注册` 不判红，它是待人裁决的线索）。
所有安装目录都不存在 → 打一行说明后退出 0（这台机器没接 Codex，不算红）。
清单缺失或坏 JSON → 注册列全标 `清单不可读`，不中断。

本脚本只读，不写盘，尤其不会"顺手同步"装侧文件，三条理由：
1. 2026-09-07 裁定：OS 是观测层，不写用户 harness 自己的配置目录 —— 机检的职责是报告
   漂移，不是掩盖漂移；
2. 这批脚本是按 CC 的 payload 形状写的，在 Codex 下并未逐项验证，照搬覆盖等于把未验的
   行为静默推上生产；
3. `ai-team-os/` 这个镜像本身是过渡态，将随 `plugin/harness/codex/` 适配器接管而退役，
   给一条注定要拆的路修自动同步是负资产。

同步动作要不要做、什么时候做，由人看完这张表再定。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shlex
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# 唯一时钟（I11 红线）：mtime 一律按 UTC 呈现，绝不贴宿主本地偏移。该模块只依赖标准库，
# 从 ROOT 取而不是 --repo-root，被比对的那个仓换成谁都不影响本脚本自己的时钟。
# 只读到底：连解释器的 .pyc 副产物都不许落盘，导入完就把开关还回去，不留全局副作用。
_PRIOR_BYTECODE_FLAG = sys.dont_write_bytecode
sys.dont_write_bytecode = True
try:
    from aiteam.clock import from_timestamp  # noqa: E402  — 必须在 sys.path 就位之后
finally:
    sys.dont_write_bytecode = _PRIOR_BYTECODE_FLAG

DEFAULT_CODEX_HOME = Path.home() / ".codex"
# 默认扫两个安装目录。顺序即表里同名文件的呈现顺序，别随手调。
INSTALLED_RELPATHS = (
    ("hooks", "ai-team-os"),
    ("hooks", "ai-team-os-observer"),
)
MANIFEST_RELPATH = ("hooks.json",)
SOURCE_RELPATH = ("src", "aiteam", "hooks")
ADAPTER_RELPATH = ("plugin", "harness", "codex", "hooks")

# 配套模块登记的两处出处。写成 (相对路径, 常量名)，头部提示直接引用，人一眼知道去哪补。
SURFACE_RELPATH = ("plugin", "harness", "codex", "surface.py")
SURFACE_ATTR = "CODEX_SUPPORT_MODULES"
ISOLATION_RELPATH = ("scripts", "check_codex_isolation.py")
ISOLATION_ATTR = "VERBATIM_COPIES"

# 只有这两类后缀算可执行 hook 脚本；`.bak-*` 之类的历史残片与 __pycache__ 不入表。
SCRIPT_SUFFIXES = (".py", ".sh")

SIDE_SOURCE = "source"
SIDE_ADAPTER = "adapter"

# 哪些安装目录只认适配器当基准（按目录名认，`--installed` 给别的路径同样按名判）。
ADAPTER_ONLY_DIR_NAMES = frozenset({"ai-team-os-observer"})
DEFAULT_BASELINES = (SIDE_SOURCE, SIDE_ADAPTER)
ADAPTER_ONLY_BASELINES = (SIDE_ADAPTER,)

BASELINE_LABELS = {
    DEFAULT_BASELINES: "源码或适配器",
    ADAPTER_ONLY_BASELINES: "仅适配器",
}

STATUS_SAME = "同源"
STATUS_DRIFT = "漂移"
STATUS_INSTALLED_ONLY = "仅装侧"
STATUS_SOURCE_ONLY = "仅源侧"

REG_NONE = "未注册"
REG_UNREADABLE = "清单不可读"
REG_COMPANION = "配套模块"
REG_PREFIX = "已注册: "

MISSING = "-"


@dataclass(frozen=True)
class Companions:
    """从仓里读回来的配套模块登记，外加读的过程中出了什么问题。"""

    names: frozenset[str]
    sources: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Row:
    """表里的一行 —— 一个文件名在某个安装目录与两处仓内源的状态。

    `install_label` 为 MISSING 表示这是仅源侧的行，没有落在任何安装目录里。
    """

    name: str
    install_label: str
    installed_sha: str
    source_sha: str
    adapter_sha: str
    installed_mtime: str
    source_mtime: str
    status: str
    registration: str

    @property
    def is_installed(self) -> bool:
        return self.install_label != MISSING


def baselines_for(directory: Path) -> tuple[str, ...]:
    """这个安装目录该拿哪一侧当基准。"""
    if directory.name in ADAPTER_ONLY_DIR_NAMES:
        return ADAPTER_ONLY_BASELINES
    return DEFAULT_BASELINES


def _import_module(path: Path, alias: str) -> ModuleType | None:
    """把仓里的一个 .py 当模块加载，只为读它的常量。读不动一律返回 None 让调用方降级。

    `dont_write_bytecode` 必须关掉再开回来：本脚本对外承诺只读，往被比对的仓里落一个
    `__pycache__` 也是写盘，单测的"跑完文件集合一个没变"会当场抓住。
    """
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location(alias, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
    except (OSError, ValueError, ImportError):
        return None
    prior = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 — 别人的脚本怎么炸都不该带塌一个只读报告
        return None
    finally:
        sys.dont_write_bytecode = prior
    return module


def _valid_companion_names(raw: list[str]) -> list[str]:
    """只收同目录下的裸脚本名 —— 带路径或别的后缀说明登记写法变了，宁可不收也不瞎猜。"""
    return [
        name
        for name in raw
        if isinstance(name, str) and Path(name).name == name and Path(name).suffix in SCRIPT_SUFFIXES
    ]


def _names_from_surface(module: ModuleType) -> list[str]:
    raw = getattr(module, SURFACE_ATTR, ())
    if not isinstance(raw, tuple):
        return []
    return _valid_companion_names(list(raw))


def _names_from_isolation(module: ModuleType) -> list[str]:
    """VERBATIM_COPIES 的键是相对适配器目录的路径，只取落在 hooks/ 下的那些。"""
    raw = getattr(module, ISOLATION_ATTR, {})
    if not isinstance(raw, dict):
        return []
    hooks_dirname = ADAPTER_RELPATH[-1]
    candidates = [
        Path(key).name
        for key in raw
        if isinstance(key, (str, Path)) and Path(key).parent == Path(hooks_dirname)
    ]
    return _valid_companion_names(candidates)


def load_companions(repo_root: Path) -> Companions:
    """读两处配套模块登记。任一读不到就当空，并留一条告警说明这轮的计数会偏大。"""
    names: set[str] = set()
    sources: list[str] = []
    warnings: list[str] = []
    plan = (
        (SURFACE_RELPATH, SURFACE_ATTR, "surface", _names_from_surface),
        (ISOLATION_RELPATH, ISOLATION_ATTR, "isolation", _names_from_isolation),
    )
    for relpath, attr, alias, extract in plan:
        path = repo_root.joinpath(*relpath)
        module = _import_module(path, f"_codex_registry_{alias}")
        if module is None:
            warnings.append(f"读不到 {path} 的 {attr}，其登记的配套模块这轮会被当成未注册")
            continue
        found = extract(module)
        names.update(found)
        sources.append(f"{Path(*relpath).as_posix()}:{attr} {len(found)}")
    return Companions(frozenset(names), tuple(sources), tuple(warnings))


def read_bytes(path: Path | None) -> bytes | None:
    """读整个文件；不存在或读不动都返回 None（读不动等同于没有，报告里照样看得出来）。"""
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def sha8(data: bytes | None) -> str:
    return MISSING if data is None else hashlib.sha256(data).hexdigest()[:8]


def mtime_text(path: Path | None) -> str:
    """文件 mtime，按 UTC 呈现（表头已标注）—— 本库只有一个时钟。"""
    if path is None:
        return MISSING
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return MISSING
    return from_timestamp(stamp).strftime("%Y-%m-%d %H:%M")


def collect_scripts(directory: Path) -> dict[str, Path]:
    """目录下的一层 hook 脚本，按文件名索引。子目录（含 __pycache__）一律不进。"""
    found: dict[str, Path] = {}
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.is_file() and entry.suffix in SCRIPT_SUFFIXES:
            found[entry.name] = entry
    return found


def dedupe_dirs(directories: list[Path]) -> list[Path]:
    """按解析后的真实路径去重，保留首次出现的写法与顺序。"""
    seen: set[Path] = set()
    kept: list[Path] = []
    for directory in directories:
        key = directory.resolve()
        if key in seen:
            continue
        seen.add(key)
        kept.append(directory)
    return kept


def label_dirs(directories: list[Path]) -> dict[Path, str]:
    """给每个安装目录取表里用的短名。basename 撞名时全体退回完整路径，不然两行长得一样。"""
    names = [d.name for d in directories]
    unique = len(set(names)) == len(names)
    return {d: (d.name if unique else str(d)) for d in directories}


def parse_manifest(
    manifest: Path, installed_dirs: list[Path]
) -> tuple[dict[tuple[Path, str], list[str]], bool, list[str]]:
    """从 hooks.json 里刨出"哪个已安装脚本被哪些事件调用"。

    清单结构：``{"hooks": {"<事件名>": [{"hooks": [{"command": "<shell 串>"}]}]}}``。
    command 是整条命令行（解释器 + 脚本 + 参数），用 shlex 拆开后取以 .py/.sh 结尾、
    且父目录解析后正好落在某个安装目录上的 token 当脚本路径。按绝对路径归属，所以同名
    文件装在两个目录里各记各的注册。一个脚本注册多个事件时全部收集，按清单顺序去重。

    返回 ((安装目录解析路径, 文件名) -> 事件名列表, 清单是否可读, 解析告警)。
    """
    registry: dict[tuple[Path, str], list[str]] = {}
    notes: list[str] = []
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, False, notes

    hooks = raw.get("hooks") if isinstance(raw, dict) else None
    if not isinstance(hooks, dict):
        return {}, False, notes

    target_dirs = {d.resolve() for d in installed_dirs}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            notes.append(f"事件 {event} 的取值不是数组，已跳过")
            continue
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                continue
            for entry in entries:
                command = entry.get("command") if isinstance(entry, dict) else None
                if not isinstance(command, str):
                    continue
                try:
                    tokens = shlex.split(command)
                except ValueError:
                    notes.append(f"事件 {event} 有一条命令引号不配对，无法拆解，已跳过")
                    continue
                for token in tokens:
                    candidate = Path(token)
                    if candidate.suffix not in SCRIPT_SUFFIXES:
                        continue
                    parent = candidate.resolve().parent
                    if parent not in target_dirs:
                        continue
                    events = registry.setdefault((parent, candidate.name), [])
                    if str(event) not in events:
                        events.append(str(event))
    return registry, True, notes


def classify(
    installed: bytes | None,
    source: bytes | None,
    adapter: bytes | None,
    baselines: tuple[str, ...] = DEFAULT_BASELINES,
) -> str:
    """定状态。`baselines` 决定哪几侧算权威副本 —— 只跟这几侧比，别的相等不算数。"""
    if installed is None:
        return STATUS_SOURCE_ONLY
    candidates = [
        data
        for side, data in ((SIDE_SOURCE, source), (SIDE_ADAPTER, adapter))
        if side in baselines and data is not None
    ]
    if not candidates:
        return STATUS_INSTALLED_ONLY
    if any(installed == candidate for candidate in candidates):
        return STATUS_SAME
    return STATUS_DRIFT


def build_rows(
    installed_dirs: list[Path],
    source_dir: Path,
    adapter_dir: Path,
    registry: dict[tuple[Path, str], list[str]],
    manifest_ok: bool,
    labels: dict[Path, str],
    companions: frozenset[str] = frozenset(),
) -> list[Row]:
    source = collect_scripts(source_dir)
    adapter = collect_scripts(adapter_dir)
    per_dir = [(directory, collect_scripts(directory)) for directory in installed_dirs]
    installed_names = {name for _, files in per_dir for name in files}

    def registration_for(directory: Path | None, name: str) -> str:
        if directory is None:
            # 仅源侧的行没有装侧文件可谈注册，标 N/A 而不是"未注册"，免得虚报。
            return MISSING
        if not manifest_ok:
            return REG_UNREADABLE
        events = registry.get((directory.resolve(), name))
        if events:
            # 清单里真有人调它，就照实印 —— 实际调用面永远盖过按名分类的推断。
            return REG_PREFIX + ",".join(events)
        if name in companions:
            return REG_COMPANION
        return REG_NONE

    def make_row(
        name: str,
        directory: Path | None,
        installed_path: Path | None,
        baselines: tuple[str, ...],
    ) -> Row:
        source_path = source.get(name)
        adapter_path = adapter.get(name)
        installed_data = read_bytes(installed_path)
        source_data = read_bytes(source_path)
        adapter_data = read_bytes(adapter_path)
        return Row(
            name=name,
            install_label=MISSING if directory is None else labels[directory],
            installed_sha=sha8(installed_data),
            source_sha=sha8(source_data),
            adapter_sha=sha8(adapter_data),
            installed_mtime=mtime_text(installed_path),
            source_mtime=mtime_text(source_path),
            status=classify(installed_data, source_data, adapter_data, baselines),
            registration=registration_for(directory, name),
        )

    rows: list[Row] = []
    for directory, files in per_dir:
        baselines = baselines_for(directory)
        for name in sorted(files):
            rows.append(make_row(name, directory, files[name], baselines))
    # 仅源侧只对"所有安装目录都没有"的文件名成立，否则每多一个安装目录就多一行假的仅源侧。
    for name in sorted((set(source) | set(adapter)) - installed_names):
        rows.append(make_row(name, None, None, DEFAULT_BASELINES))

    rows.sort(key=lambda r: (r.name, r.install_label))
    return rows


def display_width(text: str) -> int:
    """中文按两格宽算，否则表格会错位到没法读。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def render(rows: list[Row]) -> list[str]:
    header = (
        "文件",
        "安装位",
        "装 sha8",
        "源 sha8",
        "适配 sha8",
        "装 mtime(UTC)",
        "源 mtime(UTC)",
        "状态",
        "注册",
    )
    cells = [header] + [
        (
            r.name,
            r.install_label,
            r.installed_sha,
            r.source_sha,
            r.adapter_sha,
            r.installed_mtime,
            r.source_mtime,
            r.status,
            r.registration,
        )
        for r in rows
    ]
    widths = [max(display_width(row[i]) for row in cells) for i in range(len(header))]

    lines = [
        "  ".join(pad(cell, widths[i]) for i, cell in enumerate(header)).rstrip(),
        "  ".join("-" * widths[i] for i in range(len(header))),
    ]
    for row in cells[1:]:
        lines.append("  ".join(pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip())
    return lines


def count_unregistered(rows: list[Row]) -> int:
    """只数装侧存在、清单无调用者、且不是已登记配套模块的行。

    仅源侧的行天然无注册，配套模块本来就不进清单 —— 两类都不算"装了没人调"。
    """
    return sum(1 for r in rows if r.is_installed and r.registration == REG_NONE)


def summarize(rows: list[Row]) -> str:
    def count(status: str) -> int:
        return sum(1 for r in rows if r.status == status)

    return (
        f"汇总: {len(rows)} 行 / 同源 {count(STATUS_SAME)} / 漂移 {count(STATUS_DRIFT)}"
        f" / 仅装侧 {count(STATUS_INSTALLED_ONLY)} / 仅源侧 {count(STATUS_SOURCE_ONLY)}"
        f" / 装了没人调 {count_unregistered(rows)}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读比对 Codex 侧已安装 hook 副本与仓内源码的漂移（不修改任何文件）",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=DEFAULT_CODEX_HOME,
        help="Codex 配置根目录，默认 ~/.codex",
    )
    parser.add_argument(
        "--installed",
        type=Path,
        action="append",
        default=None,
        metavar="DIR",
        help=(
            "已安装 hook 目录，可重复给。默认 <codex-home>/hooks/ai-team-os 与 "
            "<codex-home>/hooks/ai-team-os-observer"
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=ROOT,
        help="仓库根目录，默认本脚本所在仓",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    codex_home: Path = args.codex_home
    repo_root: Path = args.repo_root

    if args.installed:
        configured = list(args.installed)
    else:
        configured = [codex_home.joinpath(*rel) for rel in INSTALLED_RELPATHS]
    configured = dedupe_dirs(configured)

    manifest = codex_home.joinpath(*MANIFEST_RELPATH)
    source_dir = repo_root.joinpath(*SOURCE_RELPATH)
    adapter_dir = repo_root.joinpath(*ADAPTER_RELPATH)

    installed_dirs = [d for d in configured if d.is_dir()]
    if not installed_dirs:
        for directory in configured:
            print(f"ℹ️  {directory} 不存在")
        print("本机没有 Codex 侧已安装副本，无需比对")
        return 0

    labels = label_dirs(installed_dirs)
    companions = load_companions(repo_root)
    registry, manifest_ok, notes = parse_manifest(manifest, installed_dirs)
    rows = build_rows(
        installed_dirs, source_dir, adapter_dir, registry, manifest_ok, labels, companions.names
    )

    for directory in configured:
        if directory in installed_dirs:
            baseline = BASELINE_LABELS[baselines_for(directory)]
            print(f"已安装: {directory}  (基准: {baseline})")
        else:
            print(f"已安装: {directory}  ⚠️ 不存在，已跳过")
    print(f"源码:   {source_dir}")
    print(f"适配器: {adapter_dir}")
    print(f"清单:   {manifest}" + ("" if manifest_ok else "  ⚠️ 缺失或无法解析，注册列不可判"))
    detail = "、".join(companions.sources) if companions.sources else "无"
    print(f"配套登记: {len(companions.names)} 项（{detail}）")
    for warning in companions.warnings:
        print(f"⚠️  配套登记: {warning}")
    print()

    if rows:
        for line in render(rows):
            print(line)
    else:
        print("（装侧与仓内都没有 .py/.sh 脚本）")
    print()

    for note in notes:
        print(f"⚠️  清单解析: {note}")

    print(summarize(rows))

    bad = sum(1 for r in rows if r.status in (STATUS_DRIFT, STATUS_INSTALLED_ONLY))
    if bad:
        print(f"❌ {bad} 个已安装副本与仓内基准不一致（漂移或来路不明）—— 先看清楚再决定怎么同步，本脚本不代劳")
        return 1
    print("✅ 已安装副本与仓内基准一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
