"""What feedback text goes through before anyone sees a draft, in this order.

1. The secret scan (aiteam.assets.secret_scan, every production rule): any hit
   refuses the draft, ``long_line`` included, and there is no way to allow one: the
   destination is a public repository. A refusal names the rule, the field and the
   line, never the matched text.
2. Deterministic rewrites, each counted for the preview: known folders (this
   project, the registered project roots, the Claude Code config folder, the Codex
   home, the OS data folder) to placeholders; the home folder (this machine's, and
   any ``/Users/<name>``, ``/home/<name>``, ``C:\\Users\\<name>``) to ``~``;
   e-mail addresses to ``<email>`` (an SSH remote's ``git@`` stays); the host and
   login names to ``<host>`` and ``<user>``; the credential after ``Bearer``,
   ``Basic`` or ``Token`` when it looks like one; URLs through
   ``aiteam.assets.extract.redact_url`` (user and password, query values, key-like
   path segments). Not ``extract.redact_text`` as a whole: it takes the word after
   any "token" or "basic", which is right for a shell command and wrong for prose
   ("token usage"). A login or host name shorter than three
   characters, or a common word, is not rewritten (it would mangle ordinary prose)
   but named in the preview for the user to check.
3. The private-term list (``feedback-private-terms.txt`` in the OS data folder, one
   term per line, ``#`` starts a comment, at most 500): a term found refuses the
   draft. Nothing is replaced: replacing a word changes what a sentence says, and the
   author is there to rewrite it. Matching ignores case; an ASCII term matches as a
   whole word, any other term (Chinese has no word boundaries) wherever it occurs.
   No file means no list. The file is the user's own: it is never shipped, uploaded
   or put into anything sent to GitHub.

The caller scans the final text once more (title, body and diagnostics together):
the rewrites are the first line, the scan the second, as in aiteam.assets.extract.
The secret scan reads line by line, so a credential split over two lines is not
seen: the user reading the preview is the last line.

Absolute paths left after the rewrites, and names of registered projects, are
listed in the preview, not refused: a project called ``api`` or ``web`` would
otherwise refuse half of all reports.

Pure functions over at most 20,000 characters and 500 terms: milliseconds, run in a
worker thread by the caller.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from aiteam.assets.extract import REDACTED, redact_urls
from aiteam.assets.secret_scan import scan_text

MAX_TERMS = 500
TERMS_FILE = "feedback-private-terms.txt"
PRIVATE_TERM = "private_term"
# Names this short, or this ordinary, are words in prose more often than they are a user.
MIN_NAME_CHARS = 3
COMMON_NAMES = frozenset({
    "admin", "administrator", "user", "users", "test", "tester", "dev", "developer", "root", "guest",
    "ubuntu", "debian", "owner", "home", "work", "build", "code", "data", "app", "api", "web", "server",
    "main", "master", "default", "local", "localhost", "public", "shared", "staff", "student", "demo",
    "temp", "mac", "macbook", "imac", "pc", "desktop", "laptop", "runner", "docker", "node", "vagrant",
})
# The project's own public names are not private.
PUBLIC_NAMES = frozenset({"ai team os", "ai-team-os", "aiteam", "ai-company", "ai team"})
_NAME_CHARS = r"A-Za-z0-9._-"
_GENERIC_HOMES = (
    re.compile(r"(?<![A-Za-z0-9._-])(?:/Users|/home)/[^/\\\s'\"`<>]+(?=[/\s'\"`),;:]|$)"),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]Users[\\/][^\\/\s'\"`<>]+(?=[\\/\s'\"`),;:]|$)", re.IGNORECASE),
)
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"
                    r"(?![A-Za-z0-9-])")
# A credential after an auth scheme, as it looks in a pasted header: long, mixing letters and digits.
_CREDENTIAL_CHARS = "[A-Za-z0-9._~+/=-]"
_SCHEME_CREDENTIAL = re.compile(rf"\b(Bearer|Basic|Token)(\s+)(?={_CREDENTIAL_CHARS}*[0-9])"
                                rf"(?={_CREDENTIAL_CHARS}*[A-Za-z]){_CREDENTIAL_CHARS}{{16,512}}")
_RESIDUAL_PATH = re.compile(r"(?<![A-Za-z0-9._~<>:/-])(?:/[A-Za-z0-9._@+-]+){2,}/?|"
                            r"(?<![A-Za-z0-9])[A-Za-z]:\\(?:[^\\\s'\"`<>]+\\?)+")
MAX_LISTED = 10


@dataclass(frozen=True)
class Refusal:
    """Why a draft is held back: a rule, the field and the line (1-based).

    ``term`` is set for a private term only: shown to the user, never stored.
    """

    rule: str
    field: str
    line: int
    term: str = ""

    def stored(self) -> dict[str, str | int]:
        return {"rule": self.rule, "field": self.field, "line": self.line}


class Refused(Exception):  # noqa: N818 - an outcome, like aiteam.assets' refusals
    def __init__(self, refusals: list[Refusal]) -> None:
        super().__init__(", ".join(f"{r.rule}@{r.field}:{r.line}" for r in refusals))
        self.refusals = refusals


class TermListError(ValueError):
    """The private-term file exists but cannot be used (unreadable, or over MAX_TERMS)."""


@dataclass(frozen=True)
class Context:
    """What this machine knows that must not leave it."""

    roots: tuple[tuple[str, str], ...] = ()  # (absolute folder, placeholder)
    home: str = ""
    login: str = ""
    hostnames: tuple[str, ...] = ()
    project_names: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()


@dataclass
class Cleaned:
    fields: dict[str, str]
    rewrites: dict[str, int]
    # For the user to look at: unreplaced login or host names, possible private
    # names, absolute paths left. Never a reason to refuse.
    notes: dict[str, list[str]] = field(default_factory=dict)


def load_terms(path: Path) -> tuple[str, ...]:
    """The private terms in ``path``; () when there is no such file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError) as exc:
        raise TermListError(f"私有术语词表读不了（{path}）：{exc}") from None
    terms = []
    for line in text.splitlines():
        term = line.strip()
        if term and not term.startswith("#"):
            terms.append(term)
    terms = list(dict.fromkeys(terms))
    if len(terms) > MAX_TERMS:
        raise TermListError(f"私有术语词表有 {len(terms)} 条，超过上限 {MAX_TERMS} 条，请精简（{path}）")
    return tuple(terms)


