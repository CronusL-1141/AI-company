"""The files OS installs into a Claude Code config folder, derived from ``plugin/``.

OS installs more than its hooks: agent templates, slash commands, skills and the
``/loop`` prompt as well (``install.py`` copies ``plugin/agents``, ``plugin/commands``,
``plugin/skills`` and ``plugin/loop.md``). A file the installer will put back is not
backed up; the release it came from decides:

* a name OS installs, with the release's content (or, for ``loop.md``, the OS marker
  in its first line): OS's, not backed up;
* a name OS installs, with other content: the user changed it, or it is an older
  release. Content cannot tell the two apart, so it is backed up as an OS template
  and a restore leaves it unchecked by default;
* a name OS no longer installs (retired): OS's;
* any other name: the user's.

The list is read from the source tree this process runs from, never written by
hand: a template added to ``plugin/`` is OS's the moment it ships (machine-checked).
Retired names, the loop marker, OS's MCP server name and which hook registrations
are OS's come from ``install.py`` itself (``_is_our_hook``), loaded by path the way
the installer loads the path section: one definition each.

Blocking (reads the tree): build it in a worker thread or the child process.
"""

from __future__ import annotations

import hashlib
import importlib.util
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import ModuleType

# Installed by name into the folder of the same name.
_FLAT_TEMPLATE_DIRS = ("agents", "commands")
_TEMPLATE_SUFFIX = ".md"
_SKILLS = "skills"
_LOOP = "loop.md"


def source_root() -> Path:
    """The checkout this process runs from (OS is installed editable from source)."""
    return Path(__file__).resolve().parents[3]


@cache
def _installer(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_aiteam_assets_installer", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the installer at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def installer(root: Path | None = None) -> ModuleType:
    """``install.py`` of ``root`` (default: this checkout), loaded once per path."""
    return _installer((root or source_root()) / "install.py")


def is_our_hook(command: str, root: Path | None = None) -> bool:
    """Whether a settings.json hook command is one OS registers (``install.py``'s own test)."""
    return bool(installer(root)._is_our_hook(command))  # noqa: SLF001 - the one definition


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class OsManifest:
    """OS's files in a config folder: release content by relative path, and retired names."""

    files: Mapping[str, str]
    retired: frozenset[str]
    loop_sentinel: str
    mcp_server_name: str

    def os_name(self, rel_path: str) -> bool:
        return rel_path in self.files or rel_path in self.retired

    def is_release(self, rel_path: str, content: bytes) -> bool:
        """Whether this content at this path is what OS installs there."""
        if rel_path in self.retired:
            return True
        if rel_path == _LOOP and self.loop_sentinel:
            first = content.split(b"\n", 1)[0].decode("utf-8", errors="replace")
            if self.loop_sentinel in first:
                return True
        return self.files.get(rel_path) == hashlib.sha256(content).hexdigest()

    def to_json(self) -> dict:
        return {"files": dict(self.files), "retired": sorted(self.retired),
                "loop_sentinel": self.loop_sentinel, "mcp_server_name": self.mcp_server_name}

    @classmethod
    def from_json(cls, data: Mapping) -> OsManifest:
        return cls(files=dict(data["files"]), retired=frozenset(data["retired"]),
                   loop_sentinel=data["loop_sentinel"], mcp_server_name=data["mcp_server_name"])


def build_claude_manifest(root: Path | None = None) -> OsManifest:
    """The manifest of the release in ``root`` (default: this checkout)."""
    root = root or source_root()
    plugin = root / "plugin"
    files: dict[str, str] = {}
    for name in _FLAT_TEMPLATE_DIRS:
        for template in sorted((plugin / name).glob(f"*{_TEMPLATE_SUFFIX}")):
            files[f"{name}/{template.name}"] = _sha256(template)
    skills = plugin / _SKILLS
    for path in sorted(skills.rglob("*")) if skills.is_dir() else ():
        if path.is_file() and "__pycache__" not in path.parts:
            files[f"{_SKILLS}/{path.relative_to(skills).as_posix()}"] = _sha256(path)
    if (plugin / _LOOP).is_file():
        files[_LOOP] = _sha256(plugin / _LOOP)
    module = installer(root)
    retired = frozenset(f"commands/{name}" for name in module.RETIRED_COMMAND_FILES)
    return OsManifest(files=files, retired=retired, loop_sentinel=module.LOOP_TEMPLATE_SENTINEL,
                      mcp_server_name=module.MCP_SERVER_NAME)
