"""Prediction input projected from the usage journal by recording position.

The independent recorder (``codex_usage_recorder``) saves native usage facts
with persistent per-file cursors. A prediction round consumes only the facts
that became due since the previous round: recorded after the previous position
and not occurring after this observation, or recorded earlier but occurring
inside this interval. A fact recorded late is therefore still consumed, exactly
once, and nothing is re-read from the original logs: the former whole-file
rescan of every recently written log exceeded its byte budget once one session
log grew past a few hundred megabytes, and every round then priced nothing
(task a0110751).

The counting rules are the former log reader's, applied to facts:

* a response (``token_usage_record``) counts once per response ID, across
  files, generations, archive copies and fork copies; a copy recorded under
  another owner thread is inherited history and never counts;
* a child response without an owner thread needs its parent's ledger history,
  otherwise it is unidentified;
* only the official provider counts; a configured ChatGPT alias needs provider
  evidence newer than its mapping;
* ``token_count`` events of files without responses count as tokens only, with
  the parent-replay rule for forks; they never carry a price;
* a launch request tier fills only the absent native tier of the bound first
  turn of one new exec.

Every fact that could be usage but cannot be priced marks the interval
incomplete. Nothing here decides an account: callers bind the result.
"""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from aiteam.services.codex_local_usage import (
    _FIELDS,
    CodexLocalUsageError,
    _Budget,
    _increments,
    _Legacy,
    _provider_aliases,
    _Session,
    _Usage,
    _usage,
    _verify_provider_config,
    in_codex_home,
)
from aiteam.services.codex_usage_journal import source_namespace
from aiteam.storage.codex_usage_journal import CodexUsageJournalRepository, JournalRow
from aiteam.types import (
    CodexLaunchTierEvidence,
    CodexUsageObservation,
    CodexUsageTokens,
    PricingRequestLine,
    PricingUsageEntry,
)

# Facts consumed by one round at most. A backlog (the API was down, the recorder
# caught up) drains over later rounds; one interval never exceeds the 10,000
# entries a stored price sample may hold.
MAX_FACTS_PER_ROUND = 5000
_MAX_LEGACY_ANCESTORS = 32

LaunchEvidenceLoader = Callable[[datetime, datetime], Awaitable[list[CodexLaunchTierEvidence]]]


@dataclass(frozen=True)
class JournalPosition:
    """Where a binding's consumption stands: facts recorded through ``seq`` and due by ``observed_at``."""

    seq: int
    observed_at: datetime


@dataclass(frozen=True)
class JournalView:
    """One round's consumption: tokens, request prices to quote and diagnostics."""

    tokens: int
    entries: list[PricingUsageEntry]
    counts: dict[str, int]
    position: JournalPosition


@dataclass(frozen=True)
class _Fact:
    seq: int
    source_id: str
    value: CodexUsageObservation


@dataclass(frozen=True)
class _Aliases:
    names: frozenset[str] = frozenset()
    configured_at: datetime | None = None


@dataclass
class _Lookups:
    """Journal state the rules consult, all bounded by the round's recording position."""

    earlier: dict[str, list[CodexUsageObservation]] = field(default_factory=dict)
    ledger_sessions: set[str] = field(default_factory=set)
    ledger_sources: set[str] = field(default_factory=set)
    legacy: dict[str, list[_Fact]] = field(default_factory=dict)
    launches: list[CodexLaunchTierEvidence] = field(default_factory=list)


class _Tally:
    """Counts plus the one bit that matters to pricing: is anything unpriceable?"""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def gap(self, key: str, count: int = 1) -> None:
        if count:
            self.counts[key] += count
            self.counts["pricing_incomplete"] = 1


async def journal_position(journal: CodexUsageJournalRepository, observed_at: datetime) -> JournalPosition:
    """The position a new zero baseline starts from: everything recorded so far."""
    return JournalPosition(await journal.recorded_through(), observed_at)