def _word(term: str) -> str:
    escaped = re.escape(term)
    if term.isascii():
        return rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
    return escaped


def _words(terms: Iterable[str]) -> re.Pattern[str] | None:
    alternatives = [_word(term) for term in sorted(set(terms), key=len, reverse=True) if term]
    return re.compile("|".join(alternatives), re.IGNORECASE) if alternatives else None


def secret_hits(fields: Mapping[str, str]) -> list[Refusal]:
    return [Refusal(hit.rule, name, hit.line) for name, text in fields.items() for hit in scan_text(text)]


def term_hits(fields: Mapping[str, str], terms: Iterable[str]) -> list[Refusal]:
    pattern = _words(terms)
    if pattern is None:
        return []
    refusals = []
    for name, text in fields.items():
        for number, line in enumerate(text.splitlines(), 1):
            refusals += [Refusal(PRIVATE_TERM, name, number, match.group(0)) for match in pattern.finditer(line)]
    return refusals


class _Rewriter:
    """The rewrites of step 2 over one text, counting each kind."""

    def __init__(self, context: Context) -> None:
        roots = sorted({(path.rstrip("/\\"), placeholder) for path, placeholder in context.roots
                        if len(path.rstrip("/\\")) > 1}, key=lambda root: len(root[0]), reverse=True)
        if context.home and len(context.home.rstrip("/\\")) > 1:
            roots.append((context.home.rstrip("/\\"), "~"))
        self.folders = [(re.compile(rf"(?<![{_NAME_CHARS}]){re.escape(path)}(?![{_NAME_CHARS}])", re.IGNORECASE),
                         placeholder) for path, placeholder in roots]
        self.unreplaced: dict[str, list[str]] = {}
        # Longest first: a host name often contains the login (alice-macbook).
        names = sorted({(name, "<host>", "host") for name in context.hostnames if name}
                       | ({(context.login, "<user>", "user")} if context.login else set()),
                       key=lambda entry: len(entry[0]), reverse=True)
        self.names = [(_words([name]), placeholder, kind) for name, placeholder, kind in names
                      if self._replaceable(name, kind)]
        self.counts: Counter[str] = Counter()

    def _replaceable(self, name: str, kind: str) -> bool:
        if len(name) < MIN_NAME_CHARS or name.lower() in COMMON_NAMES:
            self.unreplaced.setdefault(kind, []).append(name)
            return False
        return True

    def _sub(self, pattern: re.Pattern[str], replacement, text: str, kind: str) -> str:
        text, count = pattern.subn(replacement, text)
        self.counts[kind] += count
        return text

    def _email(self, match: re.Match[str]) -> str:
        if match.group(1).lower() == "git":  # an SSH remote (git@host:owner/repo), not a person
            return match.group(0)
        self.counts["email"] += 1
        return "<email>"

    def __call__(self, text: str) -> str:
        for pattern, placeholder in self.folders:
            text = self._sub(pattern, placeholder.replace("\\", "\\\\"), text,
                             "home" if placeholder == "~" else placeholder.strip("<>"))
        for pattern in _GENERIC_HOMES:
            text = self._sub(pattern, "~", text, "home")
        text = _EMAIL.sub(self._email, text)  # before names: an address often holds the login
        for pattern, placeholder, kind in self.names:
            text = self._sub(pattern, placeholder, text, kind)
        text = self._sub(_SCHEME_CREDENTIAL, lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text, "credential")
        lines = text.split("\n")
        redacted = [redact_urls(line) for line in lines]
        self.counts["url"] += sum(1 for before, after in zip(lines, redacted, strict=True) if before != after)
        return "\n".join(redacted)


