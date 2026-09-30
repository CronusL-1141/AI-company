"""The usage re-measure's parse child (see aiteam.api.usage_remeasure): paths in on stdin, results out on stdout.

Kept apart from usage_remeasure on purpose: this is all the child imports, the
transcript parser and the standard library. usage_remeasure pulls in the repository,
SQLAlchemy and the rest of the API, which the child has no use for and which made
every batch start pay seconds of imports.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from aiteam.services.token_attribution import parse_transcript_usage, unmeasurable_reason

_LAYERS = ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")


def unbilled(usage: dict[str, Any] | None) -> bool:
    """No billed usage in this parse: nothing at all, or only calls with every layer 0 and
    no model - the shape the parser gave for synthetic (rate limit, server error) lines
    before it learned to skip them. Never written as "measured 0" (the founder's P-3)."""
    if not usage:
        return True
    return not usage.get("model") and all(not usage.get(layer) for layer in _LAYERS)


def _signature(path: str) -> list[int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def measure_file(path: str) -> dict[str, Any]:
    """Parse one transcript; the signature is None when the file was gone or changed meanwhile."""
    before = _signature(path)
    if before is None:
        return {"usage": None, "signature": None, "read_at": 0.0}
    read_at = time.time()
    try:
        usage = parse_transcript_usage(path)
        # Why there is nothing to count, from the same function SubagentStop asks.
        reason = unmeasurable_reason(path) if unbilled(usage) else None
    except Exception as exc:  # noqa: BLE001 - one bad file must not cost the batch
        return {"usage": None, "signature": before, "read_at": read_at, "error": f"{type(exc).__name__}: {exc}"}
    return {"usage": usage, "unmeasurable": reason,
            "signature": before if _signature(path) == before else None, "read_at": read_at}


def main() -> None:
    request = json.loads(sys.stdin.buffer.read())
    json.dump([measure_file(path) for path in request["paths"]], sys.stdout, separators=(",", ":"))
