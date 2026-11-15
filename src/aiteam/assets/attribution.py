"""Which OS project a Claude Code project folder (``projects/<slug>/``) belongs to.

Claude Code names the folder by replacing every character of the session's working
directory that is not an ASCII letter or digit with ``-``. That cannot be undone (a
Chinese directory name becomes dashes only, and two names of equal length collide),
so a slug is never turned back into a path: each project's ``root_path`` is turned
into a slug and compared.

1. equal slugs: the project itself;
2. otherwise, when a session file of the folder records its ``cwd``, the path decides:
   the project it lies in (OS's subfolder rule, longest root), or none. A slug that
   merely extends a project's slug may be a sibling folder (``X-foo`` next to ``X``);
3. without a session file, a slug that extends one project's slug by ``-`` (the
   longest, if unique) is taken as a session in that project's subfolder;
4. otherwise unbound, keyed by the slug itself. Unbound memory is backed up all the
   same and never registers a project (registration is the user's act).

The subfolder is kept in slug form (the rest of the folder name). It is part of an
asset's identity, so it must not depend on the disk: a real folder looked up under
the project root would come and go with an external drive's mount, and the same
file would flip between two assets. The slug form loses nothing a restore needs:
``slug(root + "/" + sub) == slug(root) + "-" + slug(sub)``, so the target folder on
another machine is the new root's slug plus this rest.

Reading session files blocks: call ``attribute`` from a worker thread.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from aiteam.types import AssetOwnerKind

_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")
# How far into a session file the cwd is looked for.
_CWD_LINES = 20
_CWD_BYTES = 256 * 1024


def slug(path: str) -> str:
    """The folder name Claude Code gives a working directory."""
    return _NOT_ALNUM.sub("-", path)


@dataclass(frozen=True)
class ProjectRoot:
    id: str
    root_path: str


@dataclass(frozen=True)
class Owner:
    kind: AssetOwnerKind
    ref: str = ""
    subpath: str = ""


def _root(path: str) -> str:
    return path.rstrip("/") or path


def _by_path(cwd: str, projects: Iterable[ProjectRoot]) -> Owner | None:
    """The project whose root is ``cwd`` or its longest ancestor."""
    best: ProjectRoot | None = None
    for project in projects:
        root = _root(project.root_path)
        if cwd == root or cwd.startswith(root + "/"):
            if best is None or len(root) > len(_root(best.root_path)):
                best = project
    if best is None:
        return None
    rest = cwd[len(_root(best.root_path)):].lstrip("/")
    return Owner(AssetOwnerKind.PROJECT, best.id, slug(rest))


def session_cwd(project_folder: Path) -> str:
    """The ``cwd`` a session file of this Claude Code project folder records, or ""."""
    try:
        sessions = sorted(project_folder.glob("*.jsonl"))
    except OSError:
        return ""
    for session in sessions:
        try:
            with open(session, encoding="utf-8", errors="replace") as handle:
                head = handle.read(_CWD_BYTES).splitlines()[:_CWD_LINES]
        except OSError:
            continue
        for line in head:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get("cwd"), str) and record["cwd"]:
                return record["cwd"]
    return ""


def attribute(folder_slug: str, projects: list[ProjectRoot], project_folder: Path | None = None) -> Owner:
    """The owner of ``projects/<folder_slug>/`` in a Claude Code config folder."""
    exact = [p for p in projects if slug(_root(p.root_path)) == folder_slug]
    if len(exact) == 1:
        return Owner(AssetOwnerKind.PROJECT, exact[0].id)
    prefixed = [p for p in projects if folder_slug.startswith(slug(_root(p.root_path)) + "-")]
    if not (exact or prefixed):
        return Owner(AssetOwnerKind.UNBOUND, folder_slug)
    cwd = session_cwd(project_folder) if project_folder is not None else ""
    if cwd:
        return _by_path(cwd, projects) or Owner(AssetOwnerKind.UNBOUND, folder_slug)
    if not exact:
        longest = max(len(slug(_root(p.root_path))) for p in prefixed)
        best = [p for p in prefixed if len(slug(_root(p.root_path))) == longest]
        if len(best) == 1:
            return Owner(AssetOwnerKind.PROJECT, best[0].id, folder_slug[longest + 1:])
    return Owner(AssetOwnerKind.UNBOUND, folder_slug)


def attribute_path(folder: str, projects: list[ProjectRoot]) -> Owner:
    """The owner of a per-project entry keyed by its folder's path (the global config's projects)."""
    return _by_path(_root(folder), projects) or Owner(AssetOwnerKind.UNBOUND, slug(_root(folder)))
