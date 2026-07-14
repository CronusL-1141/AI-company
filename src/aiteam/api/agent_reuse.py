"""AI Team OS — agent reuse recommendation logic (P2 decision layer, batch 3B).

Given a follow-up task's domain and the pool of prior sub-agents (each carrying a
P1 context watermark), rank reuse candidates and recommend one of three actions:
direct reuse / slim-then-reuse / spawn new. The tool only recommends; the Leader
decides. See docs/agent-reuse-design.md section 5.

Pure functions with no I/O so the decision tree and availability inference are
directly unit-testable; the API route feeds them agent rows and the caller's
session id. All decision thresholds are centralized below and are meant to be
tuned once real watermark distributions are collected (open question, section 9).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Tunable decision thresholds (calibration is an open question — see
# docs/agent-reuse-design.md sections 5.3 and 9). Centralized here so they can be
# adjusted from one place once real watermark distributions land. Percentage and
# absolute-token limits are both applied, whichever trips first, so a mis-detected
# context window cannot silently keep a bloated agent in the "reuse" bucket.
# ---------------------------------------------------------------------------
DOMAIN_MATCH_MIN = 0.5  # below this -> spawn new (cross-domain contamination guard)
CTX_PCT_REUSE_MAX = 60.0  # below this (and token floor) -> direct reuse
CTX_PCT_SLIM_MAX = 85.0  # below this -> slim then reuse; at/above -> spawn new
CTX_TOKENS_REUSE_MAX = 120_000  # absolute floor guarding against window mis-detection
CTX_TOKENS_SLIM_MAX = 180_000
CLEANUP_WINDOW_DAYS = 30  # transcript retention (CC cleanupPeriodDays default)

# Recommended actions.
ACTION_REUSE = "reuse"
ACTION_SLIM = "slim_then_reuse"
ACTION_NEW = "spawn_new"

# Availability tiers (see section 5.4).
AVAIL_LIVE = "live"  # same live session, busy/waiting -> SendMessage reaches it now
AVAIL_RESUMABLE = "resumable"  # same session, offline but transcript fresh -> auto-resume
AVAIL_CROSS_SESSION = "cross-session"  # another session -> needs claude --resume, not direct
AVAIL_EXPIRED = "expired"  # transcript past retention window -> cannot resume

# Roles that are never reuse candidates (managed elsewhere).
_EXCLUDED_ROLES = frozenset({"leader", "workflow-subagent"})

# Domain tokenization: ASCII words (>=2 chars) plus individual CJK characters, so
# both English-ish role/task text and Chinese task descriptions yield some signal.
_ASCII_WORD_RE = re.compile(r"[a-z0-9]{2,}")
_CJK_RE = re.compile(r"[一-鿿]")
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "with",
        "is", "are", "be", "task", "agent", "use", "the", "this", "that",
        "的", "了", "和", "与", "在", "是", "做", "个", "任务",
    }
)


def tokenize(text: str) -> set[str]:
    """Extract a comparable token set: ASCII words + CJK chars, minus stopwords."""
    raw = text or ""
    tokens = set(_ASCII_WORD_RE.findall(raw.lower()))
    tokens.update(_CJK_RE.findall(raw))
    return tokens - _STOPWORDS


def domain_match(query_tokens: set[str], agent_tokens: set[str]) -> float:
    """Overlap coefficient between two token sets: |A n B| / min(|A|, |B|).

    Overlap (not Jaccard) because the token sets are short and asymmetric; it
    rewards a candidate whose domain covers the query terms. Returns 0.0 when
    either set is empty.
    """
    if not query_tokens or not agent_tokens:
        return 0.0
    inter = len(query_tokens & agent_tokens)
    return round(inter / min(len(query_tokens), len(agent_tokens)), 3)


def _is_fresh(
    ctx_measured_at: datetime | None,
    last_active_at: datetime | None,
    now: datetime,
) -> bool:
    """True when the agent's transcript is within the retention window."""
    ref = ctx_measured_at or last_active_at
    if ref is None:
        return False
    return (now - ref) <= timedelta(days=CLEANUP_WINDOW_DAYS)


