"""Write-side content safety scan for the memory subsystem (single source of truth).

Direction-layer entries are compiled into the system prompt of **every** dispatched
agent, which makes `memory_add` an injection amplifier: one poisoned entry reaches
every future sub-agent of the team. The scan therefore runs at write time, where the
author is still present to fix the content — not at injection time, where it is both
too late and too hot a path.

Three families are checked:

- invisible Unicode (zero-width, bidi override, deprecated format chars, tag block)
  — the classic way to smuggle text a human reviewer cannot see;
- instruction-override phrasing (Chinese and English) and forged role/turn markers
  — the payload shape of a prompt-injection entry;
- credential shapes (private key headers, provider key formats, key=value secrets)
  — a direction entry is the worst place in the system to park a secret.

Task memos take the invisible-Unicode scan only: they are a high-frequency path and
are not compiled into anyone's system prompt.

Hooks never import this module. They are pure-stdlib processes by design, and the
pattern table must not be duplicated into them — the check belongs on the write side.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ================================================================
# Pattern tables
# ================================================================

# Invisible / formatting code points, as inclusive ranges. Anything here is
# unreadable to a human reviewer yet fully visible to the model, so it is rejected
# regardless of intent. Expressed as code points rather than a literal character
# class on purpose: literal invisible characters in the source would be unreviewable
# in exactly the way this check exists to prevent.
_INVISIBLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),  # SOFT HYPHEN
    (0x200B, 0x200F),  # zero-width space/non-joiner/joiner + LTR/RTL marks
    (0x202A, 0x202E),  # bidi embedding / override
    (0x2060, 0x2064),  # word joiner + invisible operators
    (0x2066, 0x206F),  # bidi isolates + deprecated format characters
    (0xFEFF, 0xFEFF),  # BOM / zero-width no-break space
    (0xE0000, 0xE007F),  # Unicode tag block (ASCII smuggling)
)

# Instruction-override phrasing and forged conversation structure. Kept narrow on
# purpose: a legitimate direction entry *is* an instruction ("all output in Chinese"),
# so only the override/exfiltration shapes are matched, never plain imperatives.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction override (en)",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+)*"
            r"(?:previous|prior|preceding|above|earlier|system|original)\s+"
            r"(?:instruction|instructions|prompt|prompts|rule|rules|message|messages)",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction override (zh)",
        re.compile(
            r"(?:忽略|无视|忘掉|忘记|抛开"
            r"|覆盖|不要理会|不用理会)"
            r"[^。；\n]{0,8}"
            r"(?:上面|以上|上述|之前|先前"
            r"|前面|原有|所有|全部)"
            r"[^。；\n]{0,8}"
            r"(?:指令|指示|提示词|系统提示"
            r"|规则|命令|约束|设定)"
        ),
    ),
    (
        "system prompt exfiltration (en)",
        re.compile(
            r"\b(?:reveal|print|show|output|repeat|dump|leak)\s+(?:me\s+)?"
            r"(?:your\s+|the\s+|all\s+)*"
            r"(?:system\s+prompt|system\s+message|initial\s+instructions|"
            r"hidden\s+instructions)",
            re.IGNORECASE,
        ),
    ),
    (
        "system prompt exfiltration (zh)",
        re.compile(
            r"(?:输出|打印|复述|泄露|展示"
            r"|告诉我|贴出)[^。；\n]{0,8}"
            r"(?:系统提示|系统指令|初始指令"
            r"|隐藏指令|完整提示词)"
        ),
    ),
    (
        "forged role / turn marker",
        re.compile(r"<\|[^|>\n]{1,32}\|>|\[/?INST\]|<<\s*SYS\s*>>"),
    ),
    (
        "persona takeover",
        re.compile(
            r"\bfrom\s+now\s+on[, ]+you\s+are\b|\byou\s+are\s+now\s+(?:a|an|in)\b"
            r"|(?:从现在起|从此以后|接下来)"
            r"[，,]?\s*你(?:就)?是",
            re.IGNORECASE,
        ),
    ),
    (
        "jailbreak marker",
        re.compile(
            r"\bDAN\s+mode\b|\bjailbreak\b|\bdeveloper\s+mode\s+enabled\b"
            r"|越狱模式|开发者模式已启用",
            re.IGNORECASE,
        ),
    ),
)

# Credential shapes. Matched values are never echoed back — only the family name and
# the offset, so the rejection message cannot become a second copy of the secret.
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key header", re.compile(r"-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----")),
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("OpenAI-shaped API key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{28,}\b")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    (
        "key=value secret",
        re.compile(
            r"(?:api[_-]?key|access[_-]?token|secret[_-]?key|client[_-]?secret"
            r"|password|passwd)\s*[:=]\s*[\"']?[A-Za-z0-9/+=_\-]{20,}",
            re.IGNORECASE,
        ),
    ),
)

_CATEGORY_ADVICE = {
    "invisible_unicode": (
        "请去掉不可见字符后重写（多为从网页/终端复制带入）——肉眼不可见的内容不"
        "允许入库：审阅的人看不见它，读到记忆的模型却照单全收。"
    ),
    "prompt_injection": (
        "方向层条目是所有派出 agent 的常驻指令，不接受「覆盖既有指令 / 套取系统提示 / "
        "伪造对话角色」形态的内容。如确为正常表述，请换一种不含该句式的写法。"
    ),
    "credential": (
        "凭据不进记忆层。请改写成「指针条目」——只写触发条件与凭据所在文件路径，"
        "密钥本体留在该文件里。"
    ),
}


@dataclass(frozen=True)
class SafetyFinding:
    """One rejection reason: which family fired, and where."""

    category: str  # invisible_unicode / prompt_injection / credential
    pattern: str  # human-readable pattern name
    position: int  # character offset of the match in the scanned text
    excerpt: str = ""  # short excerpt, empty for credentials (never echo a secret)

    @property
    def message(self) -> str:
        """Rejection text handed back to the caller (agent-readable, Chinese)."""
        head = f"内容安全扫描拒绝写入：命中 {self.pattern}（第 {self.position + 1} 字处）"
        if self.excerpt:
            head += f"：{self.excerpt}"
        return f"{head}。{_CATEGORY_ADVICE.get(self.category, '')}"


def _describe_char(ch: str) -> str:
    """Render one invisible code point as `U+XXXX (NAME)`."""
    try:
        name = unicodedata.name(ch)
    except ValueError:
        name = "UNNAMED FORMAT CHARACTER"
    return f"U+{ord(ch):04X} ({name})"


def scan_invisible(text: str) -> SafetyFinding | None:
    """Scan for invisible/formatting code points. Returns the first finding or None."""
    for index, ch in enumerate(text or ""):
        code_point = ord(ch)
        if any(low <= code_point <= high for low, high in _INVISIBLE_RANGES):
            return SafetyFinding(
                category="invisible_unicode",
                pattern=f"不可见字符 {_describe_char(ch)}",
                position=index,
            )
    return None


def scan_direction_content(text: str) -> SafetyFinding | None:
    """Full write-side scan for direction-layer content. Returns the first finding.

    Order is deliberate: invisible characters first, because they can hide the very
    phrases the other two families look for.
    """
    text = text or ""
    finding = scan_invisible(text)
    if finding is not None:
        return finding

    for name, pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return SafetyFinding(
                category="prompt_injection",
                pattern=f"提示注入模式「{name}」",
                position=match.start(),
                excerpt=f"「{match.group()[:40]}」",
            )

    for name, pattern in _CREDENTIAL_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            # No excerpt: echoing the match would duplicate the secret into the log.
            return SafetyFinding(
                category="credential",
                pattern=f"凭据形态「{name}」",
                position=match.start(),
            )

    return None
