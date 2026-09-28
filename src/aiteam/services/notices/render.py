"""Rendering rules for user-facing notices (docs/user-notice-design.md §4).

These rules are normative for every renderer, including the hook-side copy in
``hooks/user_notice.py`` that renders the local entries without the API. Tests
pin the two to the same output, so a change here must land there in the same
batch:

* Every user line starts with ``[AI Team OS] `` (``PREFIX``). The prefix is never
  coloured; colour starts at the first body character, runs to the end of the
  line and ends with ``ESC[39m`` (foreground reset only). ``ESC[0m``, bold and
  reverse video are never used, and no uncoloured text follows a coloured run.
* Colour is applied only for host ``cc`` when ``CLAUDE_CODE_ENTRYPOINT == "cli"``.
* Width is measured in terminal columns: East Asian Width W or F counts 2,
  everything else 1, ANSI sequences 0. A rendered line never exceeds 160.
* Parameters are external text. Control and format characters (Unicode
  categories C*, which includes ESC) become spaces, whitespace is collapsed.
  Inside the user line (not the model note) the tokens the hook refuses to
  print are neutralised (``line_safe``): a URL scheme is dropped, ``http``
  becomes ``HTTP``, a backtick becomes ``'``, ``**`` becomes ``*`` and ``](``
  becomes ``] (``. Otherwise the hook would drop the whole line. A value
  longer than its limit is cut to ``limit - 1`` characters plus ``…``
  (tail parameters keep the end instead: ``…`` plus the last ``limit - 1``). If
  the line is still wider than 160 columns, the widest parameter loses one more
  character (same rule) until the line fits.
* ``{assistant}`` renders as Claude or Codex, ``{host_app}`` as Claude Code or
  Codex. Model notes are framed per entry (see ``catalog.CatalogEntry.frame``).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

# clean_text is the shared single-line rule (aiteam.text_safety); re-exported here
# because the notice catalog and its callers have always read it from render.
from aiteam.text_safety import clean_text  # noqa: F401
from aiteam.types import NoticeColor, NoticeKind

PREFIX = "[AI Team OS] "
MAX_WIDTH = 160
ELLIPSIS = "…"
RESET_FG = "\x1b[39m"
ASSISTANT = {"cc": "Claude", "codex": "Codex"}
HOST_APP = {"cc": "Claude Code", "codex": "Codex"}
KIND_COLOR: Mapping[NoticeKind, NoticeColor] = {
    NoticeKind.STATUS: NoticeColor.DEFAULT,
    NoticeKind.ACTION: NoticeColor.YELLOW,
    NoticeKind.DECISION: NoticeColor.YELLOW,
    NoticeKind.BLOCKED: NoticeColor.RED,
    NoticeKind.DONE: NoticeColor.GREEN,
}
COLOR_SEQUENCE: Mapping[NoticeColor, str] = {
    NoticeColor.DEFAULT: "",
    NoticeColor.YELLOW: "\x1b[33m",
    NoticeColor.RED: "\x1b[31m",
    NoticeColor.GREEN: "\x1b[32m",
}

# Model-note frames. ``{line}`` is the plain user line (prefix included).
MODEL_HEADER = {
    ("zh", True): "AI Team OS 刚在界面上向用户显示了以下提示（systemMessage 不进你的上下文，这里是原文）：{line}",
    ("zh", False): "AI Team OS 尝试向用户显示以下提示，界面可能没有显示：{line}",
    ("en", True): (
        "AI Team OS just showed the user this notice "
        "(systemMessage does not reach your context; this is the original text): {line}"
    ),
    ("en", False): "AI Team OS tried to show the user this notice; the interface may not have displayed it: {line}",
}
# API only (hooks never hold a line back): the budget kept the user line off the
# screen, but the entry is one the model acts on, so the note still goes out.
MODEL_HEADER_HELD = {
    "zh": "AI Team OS 因提示行数已到上限，没有在界面上向用户显示以下提示，只交给你处理：{line}",
    "en": "AI Team OS did not show the user this notice (the display limit was reached); it is passed to you: {line}",
}
MODEL_CLOSING = {
    "zh": "用户问起或说出动作句时再处理，不必主动复述。",
    "en": "Act on it when the user asks or says the action phrase; do not repeat it unprompted.",
}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_FIELD = re.compile(r"\{([a-z_]+)\}")
# "{n?one|other}": the first form when the parameter is 1, the second otherwise
# (English count agreement; Chinese texts do not need it).
_PLURAL = re.compile(r"\{([a-z_]+)\?([^|{}]*)\|([^|{}]*)\}")
_URL_SCHEME = re.compile(r"(?i)https?://")
_STARS = re.compile(r"\*{2,}")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences."""
    return _ANSI.sub("", text)