def infer_availability(
    *,
    status: str,
    session_id: str | None,
    ctx_measured_at: datetime | None,
    last_active_at: datetime | None,
    now: datetime,
    caller_session_id: str | None = None,
) -> str:
    """Classify how reachable a candidate is (section 5.4).

    - expired: transcript past the retention window (cannot resume at all).
    - cross-session: a different session than the caller's (visible but not
      directly addressable; needs ``claude --resume <session>``).
    - live: same/unknown session and currently busy or waiting.
    - resumable: same/unknown session, offline but transcript still fresh.

    When ``caller_session_id`` is omitted the cross-session distinction cannot be
    made, so it degrades to status-based inference and the caller inspects each
    candidate's raw ``session_id`` itself.
    """
    if not _is_fresh(ctx_measured_at, last_active_at, now):
        return AVAIL_EXPIRED
    if caller_session_id and session_id and session_id != caller_session_id:
        return AVAIL_CROSS_SESSION
    status_l = str(status or "").lower()
    if status_l.endswith("busy") or status_l.endswith("waiting"):
        return AVAIL_LIVE
    return AVAIL_RESUMABLE


def recommend_action(
    *,
    dmatch: float,
    availability: str,
    ctx_pct: float | None,
    ctx_tokens: int | None,
) -> tuple[str, str]:
    """Apply the three-way decision tree (section 5.3). Returns (action, rationale)."""
    if dmatch < DOMAIN_MATCH_MIN:
        return ACTION_NEW, (
            f"域匹配 {dmatch:.2f} < {DOMAIN_MATCH_MIN} — 跨域续用有上下文污染风险，建议新开"
        )
    if availability == AVAIL_EXPIRED:
        return ACTION_NEW, "transcript 已过保留窗口，无法续用，建议新开"
    if availability == AVAIL_CROSS_SESSION:
        return ACTION_NEW, (
            "候选在别的会话，当前会话不可直接寻址；建议新开，或 claude --resume 原会话后续用"
        )
    # Domain matches and the candidate is reachable (live / resumable).
    pct = ctx_pct or 0.0
    tokens = ctx_tokens or 0
    unknown = ctx_tokens is None
    note = "（水位未测，按低处理）" if unknown else f"（{pct:.0f}% / {tokens} tok）"
    if pct < CTX_PCT_REUSE_MAX and tokens < CTX_TOKENS_REUSE_MAX:
        return ACTION_REUSE, f"同域 + 水位低{note} — 直接续用，认知完整延续"
    if pct < CTX_PCT_SLIM_MAX and tokens < CTX_TOKENS_SLIM_MAX:
        return ACTION_SLIM, f"同域 + 水位中{note} — 先让其自总结再新开继承摘要"
    return ACTION_NEW, f"同域但水位高{note} — 续用将很快触顶，建议新开"


def resume_hint(action: str, availability: str, cc_tool_use_id: str, session_id: str | None) -> str:
    """Concrete next-step for the Leader. Addresses by cc id (agentId), not name."""
    ref = cc_tool_use_id or "<agentId>"
    if action == ACTION_REUSE:
        return f"SendMessage(to='{ref}') 直接续用"
    if action == ACTION_SLIM:
        return (
            f"SendMessage(to='{ref}') 让其产出交接摘要 → report_save/task_memo_add "
            "→ 新开同域 agent 承接摘要"
        )
    # spawn_new
    if availability == AVAIL_CROSS_SESSION and session_id:
        return f"新开全新 agent；或 claude --resume {session_id} 恢复原会话后 SendMessage(to='{ref}')"
    return "新开全新 agent（干净隔离上下文）"


# Ranking: prefer higher domain match, then more reachable, then lower watermark.
_AVAIL_RANK = {AVAIL_LIVE: 0, AVAIL_RESUMABLE: 1, AVAIL_CROSS_SESSION: 2, AVAIL_EXPIRED: 3}


