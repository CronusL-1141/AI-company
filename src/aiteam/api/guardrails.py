"""L1 Guardrails — basic input validation and sanitization.

Provides lightweight regex-based checks for dangerous patterns and PII.
This is intentionally minimal: no ML, no semantic analysis.
Only checks actual API input parameters, not discussion content in text fields.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dangerous pattern detection — these trigger a 400 rejection
# ---------------------------------------------------------------------------

# Each entry: (compiled_pattern, human-readable label)
_DANGEROUS_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rm\s+-rf\s+[/~]", re.IGNORECASE), "destructive shell command"),
    (re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE), "SQL DROP TABLE"),
    (re.compile(r"<script\b[^>]*>", re.IGNORECASE), "XSS script tag"),
    (re.compile(r"__import__\s*\(", re.IGNORECASE), "Python code injection (__import__)"),
    (re.compile(r"\beval\s*\(", re.IGNORECASE), "code injection (eval)"),
    (re.compile(r"\bexec\s*\(", re.IGNORECASE), "code injection (exec)"),
    (re.compile(r"(?:\.\.[\\/]){2,}", re.IGNORECASE), "path traversal"),
]

# ---------------------------------------------------------------------------
# PII detection — these only warn (log), never block
# ---------------------------------------------------------------------------

_PII_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "SSN"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "email address"),
]

# ---------------------------------------------------------------------------
# Output sanitization — redact accidental secret leaks
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Generic API key patterns (long alphanumeric tokens after common prefixes)
    (re.compile(r"(api[_-]?key\s*[=:]\s*)['\"]?[\w\-]{16,}['\"]?", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(secret\s*[=:]\s*)['\"]?[\w\-]{8,}['\"]?", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(password\s*[=:]\s*)['\"]?[^\s'\"]{6,}['\"]?", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(token\s*[=:]\s*)['\"]?[\w\-\.]{16,}['\"]?", re.IGNORECASE), r"\1[REDACTED]"),
    # Bearer tokens in Authorization headers
    (re.compile(r"(Bearer\s+)[\w\-\.]{20,}", re.IGNORECASE), r"\1[REDACTED]"),
]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Maximum string length checked (truncate beyond this to avoid regex DoS)
_MAX_CHECK_LEN = 10_000


def check_input(text: str) -> dict[str, object]:
    """Check input text for dangerous patterns.

    Args:
        text: The input string to inspect.

    Returns:
        A dict with keys:
          - safe (bool): False if any dangerous pattern matched.
          - violations (list[str]): Labels of matched dangerous patterns.
          - warnings (list[str]): Labels of matched PII patterns (non-blocking).
    """
    if not isinstance(text, str):
        return {"safe": True, "violations": [], "warnings": []}

    # Truncate to avoid catastrophic backtracking on huge payloads
    sample = text[:_MAX_CHECK_LEN]

    violations: list[str] = []
    warnings: list[str] = []

    for pattern, label in _DANGEROUS_RULES:
        if pattern.search(sample):
            violations.append(label)

    for pattern, label in _PII_RULES:
        if pattern.search(sample):
            warnings.append(label)
            logger.warning("PII detected in input (%s) — not blocking, logging only", label)

    return {
        "safe": len(violations) == 0,
        "violations": violations,
        "warnings": warnings,
    }


def sanitize_output(text: str) -> str:
    """Redact accidental secret leaks in output text.

    Args:
        text: Output string that may contain sensitive values.

    Returns:
        Sanitized string with secrets replaced by [REDACTED].
    """
    if not isinstance(text, str):
        return text

    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def check_dict(data: dict[str, object], *, path: str = "") -> dict[str, object]:
    """Recursively check all string values in a dict/list structure.

    Args:
        data: The dict (or list) to inspect.
        path: Dot-notation path for error messages (internal use).

    Returns:
        Same structure as check_input — aggregated across all values.
    """
    all_violations: list[str] = []
    all_warnings: list[str] = []

    _collect(data, all_violations, all_warnings, path)

    return {
        "safe": len(all_violations) == 0,
        "violations": all_violations,
        "warnings": all_warnings,
    }


def _collect(
    obj: object,
    violations: list[str],
    warnings: list[str],
    path: str,
) -> None:
    """Recursively walk obj and accumulate findings."""
    if isinstance(obj, str):
        result = check_input(obj)
        for v in result["violations"]:  # type: ignore[union-attr]
            entry = f"{path}: {v}" if path else v
            if entry not in violations:
                violations.append(entry)
        for w in result["warnings"]:  # type: ignore[union-attr]
            entry = f"{path}: {w}" if path else w
            if entry not in warnings:
                warnings.append(entry)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _collect(v, violations, warnings, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _collect(item, violations, warnings, f"{path}[{i}]")
