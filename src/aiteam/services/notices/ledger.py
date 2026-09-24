"""Notice ledger: register, clear, pick, budget, claim and confirm (design §5.6).

Everything here runs inside API requests; nothing is scheduled. A per-process
memory holds conveniences that are safe to lose on restart: notices a session
start had to defer for budget (offered again at the next prompt), a per-session
prompt counter (for the channel reminder), the keys whose model note already
went out while the budget held their user line back (a restart only repeats
that note once), and the last time a user prompt was seen per project (the
"user is present" hint).

The line budget limits what the user sees. It never silences the model: an
entry the model acts on (``tell_model_when_held``) still reaches it when its
user line is held back.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aiteam.clock import parse_utc, utc_now
from aiteam.services.notices import transcript
from aiteam.services.notices.catalog import (
    CATALOG,
    KIND_RANK,
    SEVERITY_RANK,
    CatalogEntry,
    render_entry,
)
from aiteam.services.notices.detectors import (
    DetectContext,
    DetectorRun,
    Scope,
    run_detectors,
    selected,
)
from aiteam.types import (
    EventType,
    Notice,
    NoticeDelivery,
    NoticeSeverity,
    NoticeStatus,
    PendingRequest,
    PendingResponse,
)

logger = logging.getLogger(__name__)

INFLIGHT = timedelta(seconds=60)
LOST_AFTER = timedelta(minutes=10)
PER_OUTPUT = 2
EVENT_BUDGET = {"SessionStart": 2, "UserPromptSubmit": 3}
SESSION_TOTAL = 5
DEADLINE_S = {"SessionStart": 1.5, "UserPromptSubmit": 0.6}
PRESENCE_WINDOW = timedelta(minutes=15)
CHANNEL_REMINDER_EVERY = 3
MAX_LOCAL_RECORDS = 500
_ACTIVE = (NoticeStatus.ACTIVE.value, NoticeStatus.SNOOZED.value)
# Local lines that only exist while the API is unreachable: an import proves
# the API answers now, so they are history the moment they arrive.
_CLEARED_BY_CONTACT = frozenset({"api_down", "install_in_progress"})
# A successful install supersedes the failure and progress lines before it.
_CLEARS_ON_IMPORT = {
    "install_done": ("install_failed:", "install_in_progress:"),
    "install_upgraded": ("install_failed:", "install_in_progress:"),
}
_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "aiteam:user-notice")
_KEY_RE = re.compile(r"^[a-z_]+(:.*)?$", re.DOTALL)

CHANNEL_REMINDER = {
    "zh": "[信道未读] 仍有 {n} 条点名 {reader} 的消息未清零：用 channel_unread 查看，读完用 channel_read_ack 清零。",
    "en": "[channel unread] {n} mentions of {reader} are still not cleared: check channel_unread, "
          "then clear them with channel_read_ack.",
}


@dataclass
class _Memory:
    carry: dict[tuple[str, str, str], set[str]] = field(default_factory=dict)
    prompts: dict[tuple[str, str, str], int] = field(default_factory=dict)
    told: dict[tuple[str, str, str], set[str]] = field(default_factory=dict)
    last_prompt: dict[tuple[str, str], datetime] = field(default_factory=dict)


_memory = _Memory()


def reset_memory() -> None:
    """Forget per-process conveniences (tests)."""
    global _memory  # noqa: PLW0603
    _memory = _Memory()


def _db_key(repo) -> str:
    return getattr(repo, "_db_url", "") or ""


def note_prompt(repo, project_id: str, now: datetime) -> None:
    _memory.last_prompt[(_db_key(repo), project_id or "")] = now


def user_recently_active(repo, project_id: str, now: datetime | None = None) -> datetime | None:
    """Time of the last user prompt in this project within the presence window."""
    now = now or utc_now()
    seen = _memory.last_prompt.get((_db_key(repo), project_id or ""))
    if seen is not None and now - seen <= PRESENCE_WINDOW:
        return seen
    return None


def _event_label(event: str, source: str) -> str:
    return f"{event}:{source}" if event == "SessionStart" and source else event


def channel_reliable(host: str, event: str, source: str) -> bool:
    """Claude Code shows SessionStart output reliably only for ``startup``."""
    if host != "cc":
        return True
    return not (event == "SessionStart" and source != "startup")


def _entry(catalog_id: str) -> CatalogEntry | None:
    return CATALOG.get(catalog_id)


def validate_notice(
    catalog_id: str, key: str, variant: str, params: Mapping[str, Any] | None, host: str = "",
) -> CatalogEntry:
    """Catalog check every ledger write goes through (detectors, hooks, the API).

    Raises ValueError for an unknown or synthetic entry, a key that does not
    start with the entry id, an unknown variant, an undeclared parameter, a
    host audience the entry is never pushed to, or a missing host on a
    ``per_host`` entry.
    """
    entry = CATALOG.get(catalog_id)
    if entry is None or not entry.ledger:
        raise ValueError(f"unknown catalog id: {catalog_id}")
    if not key.startswith(catalog_id) or not _KEY_RE.match(key) or len(key) > 512:
        raise ValueError("key must start with the catalog id")
    if variant and variant not in entry.variants:
        raise ValueError(f"unknown variant {variant!r} for {catalog_id}")
    if host and (host not in ("cc", "codex") or host not in entry.hosts):
        raise ValueError(f"host {host!r} is not an audience of {catalog_id}")
    if entry.per_host and not host:
        raise ValueError(f"{catalog_id} belongs to one host: give its host")
    unknown = set(params or {}) - set(entry.params)
    if unknown:
        raise ValueError(f"undeclared params for {catalog_id}: {sorted(unknown)}")
    return entry


def _in_scope(notice: Notice, scope: Scope) -> bool:
    if scope.project_id is not None and notice.project_id != scope.project_id:
        return False
    return notice.key in scope.keys or any(notice.key.startswith(prefix) for prefix in scope.prefixes)


async def apply_runs(
    repo, runs: Iterable[DetectorRun], now: datetime, *, source: str = "", host: str = "",
) -> None:
    """Upsert every finding; clear in-scope auto/superseded keys that were not found.

    ``host`` is the host of the request the detectors ran for: a finding of a
    ``per_host`` entry that names no host belongs to it.
    """
    for run in runs:
        if run.findings is None:
            continue  # failed or timed out: keep whatever is there
        found: set[str] = set()
        for finding in run.findings:
            if finding.catalog_id not in run.detector.catalog_ids:
                logger.warning("detector %s returned foreign id %s", run.detector.name, finding.catalog_id)
                continue
            entry = _entry(finding.catalog_id)
            audience = finding.host or (host if entry is not None and entry.per_host else "")
            try:
                validate_notice(finding.catalog_id, finding.key, finding.variant, finding.params, audience)
            except ValueError as exc:
                logger.warning("detector %s returned an invalid finding: %s", run.detector.name, exc)
                continue
            found.add(finding.key)
            await repo.upsert_notice(
                key=finding.key, catalog_id=finding.catalog_id, now=now,
                variant=finding.variant, params=dict(finding.params),
                project_id=finding.project_id, session_id=finding.session_id, host=audience,
                source=source or run.detector.name,
            )
        stale: list[str] = []
        candidates: list[Notice] = []
        for prefix in run.scope.prefixes:
            rows, _ = await repo.list_notices(
                statuses=_ACTIVE, catalog_ids=run.detector.catalog_ids, key_prefix=prefix,
                project_ids=None if run.scope.project_id is None else [run.scope.project_id],
            )
            candidates.extend(rows)
        for key in run.scope.keys:
            row = await repo.get_notice(key)
            if row is not None and row.status.value in _ACTIVE:
                candidates.append(row)
        for row in candidates:
            entry = _entry(row.catalog_id)
            if entry is None or row.key in found or not _in_scope(row, run.scope):
                continue
            if entry.clear in ("auto", "superseded"):
                stale.append(row.key)
        if stale:
            await repo.clear_notices(sorted(set(stale)), now)


async def expire_ttl(repo, now: datetime) -> None:
    """Clear active notices whose entry has ``clear=ttl:<seconds>`` and aged out."""
    for entry in CATALOG.values():
        ttl = entry.ttl_seconds
        if ttl is None:
            continue
        rows, _ = await repo.list_notices(statuses=_ACTIVE, catalog_ids=[entry.id])
        expired = [row.key for row in rows if now - row.last_seen_at > timedelta(seconds=ttl)]
        if expired:
            await repo.clear_notices(expired, now)


def _record_time(record: Mapping[str, Any], now: datetime) -> datetime:
    stamp = parse_utc(str(record.get("at") or "")) if record.get("at") else None
    return stamp if stamp is not None and stamp <= now + timedelta(minutes=5) else now


def _clean_params(entry: CatalogEntry, params: object) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    return {name: params[name] for name in entry.params if name in params and len(str(params[name])) <= 4096}


async def import_local_records(repo, host: str, records: Iterable[Mapping[str, Any]], now: datetime) -> list[str]:
    """Import a hook's local notice file entries; returns delivery ids reported written.

    Idempotent by record uuid: a local line becomes a notice row (upsert by
    key) plus one delivery row with a uuid5-derived id (``INSERT OR IGNORE``);
    a consent record becomes one ``decision.user_config_write`` event with a
    uuid5-derived id.
    """
    emitted: list[str] = []
    for record in list(records)[:MAX_LOCAL_RECORDS]:
        if not isinstance(record, Mapping):
            continue
        record_id = str(record.get("uuid") or "")
        kind = record.get("kind")
        if kind == "emitted":
            ids = record.get("delivery_ids")
            if isinstance(ids, list):
                emitted.extend(str(item) for item in ids if isinstance(item, str) and len(item) <= 64)
            continue
        if not record_id or len(record_id) > 64:
            continue
        if kind == "consent":
            data = {name: value for name, value in record.items() if name not in ("uuid", "kind")}
            data.setdefault("host", host)
            await repo.create_event_once(
                str(uuid.uuid5(_UUID_NAMESPACE, f"consent:{record_id}")),
                EventType.DECISION_USER_CONFIG_WRITE.value,
                f"notice:{host}",
                data,
            )
            continue
        if kind != "local_notice":
            continue
        entry = _entry(str(record.get("catalog_id") or ""))
        key = str(record.get("key") or "")
        if entry is None or not entry.local or not key.startswith(entry.id) or len(key) > 512:
            continue
        if not _KEY_RE.match(key):
            continue
        at = _record_time(record, now)
        session_id = str(record.get("session_id") or "")[:256]
        finished = entry.clear == "once" or entry.id in _CLEARED_BY_CONTACT
        await repo.upsert_notice(
            key=key, catalog_id=entry.id, now=at,
            variant=str(record.get("variant") or "")[:64] if str(record.get("variant") or "") in entry.variants else "",
            params=_clean_params(entry, record.get("params")),
            project_id=str(record.get("project_id") or "")[:64],
            session_id=session_id if entry.render_at & {"immediate"} else "",
            source=str(record.get("source") or "hook")[:100],
            status=NoticeStatus.CLEARED if finished else NoticeStatus.ACTIVE,
        )
        if record.get("displayed") is False:
            continue  # over the per-session cap for immediate lines: registered, not shown
        language = record.get("language") if record.get("language") in ("zh", "en") else "en"
        # Same channel rule as ledger lines: a local line written at a resume,
        # clear or compact start may not have been displayed, so it is not
        # recorded as confirmed (the hook's own prompt fallback covers E01).
        hook_event = str(record.get("event") or "hook")
        base_event, _, start_source = hook_event.partition(":")
        reliable = channel_reliable(host, base_event, start_source or "startup")
        await repo.insert_notice_delivery_if_absent(NoticeDelivery(
            id=str(uuid.uuid5(_UUID_NAMESPACE, f"local:{record_id}")),
            key=key, host=host, session_id=session_id,
            event=("local:" + hook_event)[:64],
            channel_reliable=reliable, language=language,
            claimed_at=at, emitted_at=at, confirmed_at=at if reliable else None,
        ))
        for prefix in _CLEARS_ON_IMPORT.get(entry.id, ()):
            rows, _ = await repo.list_notices(statuses=_ACTIVE, key_prefix=prefix)
            if rows:
                await repo.clear_notices([row.key for row in rows], at)
    return emitted


@dataclass
class _Pick:
    notice: Notice
    entry: CatalogEntry
    existing: NoticeDelivery | None = None
    refire: bool = False

    def rank(self) -> tuple:
        return (
            -SEVERITY_RANK[self.entry.severity],
            KIND_RANK[self.entry.kind],
            self.notice.first_seen_at,
            self.notice.key,
        )


def _eligible_timing(entry: CatalogEntry, event: str, source: str, carried: bool) -> bool:
    if event == "SessionStart":
        if "session_start" not in entry.render_at:
            return False
        return entry.start_sources is None or source in entry.start_sources
    if event == "UserPromptSubmit":
        return "prompt" in entry.render_at or carried
    return False


def _live(delivery: NoticeDelivery, now: datetime) -> bool:
    """Delivered, or claimed recently enough that the claim still counts."""
    return delivery.emitted_at is not None or delivery.claimed_at > now - INFLIGHT


async def _blocked_elsewhere(repo, entry: CatalogEntry, key: str, host: str, now: datetime) -> bool:
    """Once/cooldown gate across sessions (the claim statement re-checks it atomically)."""
    if entry.dedup != "once" and entry.cooldown_hours is None:
        return False
    rows = await repo.list_notice_deliveries(host=host, keys=[key])
    if entry.dedup == "once":
        return any(_live(row, now) for row in rows)
    window = now - timedelta(hours=entry.cooldown_hours or 0)
    return any(
        (row.confirmed_at is not None and row.confirmed_at > window)
        or (row.emitted_at is None and row.claimed_at > now - INFLIGHT)
        for row in rows
    )


def _refire_exempt(pick: _Pick) -> bool:
    """An action-level line that was probably lost is repeated even over budget.

    A key refires at most once per session and only unreliable session starts
    produce refires, so this adds at most a couple of lines to a session.
    """
    return pick.refire and SEVERITY_RANK[pick.entry.severity] >= SEVERITY_RANK[NoticeSeverity.ACTION]


def _budget_left(deliveries: list[NoticeDelivery], event: str) -> int:
    counted = [row for row in deliveries if not row.event.startswith("local:")]
    start = sum(1 for row in counted if row.event.startswith("SessionStart"))
    prompt = sum(1 for row in counted if row.event == "UserPromptSubmit")
    refires = sum(1 for row in counted if row.refired_at is not None)
    total = len(counted) + refires
    used = start if event == "SessionStart" else prompt + refires
    return max(0, min(EVENT_BUDGET.get(event, 0) - used, SESSION_TOTAL - total))


async def _refire_candidates(
    repo, req: PendingRequest, deliveries: list[NoticeDelivery], notices: Mapping[str, Notice], now: datetime,
) -> list[_Pick]:
    """Unreliable deliveries of this session: confirm from the transcript or refire once."""
    if req.host != "cc" or req.event != "UserPromptSubmit":
        return []
    waiting = [
        row for row in deliveries
        if not row.channel_reliable and row.confirmed_at is None and row.refired_at is None
        and row.emitted_at is not None and not row.event.startswith("local:")
    ]
    if not waiting:
        return []
    since = min(row.claimed_at for row in waiting)
    messages = await transcript.displayed_messages(req.transcript_path, since=since)
    picks: list[_Pick] = []
    for row in waiting:
        notice = notices.get(row.key)
        entry = _entry(notice.catalog_id) if notice else None
        if notice is None or entry is None:
            continue
        plain = render_entry(
            entry, variant=notice.variant, language=row.language, host=req.host, params=notice.params,
        ).plain
        if messages is not None and transcript.was_displayed(plain, messages):
            await repo.update_notice_delivery(row.id, confirmed_at=now)
            continue
        if messages is None and SEVERITY_RANK[entry.severity] < SEVERITY_RANK[NoticeSeverity.ACTION]:
            continue  # unknown whether shown: only action-level items are worth a repeat
        picks.append(_Pick(notice, entry, existing=row, refire=True))
    return picks


async def pending(repo, req: PendingRequest, *, now: datetime | None = None, registry=None) -> PendingResponse:
    """POST /api/notices/pending: import, detect, confirm, pick, claim, render."""
    from aiteam.api.language import resolve_language
    from aiteam.services.notices.detectors.registration import dir_scope, resolve_project_id

    now = now or utc_now()
    facts = req.facts
    session_id = req.session_id or ""
    event = req.event
    timing = {"SessionStart": "session_start", "UserPromptSubmit": "prompt"}.get(event)

    project_id, real = await resolve_project_id(repo, req.cwd) if req.cwd else ("", "")
    if req.project_id:
        project_id = req.project_id
    language = (await resolve_language(
        cwd=req.cwd or None, host=req.host, fallback_language=facts.fallback_language or None,
    ))["effective"]

    emitted = list(facts.emitted[:MAX_LOCAL_RECORDS])
    emitted += await import_local_records(repo, req.host, facts.local_records, now)
    if emitted:
        await repo.mark_notice_deliveries_emitted(sorted(set(emitted)), now)
    await repo.mark_notice_deliveries_lost(claimed_before=now - LOST_AFTER, now=now)
    await expire_ttl(repo, now)

    memory_key = (_db_key(repo), req.host, session_id)
    if event == "UserPromptSubmit":
        note_prompt(repo, project_id, now)
        _memory.prompts[memory_key] = _memory.prompts.get(memory_key, 0) + 1

    if timing is not None:
        ctx = DetectContext(
            host=req.host, event=event, source=req.source, session_id=session_id, cwd=req.cwd,
            project_id=project_id, facts={"real_dir": real}, now=now, repo=repo, reader=req.reader,
        )
        runs = await run_detectors(
            ctx, selected(ctx, timing, fresh=False, registry=registry), budget_s=DEADLINE_S.get(event),
        )
        await apply_runs(repo, runs, now, host=req.host)

    response = PendingResponse(language=language)
    if timing is None or not session_id:
        return response

    scopes = {"", project_id}
    if real:
        scopes.add(dir_scope(real))
    active, _ = await repo.list_notices(statuses=_ACTIVE)
    notices = {row.key: row for row in active}
    deliveries = await repo.list_notice_deliveries(host=req.host, session_id=session_id)
    by_key = {row.key: row for row in deliveries}
    carried = _memory.carry.get(memory_key, set())

    picks = await _refire_candidates(repo, req, deliveries, notices, now)
    for notice in active:
        entry = _entry(notice.catalog_id)
        if entry is None or not entry.ledger or entry.render_at & {"local", "immediate"}:
            continue
        if req.host not in entry.hosts or notice.project_id not in scopes:
            continue
        if notice.host and notice.host != req.host:
            continue  # host-bound (another host's release command or reader)
        if notice.session_id and notice.session_id != session_id:
            continue
        if notice.status == NoticeStatus.SNOOZED and (notice.snoozed_until is None or notice.snoozed_until > now):
            continue
        if not _eligible_timing(entry, event, req.source, notice.key in carried):
            continue
        existing = by_key.get(notice.key)
        if existing is not None and _live(existing, now):
            continue
        if await _blocked_elsewhere(repo, entry, notice.key, req.host, now):
            continue
        picks.append(_Pick(notice, entry, existing=existing))
    # Lost action-level lines first: they may use the budget, or go past it.
    picks.sort(key=lambda pick: (not _refire_exempt(pick), *pick.rank()))

    reliable = channel_reliable(req.host, event, req.source)
    label = _event_label(event, req.source)
    budget = _budget_left(deliveries, event)
    chosen: list[tuple[_Pick, str]] = []
    attempted: set[str] = set()
    for pick in picks:
        if len(chosen) >= PER_OUTPUT:
            break
        if len(chosen) >= budget and not _refire_exempt(pick):
            continue
        attempted.add(pick.notice.key)
        entry = pick.entry
        if pick.refire:
            won = await repo.update_notice_delivery(pick.existing.id, refired_at=now)
            delivery_id = pick.existing.id
        elif pick.existing is not None:
            won = await repo.reclaim_notice_delivery(
                pick.existing.id, now=now, stale_before=now - INFLIGHT,
                event=label, channel_reliable=reliable, language=language,
            )
            delivery_id = pick.existing.id
        else:
            delivery = NoticeDelivery(
                key=pick.notice.key, host=req.host, session_id=session_id, event=label,
                channel_reliable=reliable, language=language, claimed_at=now,
            )
            window = entry.cooldown_hours
            won = await repo.claim_notice_delivery(
                delivery, inflight_after=now - INFLIGHT,
                cooldown_after=now - timedelta(hours=window) if window is not None else None,
                once=entry.dedup == "once",
            )
            delivery_id = delivery.id
        if won:
            chosen.append((pick, delivery_id))

    chosen_keys = {pick.notice.key for pick, _ in chosen}
    remaining = [pick for pick in picks if pick.notice.key not in chosen_keys]
    if event == "SessionStart":
        _memory.carry[memory_key] = carried | {pick.notice.key for pick in remaining if not pick.refire}
    elif carried:
        _memory.carry[memory_key] = carried - chosen_keys

    user_lines: list[str] = []
    model_notes: list[str] = []
    for pick, delivery_id in chosen:
        rendered = render_entry(
            pick.entry, variant=pick.notice.variant, language=language, host=req.host,
            params=pick.notice.params, entrypoint=facts.entrypoint, reliable=reliable,
        )
        user_lines.append(rendered.line)
        model_notes.append(rendered.model)
        response.delivery_ids.append(delivery_id)
    if user_lines and remaining:
        summary = render_entry(
            CATALOG["more_pending"], language=language, host=req.host, params={"n": len(remaining)},
            entrypoint=facts.entrypoint, reliable=reliable,
        )
        user_lines.append(summary.line)
        model_notes.append(summary.model)

    # Held back by the budget, but the model is the one to act: tell it once.
    told = _memory.told.get(memory_key, set())
    held_now: set[str] = set()
    for pick in remaining:
        key = pick.notice.key
        if pick.refire or not pick.entry.tell_model_when_held or key in attempted or key in told:
            continue
        held = render_entry(
            pick.entry, variant=pick.notice.variant, language=language, host=req.host,
            params=pick.notice.params, entrypoint=facts.entrypoint, held=True,
        )
        model_notes.append(held.model)
        held_now.add(key)
    if held_now:
        told = _memory.told[memory_key] = told | held_now

    if event == "UserPromptSubmit" and req.reader and project_id:
        reminder = _channel_reminder(
            req, project_id, active, deliveries, chosen_keys | held_now, told, language, memory_key,
        )
        if reminder:
            model_notes.append(reminder)

    response.user_text = "\n".join(user_lines)
    response.model_text = "\n\n".join(model_notes)
    return response


def _channel_reminder(
    req: PendingRequest, project_id: str, active: list[Notice], deliveries: list[NoticeDelivery],
    sent_keys: set[str], told: set[str], language: str, memory_key: tuple[str, str, str],
) -> str:
    """Every third prompt, one short model-only line while mentions stay uncleared.

    Only after the model got the full note for a mention in this session (with
    its user line, or held back by the budget); ``sent_keys`` went out in this
    very output, so no reminder is added to them.
    """
    prefix = f"channel_mention:{req.reader}:{project_id}:"
    open_keys = {row.key for row in active if row.key.startswith(prefix)}
    if not open_keys or open_keys & sent_keys:
        return ""
    delivered = any(row.key.startswith(prefix) for row in deliveries) or any(
        key.startswith(prefix) for key in told)
    if not delivered or _memory.prompts.get(memory_key, 0) % CHANNEL_REMINDER_EVERY:
        return ""
    notice = next(row for row in active if row.key in open_keys)
    from aiteam.services.notices.render import clean_text

    return CHANNEL_REMINDER[language].format(n=clean_text(notice.params.get("n", "?")), reader=clean_text(req.reader))


async def register(repo, *, key: str, catalog_id: str, variant: str = "", params: Mapping[str, Any] | None = None,
                   project_id: str = "", session_id: str = "", host: str = "", source: str = "",
                   now: datetime | None = None) -> Notice:
    """POST /api/notices: register or refresh one notice (catalog-validated)."""
    validate_notice(catalog_id, key, variant, params, host)
    return await repo.upsert_notice(
        key=key, catalog_id=catalog_id, now=now or utc_now(), variant=variant, params=dict(params or {}),
        project_id=project_id, session_id=session_id, host=host, source=source,
    )


async def refresh(repo, *, host: str = "cc", now: datetime | None = None, registry=None) -> None:
    """``fresh=1``: run every detector that can run without a session."""
    now = now or utc_now()
    ctx = DetectContext(
        host=host, event="", source="", session_id="", cwd="", project_id="",
        facts={}, now=now, repo=repo,
    )
    runs = await run_detectors(ctx, selected(ctx, None, fresh=True, registry=registry), budget_s=3.0)
    await apply_runs(repo, runs, now, host=host)
    await expire_ttl(repo, now)
