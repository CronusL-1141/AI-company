"""The fixed terms two nodes sync under: protocol version, timings, the handshake.

Every number an operator or a test may need to know lives here. The edge reaches
its center at ``center_url`` from ``node.json``; for the two-machine setup that is a
local port forwarded over SSH to the center's loopback API (``DEFAULT_CENTER_URL``),
kept up by the network side, never by OS. No API listens beyond loopback.
"""

from __future__ import annotations

import hashlib
import os

from aiteam.storage.models import Base
from aiteam.sync.tables import ORIGIN_COLUMN, SHARED_TABLES, SYNCED_TABLES

# Bumped on any change two nodes must agree on (envelope, feed, merge rules).
SYNC_PROTOCOL = 1
# An edge's row in sync_peers: its one center.
CENTER_PEER = "center"

# The SSH local forward on the edge: 127.0.0.1:18000 -> the center's 127.0.0.1:8000.
DEFAULT_CENTER_URL = "http://127.0.0.1:18000"
CENTER_API_PORT = 8000

# An attached edge pulls the center's changes every second (a
# write on the center is read on the edge within about a second plus a round trip)
# and probes the link every five.
PROBE_INTERVAL_S = 5.0
PULL_INTERVAL_S = 1.0
PROBE_TIMEOUT_S = 2.0
DETACH_AFTER_FAILURES = 2
ATTACH_AFTER_SUCCESSES = 3
FORWARD_TIMEOUT_S = 3.0
# Writes only allowed online (deletions, the ecosystem archive) may call slow services
# on the center (GitHub); they have no offline fallback, so they wait longer.
ONLINE_ONLY_TIMEOUT_S = 120.0
READ_YOUR_WRITE_WAIT_S = 1.0
# The queue needs a person when its head keeps failing or it has waited this long.
STUCK_ATTEMPTS = 3
BACKLOG_ALERT_S = 600.0
# Changes (tombstones included) the center keeps; an edge further behind
# rebuilds its replica.
TOMBSTONE_RETENTION_DAYS = 90
CHANGES_PAGE = 500
ROWS_PAGE = 200
# An edge keeps settled operations this long for diagnosis, then forgets them.
SETTLED_OP_RETENTION_DAYS = 7

# Request headers never carried in an operation: they describe one HTTP hop.
_HOP_HEADERS = frozenset({
    "host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "trailer",
    "upgrade", "accept-encoding", "cookie", "authorization", "proxy-authorization",
})
# Response headers worth handing back from the center.
RESPONSE_HEADERS = frozenset({"content-type"})
# Marks a response the edge produced by standing in for an operation.
STAGED_HEADER = "x-aiteam-sync"


def timing(name: str, default: float) -> float:
    """A timing, overridable by ``AITEAM_SYNC_<NAME>`` (tests shorten the probe)."""
    raw = os.environ.get(f"AITEAM_SYNC_{name.upper()}", "")
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def operation_headers(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    """The request headers an operation carries: everything but the hop's own."""
    kept: dict[str, str] = {}
    for raw_name, raw_value in headers:
        name = raw_name.decode("latin-1").lower()
        if name not in _HOP_HEADERS:
            kept[name] = raw_value.decode("latin-1")
    return kept


def schema_version() -> str:
    """A fingerprint of the synced tables' columns: two nodes sync only when they agree."""
    lines = []
    for name in sorted(SYNCED_TABLES):
        table = Base.metadata.tables[name]
        face = "shared" if name in SHARED_TABLES else "session"
        columns = ",".join(sorted(c.name for c in table.columns if c.name != ORIGIN_COLUMN))
        lines.append(f"{name}:{face}:{columns}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]
