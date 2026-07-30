#!/usr/bin/env python3
"""从 CHANGELOG.md 生成 GitHub Release 正文 —— 只生成，绝不发布。

事故背景：v1.10.3 / v1.11.0 / v1.11.1 三个 tag 早已推到公开仓，Release 条目却一直
没建，访客主页侧边栏两周显示 "Latest v1.10.2"（2026-07-30 手工补齐）。上一轮完全
同型：v1.10.0/1/2 的 tag 是 07-14 打的，条目 07-21 才批量补。病因不是疏忽——凡是
进了 scripts/check_invariants.sh 的红线都没烂，没进机检的全烂，而"发版那天还要建
Release 条目"只存在于人的记忆里。本脚本把其中**可以离线确定性完成**的那部分（正文
从哪来、版本对不对、命令怎么写）固化成一条命令。

三个刻意选择：

1. **正文是 CHANGELOG 段落的原样切片，不做任何 strip。** 切片范围 = 版本标题行之后
   到下一个 ``## [`` 之前；标题行本身不含在内，因为 Release 页面已经显示 tag 与日期。
   不 strip 是实测结论：线上 v1.11.1 的 body 就是这样一段原样切片（含前导换行与尾部
   空行，15597 字符），去掉空白就再也对不上——而"新生成的正文与线上现有条目逐字节
   可比"是这个脚本唯一的验收指标，可比性一破，--check 就沦为摆设。
2. **绝不 publish。** 本仓库纪律：发布流水线必须可中断，commit / push / publish 一律
   留给人执行。脚本只打印可直接粘贴的 ``gh release create``。这也是 --check 拿不到网络
   时警告而非失败的原因：它的职责是报告，不是把关。
3. **校验只做离线能做的那些。** 段落存在、版本与 I2 锁步的五处一致、正文非空——都不
   需要联网，所以无条件跑。查条目在不在、正文对不对必须联网，所以放进可选的 --check，
   并且允许优雅降级。这也正是"给 Release 条目加一条机检"目前做不到的地方：
   check_invariants.sh 必须离线确定性可跑。

用法::

    python3 scripts/release_notes.py 1.11.1                    # 抽段 → 落文件 → 打印 gh 命令
    python3 scripts/release_notes.py 1.11.1 --title "v1.11.1 — Truthful Ledgers"
    python3 scripts/release_notes.py --check 1.11.1             # 与线上 Release 正文逐字节比对
    python3 scripts/release_notes.py 1.10.3 --backfill          # 为历史 tag 补条目

退出码：0=通过（含联网失败降级跳过），1=离线校验失败 / 线上缺条目 / 正文不一致。
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
# build/ 已在 .gitignore 内，落在这里不污染工作树，同时路径稳定到可以直接复制进命令。
DEFAULT_OUT_DIR = ROOT / "build" / "release-notes"
# 公开仓的 remote 名。origin 是私有仓，Release 条目只建在公开仓。
PUBLIC_REMOTE = "public"

# 版本标题行的破折号有两种写法（1.10.0 及以后 ASCII "-"，1.9.0 及以前中文 "—"），
# 所以标题行尾部一律用 [^\n]* 吞掉，不去匹配日期分隔符本身。
_HEADING_TMPL = r"^## \[{ver}\][^\n]*\n"
_NEXT_HEADING = re.compile(r"^## \[", re.M)
_ANY_HEADING = re.compile(r"^## \[([^\]]+)\]", re.M)
_SEMVER = re.compile(r"\d+\.\d+\.\d+")

# gh 失败分两类。**认不出来的一律按降级处理**：一个只负责报告的脚本不该为未知错误崩，
# 更不该把"我没查到"渲染成"线上有问题"。
_DEGRADE = re.compile(
    r"dial tcp|no such host|timed? ?out|connection refused|network is unreachable"
    r"|could not resolve|certificate|tls|proxy|unexpected eof|未安装"
    r"|not logged in|authentication|bad credentials|gh auth",
    re.I,
)
_MISSING = re.compile(r"release not found|not found|404", re.I)


class OnlineUnavailableError(Exception):
    """联网/鉴权/工具缺失导致查不到线上状态——查不到不等于有问题，降级跳过。"""


# 三个等级全部走 stdout，与 scripts/ 下其余检查脚本一致。分流到 stderr 会在管道里
# 与 stdout 交错错序——而这些输出的第一读者是会话里捕获输出的 Leader，顺序即因果。
def ok(msg: str) -> None:
    print(f"✅ {msg}")


def warn(msg: str) -> None:
    print(f"⚠️  {msg}")


def fail(msg: str) -> None:
    print(f"❌ {msg}")


# ── CHANGELOG 抽段 ────────────────────────────────────────────────


def extract_section(text: str, version: str) -> str | None:
    """取版本段落原样切片；找不到该版本返回 None。"""
    head = re.search(_HEADING_TMPL.format(ver=re.escape(version)), text, re.M)
    if head is None:
        return None
    rest = text[head.end() :]
    nxt = _NEXT_HEADING.search(rest)
    return rest if nxt is None else rest[: nxt.start()]


def known_versions(text: str) -> list[str]:
    """CHANGELOG 里出现过的版本号，按文件顺序（即从新到旧）。"""
    return _ANY_HEADING.findall(text)


# ── I2 五处版本锁步 ──────────────────────────────────────────────


def read_version_sites() -> dict[str, str]:
    """读 I2 机检的五处版本真相源。这个集合必须与 check_invariants.sh 里那份保持一致。"""
    sites: dict[str, str] = {}

    m = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    sites["pyproject.toml"] = m.group(1) if m else "?"

    m = re.search(r'__version__ = "([^"]+)"', (ROOT / "src/aiteam/__init__.py").read_text(encoding="utf-8"))
    sites["src/aiteam/__init__.py"] = m.group(1) if m else "?"

    plugin_json = json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
    sites["plugin/.claude-plugin/plugin.json"] = plugin_json.get("version") or "?"

    for rel in ("plugin/.claude-plugin/marketplace.json", ".claude-plugin/marketplace.json"):
        data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        plugins = data.get("plugins") or []
        sites[rel] = (plugins[0].get("version") if plugins else data.get("version")) or "?"

    return sites


# ── 外部命令 ──────────────────────────────────────────────────────


def _run(argv: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """跑外部命令，永不抛异常。cwd 固定在 ROOT —— 本仓库要求并行会话用 git worktree
    隔离，不锚定工作目录就会去读另一个 checkout 的 remote 与 tag。"""
    try:
        proc = subprocess.run(
            argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: 未安装"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: 超时 {timeout}s"
    return proc.returncode, proc.stdout, proc.stderr


def public_repo_slug() -> str | None:
    code, out, _ = _run(["git", "remote", "get-url", PUBLIC_REMOTE], timeout=15)
    if code != 0:
        return None
    url = out.strip().removesuffix(".git")
    m = re.search(r"[:/]([^/:]+/[^/]+)$", url)
    return m.group(1) if m else None


def tag_exists_locally(tag: str) -> bool:
    code, _, _ = _run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], timeout=15)
    return code == 0


def gh_json(args: list[str]) -> object | None:
    """跑 gh 并解析 JSON。返回 None = 线上明确没有该对象；抛 OnlineUnavailableError = 查不到。"""
    code, out, err = _run(["gh", *args])
    if code == 0:
        return json.loads(out)
    msg = (err or out).strip() or f"gh 退出码 {code}"
    if code in (124, 127) or _DEGRADE.search(msg):
        raise OnlineUnavailableError(msg)
    if _MISSING.search(msg):
        return None
    raise OnlineUnavailableError(msg)


# ── --check：与线上正文逐字节比对 ─────────────────────────────────


def _first_diff(a: str, b: str) -> int:
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            return i
    return min(len(a), len(b))


def _report_body_diff(version: str, local: str, online: str, slug: str, out_path: Path) -> None:
    i = _first_diff(local, online)
    lo, hi = max(0, i - 40), i + 40
    fail(
        f"正文与线上不一致：本地 {len(local)} 字符 / 线上 {len(online)} 字符，"
        f"首个差异在第 {i} 字符"
    )
    print(f"   本地: {local[lo:hi]!r}")
    print(f"   线上: {online[lo:hi]!r}")
    same_normalized = local.replace("\r\n", "\n").strip() == online.replace("\r\n", "\n").strip()
    print(
        "   规范化（去 CRLF 与首尾空白）后"
        + (
            "相等 —— 差异只在换行/空白，GitHub 渲染结果不变"
            "（v1.10.0/1/2 那批条目是用 strip 过的切片建的，属历史差异）"
            if same_normalized
            else "仍不相等 —— 正文实质不同，多半是条目建好后 CHANGELOG 又被改过"
        )
    )
    print(
        f"   修正线上正文（由人执行）: gh release edit v{version} --repo {slug} "
        f"--notes-file {shlex.quote(str(out_path))}"
    )


def _report_latest_flag(version: str, slug: str, is_newest: bool) -> int:
    """核对 latest 徽章。只有当该版本是 CHANGELOG 里最新的那个时，不是 latest 才算失败
    ——访客主页侧边栏显示的就是这个徽章，2026-07-30 之前它卡在 v1.10.2 两周。"""
    try:
        rels = gh_json(["release", "list", "--repo", slug, "--limit", "30", "--json", "tagName,isLatest"])
    except OnlineUnavailableError as exc:
        warn(f"latest 徽章未能核对（{exc}）")
        return 0
    if not rels:
        warn("latest 徽章未能核对：该仓库没有任何 Release 条目")
        return 0

    latest = next((r["tagName"] for r in rels if r.get("isLatest")), None)
    tag = f"v{version}"
    if latest == tag:
        ok(f"latest 徽章 = {tag}")
        return 0
    if is_newest:
        fail(
            f"latest 徽章指向 {latest or '（无）'}，而 CHANGELOG 里最新的版本是 {tag} "
            "—— 访客主页侧边栏正显示着旧版本"
        )
        return 1
    print(f"ℹ️  latest 徽章 = {latest or '（无）'}（{tag} 不是最新版本，符合预期）")
    return 0


def check_online(version: str, generated: str, slug: str, out_path: Path, is_newest: bool) -> int:
    tag = f"v{version}"
    try:
        data = gh_json(["release", "view", tag, "--repo", slug, "--json", "body,name,publishedAt"])
    except OnlineUnavailableError as exc:
        warn(f"--check 跳过：拿不到线上 Release（{exc}）。脚本只报告不把关，不算失败。")
        return 0

    if data is None:
        fail(
            f"{slug} 上没有 {tag} 的 Release 条目 —— tag 推了但条目没建，"
            "正是 2026-07-30 手工补齐的那类漏项"
        )
        return 1

    assert isinstance(data, dict)
    status = 0
    online_body: str = data.get("body") or ""
    if online_body == generated:
        ok(f"正文与线上逐字节一致（{len(generated)} 字符）")
    else:
        _report_body_diff(version, generated, online_body, slug, out_path)
        status = 1

    print(f"ℹ️  线上标题: {data.get('name')!r} · 发布时间 {data.get('publishedAt')}")
    return status or _report_latest_flag(version, slug, is_newest)


# ── 发布命令（打印，不执行）───────────────────────────────────────


def print_create_command(version: str, slug: str, out_path: Path, title: str, backfill: bool) -> None:
    tag = f"v{version}"
    print()
    print("发布命令 —— 由人执行，本脚本不发布:")
    print(f"  gh release create {tag} \\")
    print(f"    --repo {slug} \\")
    print("    --verify-tag \\")
    print(f"    --title {shlex.quote(title)} \\")
    # 仓库路径可能带空格（本机 checkout 就在 "AI team OS" 下），不 quote 的命令粘贴即错。
    print(f"    --notes-file {shlex.quote(str(out_path))}")
    print()
    print(f"  · --verify-tag 校验的是**远端**有没有 {tag}；本地有 tag ≠ 远端有，"
          f"必要时先 git push {PUBLIC_REMOTE} {tag}（同样由人执行）")
    print('  · 历史条目的标题惯例是 "v1.11.1 — <英文副标>"，副标要人写，脚本不编造；'
          "用 --title 传")
    if backfill:
        print("  · 补建多个条目时**按版本升序逐个 create**：GitHub 的 latest 徽章看发布时间，"
              "倒序建会把旧版本顶成 latest")
    print(f"  · 建完立刻核对: python3 scripts/release_notes.py --check {version}")


# ── 主流程 ────────────────────────────────────────────────────────


def _resolve_version(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    raw = args.check or args.version
    if raw is None:
        parser.error("需要版本号: release_notes.py 1.11.1 或 release_notes.py --check 1.11.1")
    if args.check and args.version and args.check.lstrip("v") != args.version.lstrip("v"):
        parser.error("--check 与位置参数给了两个不同的版本")
    version = raw.lstrip("v")
    if not _SEMVER.fullmatch(version):
        parser.error(f"版本号格式不对: {raw!r}（期望 1.11.1 或 v1.11.1）")
    return version


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 CHANGELOG.md 生成 GitHub Release 正文（只生成，不发布）"
    )
    parser.add_argument("version", nargs="?", help="版本号，1.11.1 或 v1.11.1")
    parser.add_argument("--check", metavar="VERSION", help="与线上 Release 正文逐字节比对（联网失败则降级跳过）")
    parser.add_argument("--out", type=Path, help=f"正文输出路径（默认 {DEFAULT_OUT_DIR}/v<版本>.md）")
    parser.add_argument("--title", help='Release 标题（默认 "v<版本>"）')
    parser.add_argument("--repo", help=f"目标仓库 owner/name（默认取 git remote {PUBLIC_REMOTE}）")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="为历史版本补条目：显式声明版本号与当前树的版本不一致是有意的",
    )
    args = parser.parse_args()
    version = _resolve_version(parser, args)

    if not CHANGELOG.exists():
        fail(f"找不到 {CHANGELOG}")
        return 1
    text = CHANGELOG.read_text(encoding="utf-8")

    # ① 该版本在 CHANGELOG 里存在
    section = extract_section(text, version)
    if section is None:
        versions = known_versions(text)
        fail(f"CHANGELOG.md 里没有 {version} 段 —— 先补 CHANGELOG，再建 Release 条目")
        print(f"   现有版本（最近 8 个）: {', '.join(versions[:8])}")
        return 1

    # ② 正文非空
    if not section.strip():
        fail(f"{version} 段是空的 —— 空正文的 Release 条目等于没建")
        return 1

    # ③ 版本与 I2 锁步的五处一致
    sites = read_version_sites()
    distinct = set(sites.values())
    if len(distinct) != 1:
        fail("I2 版本五处漂移，发版前必须先收敛:")
        for k, v in sites.items():
            print(f"   {k} = {v}")
        return 1
    tree_version = distinct.pop()
    if tree_version != version:
        detail = f"当前树的版本是 {tree_version}，要生成的是 {version}"
        if args.check:
            warn(f"{detail} —— --check 在核对历史条目，继续")
        elif args.backfill:
            warn(f"{detail} —— --backfill 已显式声明，继续")
        else:
            fail(f"{detail} —— 发版日这通常是打错了版本号；确实在补历史条目请加 --backfill")
            return 1

    # ④ tag 存在性：本地可查，远端不可（离线），所以只警告
    tag = f"v{version}"
    if not tag_exists_locally(tag):
        warn(f"本地没有 tag {tag} —— gh release create --verify-tag 会失败，先打 tag 并推到远端")

    slug = args.repo or public_repo_slug()
    if slug is None:
        fail(f"取不到公开仓地址（git remote {PUBLIC_REMOTE} 不存在）—— 用 --repo owner/name 显式指定")
        return 1

    out_path = args.out or DEFAULT_OUT_DIR / f"{tag}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" 是硬要求：Windows 上默认换行翻译会把 \n 写成 \r\n，正文一进 GitHub
    # 就与 CHANGELOG 不再逐字节可比，--check 从此永远报不一致。
    out_path.write_text(section, encoding="utf-8", newline="\n")

    ok(
        f"{tag} 正文已生成: {out_path}"
        f"（{len(section)} 字符 / {len(section.splitlines())} 行，取自 CHANGELOG.md 原样切片）"
    )

    if args.check:
        is_newest = known_versions(text)[:1] == [version]
        return check_online(version, section, slug, out_path, is_newest)

    print_create_command(version, slug, out_path, args.title or tag, args.backfill)
    return 0


if __name__ == "__main__":
    sys.exit(main())
