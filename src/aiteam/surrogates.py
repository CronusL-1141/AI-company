"""Lone surrogates: find them, replace them, and keep them out of stored JSON.

A lone surrogate (a code point in U+D800-U+DFFF on its own) is what a JSON
``\\ud800`` escape decodes to. It has no UTF-8 encoding. A TEXT column refuses it
on write, but JSON text stores it as an ASCII escape, so the row lands and every
later read that serializes it fails: one poisoned memories row made
``GET /api/memories`` answer 400 and emptied the direction layer of every session
start.

Three users, one definition:
- the API refuses request bodies that carry one (middleware);
- request models fed by hooks replace it, since a refused hook event is lost
  (``SurrogateTolerantBody`` in aiteam.types);
- the storage layer serializes every JSON value through ``json_dumps``, the one
  place internal writers (no request body, no author to refuse) also pass.

Standard library only, and no import of the storage package, so the read-only
health scan can use it without the storage package's import-time side effects.
"""

from __future__ import annotations

import json
import re

# Built from code points so this source stays plain ASCII.
_LONE_SURROGATE = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")
REPLACEMENT = chr(0xFFFD)


def has_lone_surrogate(obj: object) -> bool:
    """True when any string in a JSON-shaped structure, key or value, holds a lone surrogate."""
    stack = [obj]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _LONE_SURROGATE.search(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


def replace_lone_surrogates(obj: object) -> object:
    """Copy of a JSON-shaped structure with every lone surrogate replaced by U+FFFD."""
    if isinstance(obj, str):
        return _LONE_SURROGATE.sub(REPLACEMENT, obj)
    if isinstance(obj, dict):
        return {replace_lone_surrogates(k): replace_lone_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [replace_lone_surrogates(item) for item in obj]
    return obj


def json_dumps(obj: object) -> str:
    """``json.dumps`` for stored JSON, with lone surrogates written as U+FFFD.

    The output is byte-identical to ``json.dumps`` for every value without one.
    Fast path: the ASCII-escaped text shows any surrogate, paired (an emoji) or
    lone, as a ``\\ud`` escape, so the structure is walked only when that
    substring appears, and copied only when a lone one is really there.
    """
    text = json.dumps(obj)
    if "\\ud" in text and has_lone_surrogate(obj):
        text = json.dumps(replace_lone_surrogates(obj))
    return text