def _notes(fields: Mapping[str, str], context: Context, unreplaced: dict[str, list[str]]) -> dict[str, list[str]]:
    joined = "\n".join(fields.values())
    notes: dict[str, list[str]] = {}
    for kind, names in unreplaced.items():
        present = [name for name in names if (pattern := _words([name])) and pattern.search(joined)]
        if present:
            notes[f"unreplaced_{kind}"] = present
    candidates = {name for name in context.project_names
                  if len(name) >= MIN_NAME_CHARS and name.lower() not in PUBLIC_NAMES}
    private = sorted(name for name in candidates if (pattern := _words([name])) and pattern.search(joined))
    if private:
        notes["possible_private_names"] = private[:MAX_LISTED]
    paths = list(dict.fromkeys(match.group(0) for match in _RESIDUAL_PATH.finditer(joined)))
    if paths:
        notes["residual_paths"] = paths[:MAX_LISTED]
    return notes


def clean(fields: Mapping[str, str], context: Context) -> Cleaned:
    """Steps 1 to 3 over every field; raises Refused with every reason found by the step that refused."""
    refusals = secret_hits(fields)
    if refusals:
        raise Refused(refusals)
    rewriter = _Rewriter(context)
    rewritten = {name: rewriter(text) for name, text in fields.items()}
    refusals = term_hits(rewritten, context.terms)
    if refusals:
        raise Refused(refusals)
    rewrites = {kind: count for kind, count in sorted(rewriter.counts.items()) if count}
    return Cleaned(rewritten, rewrites, _notes(rewritten, context, rewriter.unreplaced))
