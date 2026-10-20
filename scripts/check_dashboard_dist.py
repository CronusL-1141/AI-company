#!/usr/bin/env python3
"""I3 - the two Dashboard builds are one build.

dashboard/dist is the local build (ignored by Git); plugin/dashboard-dist is
the tracked copy that ships with the plugin. The release step builds once and
copies the whole directory. Comparing only the JS file names missed two shapes:
a build that changed only the stylesheet, and an index.html that points at a
bundle the directory does not contain (a blank page for every plugin user).

Checks:
1. Every dist that exists has an index.html, and each file it references under
   assets/ is present in that same directory.
2. When both exist: the .js and .css names under assets/ match, index.html is
   byte-identical, and every asset it references is byte-identical.

Two builds that are equally old compared with dashboard/src look identical
here; I4 and the release build step cover freshness against the sources.

Usage: python3 scripts/check_dashboard_dist.py [--root DIR]
Exit code: 0 = one build; 1 = mismatch or a broken reference; 2 = a dist has
no assets/ directory (an unbuilt checkout; the total check only warns). The
dist that does exist is still checked by item 1, and a broken one returns 1.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ("dashboard", "dist")
SHIPPED = ("plugin", "dashboard-dist")
BUNDLE_SUFFIXES = (".js", ".css")
REFERENCE = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def asset_references(index: str) -> tuple[list[str], list[str]]:
    """Paths under assets/ that index.html loads, and references that escape it."""
    found: list[str] = []
    invalid: list[str] = []
    for raw in REFERENCE.findall(index):
        target = raw.split("?", 1)[0].split("#", 1)[0]
        if "://" in target or target.startswith("//"):
            continue
        path = PurePosixPath(target.lstrip("/").removeprefix("./"))
        if not path.parts or path.parts[0] != "assets":
            continue
        if ".." in path.parts or len(path.parts) < 2:
            invalid.append(raw)
            continue
        if path.as_posix() not in found:
            found.append(path.as_posix())
    return found, invalid


def bundle_names(dist: Path) -> dict[str, list[str]]:
    names: dict[str, list[str]] = {suffix: [] for suffix in BUNDLE_SUFFIXES}
    for entry in sorted((dist / "assets").iterdir()):
        if entry.is_file() and entry.suffix in names:
            names[entry.suffix].append(entry.name)
    return names


def check_self(label: str, dist: Path) -> tuple[list[str], list[str]]:
    """Problems inside one dist, and the assets its index.html references."""
    index = dist / "index.html"
    try:
        text = index.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"{label}/index.html 不可读: {exc.__class__.__name__}"], []
    references, invalid = asset_references(text)
    problems = [f"{label}/index.html 的引用越出 assets/: {raw}" for raw in invalid]
    if not references:
        problems.append(f"{label}/index.html 没有引用 assets/ 下的任何资源")
    for name in references:
        if not (dist / name).is_file():
            problems.append(f"{label}/index.html 引用的 {name} 不存在")
    return problems, references


def compare(local: Path, shipped: Path, references: list[str]) -> list[str]:
    problems: list[str] = []
    local_names, shipped_names = bundle_names(local), bundle_names(shipped)
    for suffix in BUNDLE_SUFFIXES:
        if local_names[suffix] != shipped_names[suffix]:
            problems.append(
                f"assets/*{suffix} 不一致: {'/'.join(LOCAL)} {local_names[suffix]}"
                f" vs {'/'.join(SHIPPED)} {shipped_names[suffix]}"
            )
    if (local / "index.html").read_bytes() != (shipped / "index.html").read_bytes():
        problems.append("两份 index.html 不是逐字节相同")
    for name in references:
        try:
            if (local / name).read_bytes() != (shipped / name).read_bytes():
                problems.append(f"{name} 两份内容不同")
        except OSError:
            continue  # already reported by check_self
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="I3: compare the local and shipped Dashboard builds")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    args = parser.parse_args(argv)

    dists = {"/".join(parts): args.root.joinpath(*parts) for parts in (LOCAL, SHIPPED)}
    present = {label: path for label, path in dists.items() if (path / "assets").is_dir()}
    problems: list[str] = []
    references: dict[str, list[str]] = {}
    for label, path in present.items():
        found, refs = check_self(label, path)
        problems.extend(found)
        references[label] = refs

    if len(present) == len(dists) and not problems:
        local, shipped = dists["/".join(LOCAL)], dists["/".join(SHIPPED)]
        problems.extend(compare(local, shipped, references["/".join(SHIPPED)]))

    if problems:
        for problem in problems:
            print(problem)
        return 1
    if len(present) != len(dists):
        missing = ", ".join(label for label in dists if label not in present)
        print(f"缺 assets/ 目录: {missing}")
        return 2
    names = bundle_names(dists["/".join(SHIPPED)])
    print(f"JS {len(names['.js'])} / CSS {len(names['.css'])} / index.html 引用 "
          f"{len(references['/'.join(SHIPPED)])} 项，两份逐字节一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
