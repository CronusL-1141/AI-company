#!/usr/bin/env python3
"""I23 —— 仓库内项目级 agent 模板必须是分发版的「前缀扩展」。

`plugin/agents/*.md` 随安装分发给所有用户，只写通用内容；只对开发本仓库有用的事实
放在 `.claude/agents/<同名>.md`。CC 按 project > user > plugin 解析同名 agent（按
frontmatter name，且递归扫描子目录），于是在本仓库里派工时，项目级文件整份顶替分发版。

这正是它危险的地方：分发版改了而项目级没跟上，本仓库里派出去的永远是旧模板，别处
的用户拿到的才是新的——两边各自看都没错，只有在本仓库里派工才会撞上。本目录此前
就因此被整个删过一次（22 份副本里 16 份仍写着旧的 model，22 份都没有 disallowedTools）。

断言（项目级文件递归收集，子目录里的一样算）：
1. 目录存在且至少有一份（目录被删或清空时静默通过，等于机检自己下线）。
2. 项目级各文件的 frontmatter name 互不重复（CC 只会取其中一份，另一份成了暗桩）。
3. 按 name 在 `plugin/agents/` 找到对应分发版（找不到 = 在本仓库凭空多出一个模板）。
4. 两者 frontmatter 逐字相同（description / model / 工具限制 / skills 不许分叉）。
5. 项目级正文以分发版正文为前缀（分发版的每一句在本仓库里都照样生效）。
6. 项目级正文比分发版多出内容（没有本仓库专属内容就不该建项目级文件）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROJECT_DIR = ROOT / ".claude" / "agents"
PLUGIN_DIR = ROOT / "plugin" / "agents"


def split_frontmatter(text: str) -> tuple[str, str] | None:
    """Return (frontmatter, body) split on the first closing fence, or None."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    return text[4:end], text[end + 5:]


def template_name(path: Path, frontmatter: str) -> str:
    """Frontmatter name - the identity CC resolves - with the stem as fallback."""
    try:
        meta = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        meta = None
    name = meta.get("name") if isinstance(meta, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else path.stem


def load(path: Path) -> tuple[str, str, str] | None:
    """Return (name, frontmatter, body), or None when the file has no frontmatter."""
    parts = split_frontmatter(path.read_text(encoding="utf-8"))
    if parts is None:
        return None
    return template_name(path, parts[0]), parts[0], parts[1]


def main() -> int:
    files = sorted(PROJECT_DIR.rglob("*.md")) if PROJECT_DIR.is_dir() else []
    if not files:
        print(f"[FAIL] I23: {PROJECT_DIR.relative_to(ROOT)} 不存在或没有模板 —— 项目级覆盖整体丢失")
        return 1

    plugin: dict[str, tuple[Path, str, str]] = {}
    for path in sorted(PLUGIN_DIR.glob("*.md")):
        loaded = load(path)
        if loaded is not None:
            plugin[loaded[0]] = (path, loaded[1], loaded[2])

    errors: list[str] = []
    seen: dict[str, Path] = {}
    for proj in files:
        rel = proj.relative_to(ROOT)
        loaded = load(proj)
        if loaded is None:
            errors.append(f"{rel}: frontmatter 无法切分（须以 --- 行开头并闭合）")
            continue
        name, fm, body = loaded
        if name in seen:
            errors.append(f"{rel}: name `{name}` 与 {seen[name].relative_to(ROOT)} 重复")
            continue
        seen[name] = proj
        if name not in plugin:
            errors.append(f"{rel}: plugin/agents/ 下没有 name 为 `{name}` 的分发版")
            continue
        dist_path, dist_fm, dist_body = plugin[name]
        if fm != dist_fm:
            errors.append(f"{rel}: frontmatter 与 {dist_path.relative_to(ROOT)} 不逐字相同")
        if not body.startswith(dist_body):
            errors.append(f"{rel}: 正文不以 {dist_path.relative_to(ROOT)} 的正文为前缀（分发版改了没同步过来）")
        elif not body[len(dist_body):].strip():
            errors.append(f"{rel}: 正文与分发版相同，没有本仓库专属内容就删掉这份")

    if errors:
        print("[FAIL] I23:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"[OK] I23: {len(files)} 份项目级模板均为分发版的前缀扩展")
    return 0


if __name__ == "__main__":
    sys.exit(main())
