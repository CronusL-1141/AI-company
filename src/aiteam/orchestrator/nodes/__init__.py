"""AI Team OS — Orchestration nodes."""

import re
from typing import Any

# The one place this path names a concrete model: the Anthropic API takes no tier
# alias, so this string moves when the target model does.
DEFAULT_LLM_MODEL = "claude-opus-5-5"

# Thinking is always on for the target model and counts toward max_tokens, so a
# client default sized for replies without thinking cuts the reply off. A ceiling
# this high needs streaming: a non-streaming call may run past HTTP timeouts.
LLM_MAX_TOKENS = 64_000

_API_MODEL_ID = re.compile(r"claude-[a-z0-9]+(?:-[a-z0-9]+)*")
_CONTEXT_TAG = re.compile(r"\[[^\]]*\]$")


def api_model_id(value: str | None) -> str | None:
    """Return ``value`` as a Messages API model ID, or None when it is not one.

    ``agents.model`` is backfilled from observation, so it can be empty, a tier
    alias (``opus``), another vendor's model, or carry a client-side context tag
    (``claude-opus-5-5[1m]``). The tag is dropped; anything else that is not a
    ``claude-*`` ID is rejected so the caller falls back to a real model.
    """
    if not value:
        return None
    candidate = _CONTEXT_TAG.sub("", value.strip())
    return candidate if _API_MODEL_ID.fullmatch(candidate) else None


def response_text(message: Any) -> str:
    """Return only the text of a model reply.

    With thinking on, ``message.content`` is a list of blocks (thinking blocks
    first, then text) instead of a string; state fields hold the text alone.
    """
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        block if isinstance(block, str) else block.get("text", "")
        for block in content
        if isinstance(block, str) or (isinstance(block, dict) and block.get("type") == "text")
    )