async def project_journal(
    journal: CodexUsageJournalRepository, codex_home: Path, *,
    binding: datetime, previous: JournalPosition, until: datetime,
    provider_mapping_evidence: tuple[str, datetime] | None = None,
    launch_evidence: LaunchEvidenceLoader | None = None,
    max_facts: int = MAX_FACTS_PER_ROUND,
) -> JournalView:
    """Consume the facts due in ``(previous.observed_at, until]`` for a binding.

    Data problems (an invalid window, a changed provider mapping) raise
    ``CodexLocalUsageError``; an unreadable journal raises
    ``CodexUsageJournalUnavailableError``.
    """
    _validate_window(binding, previous, until, provider_mapping_evidence)
    aliases, signature, alias_counts = await asyncio.to_thread(
        _read_aliases, codex_home, previous.observed_at, provider_mapping_evidence,
    )
    recorded = await journal.recorded_through()
    if recorded < previous.seq:
        raise CodexLocalUsageError("rollback")  # the journal lost facts this binding consumed
    through = min(recorded, previous.seq + max(1, max_facts))
    rows = await journal.consumable(
        after=previous.seq, through=through, since=previous.observed_at, until=until, binding=binding,
    )
    facts, unreadable = await asyncio.to_thread(_parse, rows)
    lookups = await _lookups(journal, facts, previous, binding, through)
    candidates = [fact for fact in facts if _launch_candidate(fact.value)]
    if candidates and launch_evidence is not None:
        since = min([previous.observed_at, *(fact.value.occurred_at for fact in candidates)])
        lookups.launches = await launch_evidence(since, until)
    tokens, entries, tally = await asyncio.to_thread(
        _project, facts, lookups, aliases, source_namespace(codex_home),
    )
    await asyncio.to_thread(_verify_aliases, codex_home, signature)
    tally.counts.update(alias_counts)
    tally.gap("pricing_unreadable_records", unreadable)
    tally.counts["journal_facts_consumed"] = len(facts)
    if through < recorded:
        tally.counts["journal_backlog"] = recorded - through
    tally.counts["pricing_entries"] = len(entries)
    tally.counts.setdefault("pricing_incomplete", 0)
    return JournalView(tokens, entries, dict(tally.counts), JournalPosition(through, until))


def _validate_window(
    binding: datetime, previous: JournalPosition, until: datetime,
    evidence: tuple[str, datetime] | None,
) -> None:
    moments = (binding, previous.observed_at, until)
    if any(not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
           for value in moments) or not binding <= previous.observed_at < until:
        raise CodexLocalUsageError("invalid_window")
    if type(previous.seq) is not int or previous.seq < 0:
        raise CodexLocalUsageError("invalid_window")
    if evidence is not None and (
        not isinstance(evidence, tuple) or len(evidence) != 2 or not isinstance(evidence[0], str)
        or not isinstance(evidence[1], datetime) or evidence[1].tzinfo is None
        or evidence[1].utcoffset() is None or evidence[1] > previous.observed_at
    ):
        raise CodexLocalUsageError("unreadable")


# ---------------------------------------------------------------- provider mapping


def _read_aliases(
    codex_home: Path, start: datetime, evidence: tuple[str, datetime] | None,
) -> tuple[_Aliases, tuple[int, ...] | None, dict[str, int]]:
    """Accepted official aliases, effective only if configured before the interval."""
    names, configured_at, signature, digest = in_codex_home(codex_home, _provider_aliases)
    if evidence is not None:
        expected, configured_at = evidence
        if expected != digest:
            raise CodexLocalUsageError("unreadable")
    if names and configured_at is not None and configured_at <= start:
        return _Aliases(names, configured_at), signature, {"official_provider_aliases": len(names)}
    return _Aliases(), signature, {"alias_configuration_newer_than_window": 1} if names else {}


def _verify_aliases(codex_home: Path, signature: tuple[int, ...] | None) -> None:
    """The mapping read before the projection must still be the one on disk."""
    in_codex_home(codex_home, lambda descriptor: _verify_provider_config(descriptor, signature))


