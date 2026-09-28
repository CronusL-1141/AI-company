"""Write-side text rules shared by every layer: clean a single line, scan long text.

Two rules, one definition each:

- ``clean_text``: a single-line field (a title, a name, a sender, a tag) is cleaned
  on the way in: every control or format character becomes a space, whitespace is
  collapsed. The same rule the injection side applies (hook_core._sanitize_inline
  is its standard-library twin, held to identical output by a parity test).
- ``scan_invisible``: long text (a description, a report, a message body) keeps its
  layout and is refused, not rewritten, when it carries a character a reader cannot
  see: the author is present and can fix it, and silently rewriting a body could
  change what it says.

Standard library only and no aiteam import, so aiteam.types can build the request
field types on it and the hooks' server-side twins stay in step.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Unassigned in this Python's Unicode data but kept by clean_text: the emoji and
# symbol blocks, where newer Unicode versions keep adding pictographs (Python 3.12
# ships Unicode 15.0, so a 15.1 or 16.0 emoji reads as unassigned). Anywhere else an
# unassigned code point is still cleaned, so a format character added in a later
# version cannot slip through. hook_core._sanitize_inline and user_notice.clean_text
# carry the same table; tests/unit/test_sanitize_parity.py holds all of them equal.
_SYMBOL_BLOCKS = ((0x2600, 0x27BF), (0x2B00, 0x2BFF), (0x1F000, 0x1FBFF))


def _unsafe(char: str) -> bool:
    kind = unicodedata.category(char)
    if kind[0] != "C":
        return False
    return kind != "Cn" or not any(low <= ord(char) <= high for low, high in _SYMBOL_BLOCKS)


def clean_text(value: object) -> str:
    """One safe line: control/format characters to spaces, whitespace collapsed."""
    text = "" if value is None else str(value)
    text = "".join(" " if _unsafe(char) else char for char in text)
    return " ".join(text.split())


# Code points long text may not carry, as inclusive ranges. Anything here is
# unreadable to a human reviewer yet fully visible to the model. Written as code
# points, never as literal characters: a literal invisible character in this table
# would be unreviewable in exactly the way the check exists to prevent.
INVISIBLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x0008),  # C0 controls before TAB
    (0x000B, 0x000C),  # VT, FF (TAB, LF and CR are ordinary layout)
    (0x000E, 0x001F),  # the rest of C0, ESC included (terminal colour codes)
    (0x007F, 0x009F),  # DEL and the C1 controls
    (0x00AD, 0x00AD),  # SOFT HYPHEN
    (0x061C, 0x061C),  # ARABIC LETTER MARK (the Arabic counterpart of LRM / RLM)
    (0x180E, 0x180E),  # MONGOLIAN VOWEL SEPARATOR (a format character)
    (0x200B, 0x200B),  # zero-width space
    # U+200C ZERO WIDTH NON-JOINER and U+200D ZERO WIDTH JOINER are allowed: Persian,
    # Urdu and Devanagari spelling needs the first, emoji sequences the second.
    (0x200E, 0x200F),  # LTR / RTL marks
    (0x202A, 0x202E),  # bidi embedding / override
    (0x2060, 0x2064),  # word joiner + invisible operators
    (0x2066, 0x206F),  # bidi isolates + deprecated format characters
    (0xFEFF, 0xFEFF),  # BOM / zero-width no-break space
    (0xFFF9, 0xFFFB),  # interlinear annotation controls
    (0xE0000, 0xE007F),  # Unicode tag block (ASCII smuggling), except the flags below
)
# The only tag sequences a reader sees: the England, Scotland and Wales flags (black
# flag, tag letters, cancel tag). Exactly these three are exempt, so the tag block
# carries no free text: a made-up "flag" still hides letters and stays refused.
_RGI_TAG_FLAGS = re.compile("|".join(
    re.escape(chr(0x1F3F4) + "".join(chr(0xE0000 + ord(letter)) for letter in code) + chr(0xE007F))
    for code in ("gbeng", "gbsct", "gbwls")
))
_INVISIBLE = re.compile(
    "[" + "".join(f"{re.escape(chr(low))}-{re.escape(chr(high))}" for low, high in INVISIBLE_RANGES) + "]"
)

INVISIBLE_ADVICE = (
    "请去掉不可见字符后重写（多为从网页/终端复制带入）——肉眼不可见的内容不"
    "允许入库：审阅的人看不见它，读到记忆的模型却照单全收。"
)


@dataclass(frozen=True)
class SafetyFinding:
    """One rejection reason: which family fired, and where."""

    category: str  # invisible_unicode / prompt_injection / credential
    pattern: str  # human-readable pattern name
    position: int  # character offset of the match in the scanned text
    excerpt: str = ""  # short excerpt, empty for credentials (never echo a secret)
    advice: str = ""  # how to fix it; each scanner supplies its own family's advice

    @property
    def message(self) -> str:
        """Rejection text handed back to the caller (agent-readable, Chinese)."""
        head = f"内容安全扫描拒绝写入：命中 {self.pattern}（第 {self.position + 1} 字处）"
        if self.excerpt:
            head += f"：{self.excerpt}"
        return f"{head}。{self.advice}"


def _describe_char(ch: str) -> str:
    """Render one invisible code point as `U+XXXX (NAME)`."""
    try:
        name = unicodedata.name(ch)
    except ValueError:
        name = "CONTROL CHARACTER" if ord(ch) <= 0x9F else "UNNAMED FORMAT CHARACTER"
    return f"U+{ord(ch):04X} ({name})"


def scan_invisible(text: str) -> SafetyFinding | None:
    """The first invisible code point in ``text``, or None."""
    text = text or ""
    # Blank out the exempt flags first; same length, so positions still match ``text``.
    masked = _RGI_TAG_FLAGS.sub(lambda flag: " " * len(flag.group()), text)
    match = _INVISIBLE.search(masked)
    if match is None:
        return None
    return SafetyFinding(
        category="invisible_unicode",
        pattern=f"不可见字符 {_describe_char(match.group())}",
        position=match.start(),
        advice=INVISIBLE_ADVICE,
    )
