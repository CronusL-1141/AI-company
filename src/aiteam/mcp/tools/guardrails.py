"""Guardrails MCP tools — manual safety check for input text."""

from __future__ import annotations

from typing import Any

from aiteam.api.guardrails import check_dict, check_input, sanitize_output


def register(mcp):
    """Register guardrail MCP tools."""

    @mcp.tool()
    def guardrail_check(text: str) -> dict[str, Any]:
        """Manually check whether a text string contains dangerous patterns or PII.

        Useful for Agents to pre-validate content before sending it to the API
        or including it in task descriptions.

        Args:
            text: The input text to inspect.

        Returns:
            A dict with:
              - safe (bool): True if no dangerous patterns found.
              - violations (list[str]): Dangerous pattern labels (would be blocked by API).
              - warnings (list[str]): PII pattern labels (informational only).
              - sanitized (str): Output-sanitized version of the text (secrets redacted).
        """
        result = check_input(text)
        sanitized = sanitize_output(text)
        return {
            "safe": result["safe"],
            "violations": result["violations"],
            "warnings": result["warnings"],
            "sanitized": sanitized,
        }

    @mcp.tool()
    def guardrail_check_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Check a full dict/JSON payload for dangerous patterns.

        Recursively inspects all string values in the payload.
        Useful before calling task_run or other write tools with user-supplied content.

        Args:
            payload: The dict payload to inspect.

        Returns:
            A dict with safe (bool), violations (list), and warnings (list).
        """
        return check_dict(payload)