def _provider(provider: str | None, evidence_at: datetime | None, occurred_at: datetime | None,
              aliases: _Aliases, tally: _Tally) -> str | None:
    """The official provider for a fact, or the provider it actually names.

    A historical provider ID may once have meant another endpoint: an alias
    counts only with provider evidence after the mapping became effective, and
    a child's later metadata cannot reclassify history recorded before it.
    """
    if provider not in aliases.names:
        return provider
    if (
        evidence_at is None or aliases.configured_at is None or occurred_at is None
        or evidence_at < aliases.configured_at or occurred_at < evidence_at
    ):
        tally.counts["stale_alias_evidence_records_skipped"] += 1
        return provider
    tally.counts["official_alias_records_recognized"] += 1
    return "openai"


def _eligible(provider: str | None, tally: _Tally) -> bool:
    if provider == "openai":
        return True
    tally.counts["unknown_provider_events_skipped" if provider is None else "other_provider_events_skipped"] += 1
    return False


# ---------------------------------------------------------------- journal reads


def _parse(rows: Iterable[JournalRow]) -> tuple[list[_Fact], int]:
    facts, unreadable = [], 0
    for seq, source_id, payload in rows:
        try:
            value = CodexUsageObservation.model_validate_json(payload)
        except ValidationError:
            unreadable += 1
            continue
        facts.append(_Fact(int(seq), source_id, value))
    return facts, unreadable


async def _read(rows: Awaitable[list[JournalRow]]) -> list[_Fact]:
    """Decode looked-up facts off the event loop; an unreadable stored row is simply absent."""
    return (await asyncio.to_thread(_parse, await rows))[0]


def _inherited(value: CodexUsageObservation) -> bool:
    return bool(value.thread_id and value.session_id and value.thread_id != value.session_id)


def _child(value: CodexUsageObservation) -> bool:
    return bool(value.parent_thread_id) or value.session_child is True


def _unanchored(value: CodexUsageObservation, ledger_sessions: set[str]) -> bool:
    """A child response without its owner thread cannot be told apart from replayed history."""
    return _child(value) and value.thread_id is None and value.parent_thread_id not in ledger_sessions


def _launch_candidate(value: CodexUsageObservation) -> bool:
    return (value.kind == "ledger" and value.session_source == "exec" and value.service_tier is None
            and value.service_tier_present is False and value.occurred_at is not None)


async def _lookups(
    journal: CodexUsageJournalRepository, facts: list[_Fact], previous: JournalPosition,
    binding: datetime, through: int,
) -> _Lookups:
    """Earlier copies of this round's responses, ledger owners, and legacy session histories."""
    lookups = _Lookups()
    ledger = [fact.value for fact in facts if fact.value.kind == "ledger"]
    legacy = [fact for fact in facts if fact.value.kind == "legacy"]
    # Responses this binding already consumed: recorded by the previous position and due by then.
    for row in await _read(journal.ledger_occurrences(
        {value.response_id for value in ledger if value.response_id}, through=previous.seq,
    )):
        value = row.value
        if value.occurred_at is not None and binding < value.occurred_at <= previous.observed_at:
            lookups.earlier.setdefault(value.response_id, []).append(value)
    parents = {value.parent_thread_id for value in ledger if value.parent_thread_id}
    parents |= {value.parent_thread_id for row in lookups.earlier.values() for value in row
                if value.parent_thread_id}
    lookups.ledger_sources = await journal.ledger_sources({fact.source_id for fact in legacy}, through=through)
    sessions = {fact.value.session_id for fact in legacy
                if fact.value.session_id and fact.source_id not in lookups.ledger_sources}
    lookups.ledger_sessions = await journal.ledger_sessions(parents | sessions, through=through)
    pending = sessions - lookups.ledger_sessions
    for _ in range(_MAX_LEGACY_ANCESTORS):
        if not pending:
            break
        rows = await _read(journal.session_facts(pending, kind="legacy", through=through))
        found: dict[str, list[_Fact]] = {session: [] for session in pending}
        for fact in rows:
            found[fact.value.session_id].append(fact)
        lookups.legacy.update(found)
        # Parents of legacy children: their replayed prefix decides the child's counts.
        pending = {fact.value.parent_thread_id for group in found.values() for fact in group
                   if fact.value.parent_thread_id} - lookups.legacy.keys()
        if pending:
            known = await journal.ledger_sessions(pending, through=through)
            lookups.ledger_sessions |= known
            pending -= known
    return lookups