def display_width(text: str) -> int:
    """Terminal columns: W/F East Asian Width counts 2, ANSI sequences 0."""
    return sum(
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        for char in strip_ansi(text)
    )


def truncate(text: str, limit: int, *, tail: bool = False) -> str:
    """Cut ``text`` to ``limit`` characters including the ellipsis."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return ELLIPSIS
    return ELLIPSIS + text[-(limit - 1):] if tail else text[: limit - 1] + ELLIPSIS


def line_safe(text: str) -> str:
    """Neutralise, inside one parameter, what the hook refuses to print in a user line.

    ``hooks/user_notice.emit`` drops a line containing ``**``, a backtick, ``](``
    or ``http``; a parameter carrying them (a briefing title, a branch named
    ``feat/http2``) must not make its whole notice disappear.
    """
    text = _URL_SCHEME.sub("", text)
    text = text.replace("http", "HTTP").replace("`", "'").replace("](", "] (")
    return _STARS.sub("*", text)


def clean_block(value: object, limit: int) -> str:
    """Multi-line model-only text: each line cleaned, newlines kept, total capped."""
    lines = [clean_text(line) for line in ("" if value is None else str(value)).splitlines()]
    return truncate("\n".join(line for line in lines if line), limit)


def template_fields(template: str) -> set[str]:
    """Placeholder names used by a template."""
    return set(_FIELD.findall(template))


def color_of(kind: NoticeKind) -> NoticeColor:
    """Colour mapping from §4.3."""
    return KIND_COLOR[kind]


def colorize(body: str, kind: NoticeKind, *, host: str, entrypoint: str) -> str:
    """Colour the body only when the host renders it (CC CLI)."""
    sequence = COLOR_SEQUENCE[color_of(kind)]
    if not sequence or host != "cc" or entrypoint != "cli":
        return body
    return sequence + body + RESET_FG


def fill(template: str, values: Mapping[str, str]) -> str:
    """Substitute ``{name}`` placeholders (unknown names render empty) and ``{name?one|other}`` forms."""
    template = _PLURAL.sub(
        lambda match: match.group(2) if str(values.get(match.group(1), "")).strip() == "1" else match.group(3),
        template,
    )
    return _FIELD.sub(lambda match: values.get(match.group(1), ""), template)


def fit_line(
    template: str,
    base: Mapping[str, str],
    params: Mapping[str, str],
    limits: Mapping[str, int],
    tails: frozenset[str] = frozenset(),
) -> str:
    """Render ``PREFIX + template`` within ``MAX_WIDTH`` columns.

    ``params`` are cleaned but not yet truncated. Each is first cut to its limit;
    while the line is too wide, the parameter with the widest rendered value
    (ties broken by name) loses one more character. Static text is never cut.
    """
    lengths = {name: min(len(value), limits.get(name, len(value))) for name, value in params.items()}
    used = template_fields(template)
    while True:
        values = {
            name: truncate(value, lengths[name], tail=name in tails)
            for name, value in params.items()
        }
        line = PREFIX + fill(template, {**base, **values})
        if display_width(line) <= MAX_WIDTH:
            return line
        shrinkable = [name for name in sorted(used) if name in values and lengths[name] > 1]
        if not shrinkable:
            return line
        widest = max(shrinkable, key=lambda name: display_width(values[name]))
        lengths[widest] -= 1