def is_reuse_candidate(role: str, cc_tool_use_id: str | None) -> bool:
    """A row is a candidate only if it is a resumable sub-agent (has a cc id) and
    is not a leader / workflow fan-out agent (managed by other subsystems)."""
    if not cc_tool_use_id:
        return False
    return str(role or "").lower() not in _EXCLUDED_ROLES


def build_recommendations(
    *,
    agents: list[Any],
    query_text: str,
    now: datetime,
    caller_session_id: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Rank reuse candidates and pick a default recommendation.

    ``agents`` are Agent pydantic rows (already scoped to the relevant project).
    Non-candidates (leaders, workflow agents, rows without a cc id) are skipped.
    """
    query_tokens = tokenize(query_text)
    candidates: list[dict[str, Any]] = []
    for a in agents:
        cc_id = getattr(a, "cc_tool_use_id", None)
        role = getattr(a, "role", "") or ""
        if not is_reuse_candidate(role, cc_id):
            continue
        agent_text = " ".join(
            str(x or "")
            for x in (
                getattr(a, "name", ""),
                role,
                getattr(a, "current_task", ""),
                getattr(a, "reuse_domain", ""),
            )
        )
        dmatch = domain_match(query_tokens, tokenize(agent_text))
        status = getattr(a, "status", "")
        status_str = status.value if hasattr(status, "value") else str(status)
        availability = infer_availability(
            status=status_str,
            session_id=getattr(a, "session_id", None),
            ctx_measured_at=getattr(a, "ctx_measured_at", None),
            last_active_at=getattr(a, "last_active_at", None),
            now=now,
            caller_session_id=caller_session_id,
        )
        ctx_pct = getattr(a, "ctx_pct", None)
        ctx_tokens = getattr(a, "ctx_tokens", None)
        action, rationale = recommend_action(
            dmatch=dmatch, availability=availability, ctx_pct=ctx_pct, ctx_tokens=ctx_tokens
        )
        candidates.append(
            {
                "agent_id": getattr(a, "id", None),
                "cc_tool_use_id": cc_id,
                "name": getattr(a, "name", ""),
                "role": role,
                "team_id": getattr(a, "team_id", None),
                "session_id": getattr(a, "session_id", None),
                "reuse_domain": getattr(a, "reuse_domain", None),
                "current_task": getattr(a, "current_task", None),
                "status": status_str,
                "domain_match": dmatch,
                "ctx_tokens": ctx_tokens,
                "ctx_pct": ctx_pct,
                "ctx_window": getattr(a, "ctx_window", None),
                "ctx_measured_at": (
                    m.isoformat() if isinstance(m := getattr(a, "ctx_measured_at", None), datetime) else None
                ),
                "last_active_at": (
                    la.isoformat() if isinstance(la := getattr(a, "last_active_at", None), datetime) else None
                ),
                "availability": availability,
                "recommended_action": action,
                "rationale": rationale,
                "resume_hint": resume_hint(action, availability, cc_id or "", getattr(a, "session_id", None)),
            }
        )

    candidates.sort(
        key=lambda c: (
            -c["domain_match"],
            _AVAIL_RANK.get(c["availability"], 9),
            c["ctx_pct"] if c["ctx_pct"] is not None else 0.0,
        )
    )
    candidates = candidates[: max(1, limit)] if candidates else []

    # Default = the top candidate's action when it is actually reusable; otherwise
    # spawn new. No viable same-domain reachable candidate -> spawn new.
    default = ACTION_NEW
    if candidates:
        top = candidates[0]
        if top["recommended_action"] in (ACTION_REUSE, ACTION_SLIM):
            default = top["recommended_action"]

    return {
        "candidates": candidates,
        "default_recommendation": default,
        "query": query_text,
        "thresholds": {
            "domain_match_min": DOMAIN_MATCH_MIN,
            "ctx_pct_reuse_max": CTX_PCT_REUSE_MAX,
            "ctx_pct_slim_max": CTX_PCT_SLIM_MAX,
            "ctx_tokens_reuse_max": CTX_TOKENS_REUSE_MAX,
            "ctx_tokens_slim_max": CTX_TOKENS_SLIM_MAX,
            "cleanup_window_days": CLEANUP_WINDOW_DAYS,
        },
    }