# ---------------------------------------------------------------- rules


def _project(
    facts: list[_Fact], lookups: _Lookups, aliases: _Aliases, namespace: str,
) -> tuple[int, list[PricingUsageEntry], _Tally]:
    tally = _Tally()
    tokens = 0
    entries: list[PricingUsageEntry] = []
    launches = _launch_bindings(facts, lookups.launches, namespace)
    tokens += _project_ledger(facts, lookups, aliases, launches, tally, entries)
    tokens += _project_legacy(facts, lookups, aliases, tally)
    for fact in facts:
        if fact.value.kind == "diagnostic":
            # An unreadable line may have been usage; nothing proves otherwise.
            tally.gap("pricing_unreadable_records")
    entries.sort(key=lambda entry: (entry.occurred_at, entry.request.request_id))
    return tokens, entries, tally


def _ledger_usage(tokens: CodexUsageTokens | None) -> _Usage | None:
    if tokens is None:
        return None
    try:
        return _usage(tokens.model_dump(exclude_none=True), ledger=True)
    except CodexLocalUsageError:
        return None


def _identity(value: CodexUsageObservation) -> tuple:
    """What two copies of one response must agree on to be the same priced request."""
    return (value.occurred_at, value.usage, value.provider, value.thread_id,
            value.model, value.service_tier, value.service_tier_source, value.turn_id)


def _project_ledger(
    facts: list[_Fact], lookups: _Lookups, aliases: _Aliases,
    launches: dict[str, CodexLaunchTierEvidence], tally: _Tally, entries: list[PricingUsageEntry],
) -> int:
    tokens = 0
    counted: dict[str, CodexUsageObservation] = {}
    for fact in facts:
        value = fact.value
        if value.kind != "ledger":
            continue
        # A copied response keeps its original owner; its replay time and the
        # copying child's provider must not conflict with the owner's record.
        if _inherited(value):
            tally.counts["inherited_response_records_skipped"] += 1
            continue
        provider = _provider(value.provider, value.provider_evidence_at, value.occurred_at, aliases, tally)
        usage = _ledger_usage(value.usage)
        if value.response_id is None or value.occurred_at is None or usage is None:
            tally.counts["invalid_ledger_records"] += 1
            if provider == "openai":
                tally.gap("pricing_unreadable_records")
            continue
        earlier = counted.get(value.response_id) or next((
            item for item in lookups.earlier.get(value.response_id, ())
            if not _inherited(item) and not _unanchored(item, lookups.ledger_sessions)
        ), None)
        if earlier is not None:
            tally.counts["duplicate_response_records"] += 1
            if _identity(earlier) != _identity(value):
                tally.counts["conflicting_response_records"] += 1
                if provider == "openai":
                    tally.gap("pricing_conflicting_responses")
            continue
        if _unanchored(value, lookups.ledger_sessions):
            tally.counts["unanchored_child_response_records_skipped"] += 1
            if provider == "openai":
                tally.gap("pricing_unidentified_events")
            continue
        counted[value.response_id] = value
        if not _eligible(provider, tally):
            continue
        tokens += usage.tokens
        tally.counts["ledger_events_counted"] += 1
        _append_entry(value, usage, launches.get(value.observation_id), entries, tally)
    return tokens


