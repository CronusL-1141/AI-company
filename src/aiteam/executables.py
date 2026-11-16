"""Find a program to run as a child process, whatever PATH the calling process was given.

The API may be started by launchd, by a hook or from a graphical session, each with
a different PATH, and a GUI-launched process often lacks the usual install folders.
So a program is never run by its bare name: it is resolved here to an absolute path,
in this order:

1. an environment variable naming it, when the caller has one (an absolute path to an
   executable file, used alone: an invalid value is an error, nothing else is tried);
2. the caller's fixed install locations, in its order;
3. the name on PATH extended with common install folders (Homebrew, /usr/local/bin,
   ~/.local/bin, ~/bin, the Nix profile). Relative PATH entries are dropped, so the
   choice never depends on the working directory. No login shell runs.

Standard library only.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path

# Folders a GUI-launched process often lacks on PATH.
PATH_EXTRA_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/bin", "~/.nix-profile/bin")


class ExecutableConfigError(ValueError):
    """The environment variable naming a program does not name an executable file."""


def executable_name(name: str) -> str:
    """``name`` as the file it is on this platform."""
    return f"{name}.exe" if sys.platform == "win32" else name


def is_executable(path: Path) -> bool:
    """An executable file; an unreadable location is skipped like a missing one, as PATH lookup does."""
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def search_path() -> str:
    """This process's PATH plus PATH_EXTRA_DIRS, absolute entries only."""
    entries = os.environ.get("PATH", os.defpath).split(os.pathsep)
    entries += [os.path.expanduser(extra) for extra in PATH_EXTRA_DIRS]
    return os.pathsep.join(entry for entry in entries if os.path.isabs(entry))


def configured(env_var: str) -> Path | None:
    """The program ``env_var`` names, None when it is unset; raises ExecutableConfigError when invalid."""
    value = os.environ.get(env_var)
    if value is None:
        return None
    path = Path(value).expanduser()
    if not value.strip() or not path.is_absolute():
        raise ExecutableConfigError(f"{env_var} 必须是明确的可执行文件绝对路径。")
    if not is_executable(path):
        raise ExecutableConfigError(f"{env_var} 指向的文件不存在或不可执行：{path}")
    return path


def find(name: str, *, env_var: str = "", candidates: Iterable[Path] = ()) -> Path | None:
    """The program to run (see the module doc), None when it is not installed anywhere looked."""
    if env_var:
        chosen = configured(env_var)
        if chosen is not None:
            return chosen
    for candidate in candidates:
        if is_executable(candidate):
            return candidate
    found = shutil.which(executable_name(name), path=search_path())
    return Path(found) if found is not None else None