def _append_entry(
    value: CodexUsageObservation, usage: _Usage, launch: CodexLaunchTierEvidence | None,
    entries: list[PricingUsageEntry], tally: _Tally,
) -> None:
    """One request to quote; a missing model or tier is a gap, never a guess."""
    if value.model is None:
        tally.gap("pricing_missing_model")
        return
    tier, tier_source = value.service_tier, value.service_tier_source
    if launch is not None:
        tier, tier_source = launch.requested_service_tier, "launch_requested"
        tally.counts["pricing_service_tier_requested"] += 1
        tally.gap("pricing_missing_service_tier")
    else:
        tally.counts["pricing_service_tier_observed" if tier else "pricing_service_tier_assumed"] += 1
    if tier is None:
        tally.gap("pricing_missing_service_tier")
    try:
        request = PricingRequestLine.model_validate({
            "request_id": value.response_id, "model": value.model, "service_tier": tier or "standard",
            **dict(zip(_FIELDS[:4], usage.values[:4], strict=True)),
        })
        entries.append(PricingUsageEntry(
            occurred_at=value.occurred_at, request=request, model_source=value.model_source,
            service_tier_source=tier_source if tier else "standard_assumption",
            launch_id=launch.launch_id if launch is not None else None,
        ))
    except ValidationError:
        tally.gap("pricing_invalid_requests")


def _launch_bindings(
    facts: list[_Fact], evidence: list[CodexLaunchTierEvidence], namespace: str,
) -> dict[str, CodexLaunchTierEvidence]:
    """Requested tiers for responses of the identified first turn of one new exec.

    Native presence includes an explicit null, and an unknown context never
    proves absence. A launch receipt supplies no tokens, models or observed tiers.
    """
    by_thread: dict[str, dict[str, CodexLaunchTierEvidence]] = {}
    conflicting: set[str] = set()
    for record in evidence:
        if record.source_namespace != namespace:
            continue
        records = by_thread.setdefault(record.thread_id, {})
        previous = records.get(record.launch_id)
        if previous is not None and previous != record:
            conflicting.add(record.thread_id)
        records[record.launch_id] = record
    bound: dict[str, CodexLaunchTierEvidence] = {}
    for fact in facts:
        value = fact.value
        records = by_thread.get(value.session_id or "", {})
        if (len(records) != 1 or value.session_id in conflicting or not _launch_candidate(value)
                or value.session_child is not False or value.parent_thread_id
                or value.session_started_at is None):
            continue
        record = next(iter(records.values()))
        if (
            value.session_started_at < record.started_at
            or value.thread_id != value.session_id or value.turn_id is None
            or value.occurred_at < value.session_started_at or value.occurred_at < record.started_at
            or (record.finished_at is not None and value.occurred_at > record.finished_at)
            or (record.turn_ids and value.turn_id not in record.turn_ids)
            or (not record.turn_ids and value.turn_id != value.first_turn_id)
        ):
            continue
        bound[value.observation_id] = record
    return bound


def _tokens_key(tokens: CodexUsageTokens | None) -> tuple | None:
    return None if tokens is None else tuple(sorted(tokens.model_dump(exclude_none=True).items()))


def _tokens_dict(tokens: CodexUsageTokens | None) -> dict | None:
    return None if tokens is None else tokens.model_dump(exclude_none=True)


@dataclass
class _LegacyStream:
    parent_id: str | None
    child: bool
    started_at: datetime | None
    increments: list


def _project_legacy(facts: list[_Fact], lookups: _Lookups, aliases: _Aliases, tally: _Tally) -> int:
    """Token-only counts for files that never wrote identified responses."""
    due: dict[str, dict[str, str | None]] = {}  # session -> consumed fact -> resolved provider
    for fact in facts:
        value = fact.value
        if value.kind != "legacy":
            continue
        provider = _provider(value.provider, value.provider_evidence_at, value.occurred_at, aliases, tally)
        if fact.source_id in lookups.ledger_sources:
            tally.counts["token_count_events_ignored"] += 1
        elif value.session_id is None:
            tally.counts["unidentified_legacy_records_skipped"] += 1
            if provider == "openai":
                tally.gap("pricing_unidentified_events")
        elif value.session_id in lookups.ledger_sessions:
            tally.counts["legacy_copies_of_ledger_sessions_skipped"] += 1
        else:
            due.setdefault(value.session_id, {})[value.observation_id] = provider
            if value.total_token_usage is None and value.last_token_usage is not None and provider == "openai":
                # Without a cumulative anchor a repeat is indistinguishable from new use.
                tally.gap("pricing_unidentified_events")
    streams = {session: _legacy_stream(rows, aliases, tally) for session, rows in lookups.legacy.items()}
    tokens = 0
    for session, consumed in due.items():
        stream = streams.get(session)
        increments = None if stream is None else _replay_free(session, stream, streams, tally)
        if increments is None:
            tally.counts["ambiguous_legacy_records_skipped"] += len(consumed)
            tally.gap("pricing_unidentified_events", sum(provider == "openai" for provider in consumed.values()))
            continue
        for increment in increments:
            if increment.origin in consumed and _eligible(increment.provider, tally):
                tokens += increment.usage.tokens
                tally.counts["legacy_events_counted"] += 1
                tally.gap("pricing_legacy_events")
    return tokens


def _legacy_stream(rows: list[_Fact], aliases: _Aliases, tally: _Tally) -> _LegacyStream | None:
    """One session's increments over every recorded copy of its events; None if they disagree."""
    unique: dict[tuple, _Fact] = {}
    sources_at: dict[datetime, set[str]] = {}
    for fact in rows:
        value = fact.value
        if value.occurred_at is None:
            return None
        key = (value.occurred_at, _tokens_key(value.total_token_usage), _tokens_key(value.last_token_usage))
        if key not in unique:
            unique[key] = fact
            sources_at.setdefault(value.occurred_at, set()).add(fact.source_id)
    if any(len(sources) > 1 for sources in sources_at.values()):
        return None  # two copies of the session disagree about the same moment
    ordered = sorted(unique.values(), key=lambda fact: (fact.value.occurred_at, fact.seq))
    if not ordered:
        return None
    scratch = _Tally()  # history outside this round is not part of its diagnostics
    session = _Session(session_id=ordered[0].value.session_id, legacy=[
        _Legacy(value.occurred_at, _tokens_dict(value.total_token_usage), _tokens_dict(value.last_token_usage),
                _provider(value.provider, value.provider_evidence_at, value.occurred_at, aliases, scratch),
                origin=value.observation_id)
        for value in (fact.value for fact in ordered)
    ])
    budget = _Budget(threading.Event())
    try:
        increments = _increments(session, budget)
    except CodexLocalUsageError:
        return None
    for key in ("duplicate_cumulative_snapshots", "unanchored_legacy_events_skipped"):
        tally.counts[key] += budget.counts[key]
    first = ordered[0].value
    return _LegacyStream(first.parent_thread_id, _child(first), first.session_started_at, increments)


def _replay_free(session: str, stream: _LegacyStream, streams: dict, tally: _Tally) -> list | None:
    """A legacy child's own increments after its exact replayed parent prefix.

    Only a complete, nonempty parent prefix establishes the replay boundary;
    missing parents, partial matches and cycles leave the child ambiguous.
    """
    if not stream.child:
        return stream.increments
    parent = streams.get(stream.parent_id or "")
    if parent is None or stream.started_at is None:
        return None
    prefix = [event for event in parent.increments if event.timestamp <= stream.started_at]
    ancestors, ancestor = {session}, stream.parent_id
    while streams.get(ancestor) is not None and ancestor not in ancestors:
        ancestors.add(ancestor)
        ancestor = streams[ancestor].parent_id
    if ancestor in ancestors or not prefix or len(stream.increments) < len(prefix) or any(
        child.usage != original.usage or child.provider != original.provider
        for child, original in zip(stream.increments, prefix)
    ):
        return None
    tally.counts["replayed_legacy_events_skipped"] += len(prefix)
    return stream.increments[len(prefix):]


__all__ = [
    "MAX_FACTS_PER_ROUND", "JournalPosition", "JournalView", "LaunchEvidenceLoader",
    "journal_position", "project_journal",
]
