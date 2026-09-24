"""Notice detectors: API-side checks that find the notices OS should show.

Contract (docs/user-notice-design.md §5.4):

1. A detector returns every hit for its ``catalog_ids`` within its scope. The
   ledger upserts the returned keys; active keys in the same scope that were
   not returned are cleared (for ``clear`` = auto / superseded entries).
2. A detector that times out or raises produces nothing and clears nothing:
   no data is not the same as no problem.
3. All file and subprocess IO goes through worker threads. Detectors run only
   when a request asks for them; there are no timers.
4. ``timing``: ``session_start`` runs on SessionStart fetches, ``prompt`` on
   UserPromptSubmit fetches (cheap or cached checks only), ``demand`` only on
   ``fresh=1``. Every detector runs on ``fresh=1``.
5. ``REGISTRY`` is the single list; the Codex batch appends its two detectors.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from aiteam.storage.repository import StorageRepository

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 0.3


@dataclass(frozen=True)
class DetectContext:
    """What a detector may look at for one request."""

    host: str
    event: str
    source: str
    session_id: str
    cwd: str
    project_id: str
    facts: Mapping[str, object]
    now: datetime
    repo: StorageRepository
    reader: str = ""


@dataclass(frozen=True)
class Finding:
    """One detector hit: a notice key with its render parameters.

    ``host`` limits the audience to one host's sessions. For a ``per_host``
    catalog entry the ledger fills in the host of the request when it is left
    empty, so such entries need keys that differ per host (the host itself, or
    that host's reader). For other entries empty means every host in the
    entry's ``hosts``.
    """

    catalog_id: str
    key: str
    params: Mapping[str, object] = field(default_factory=dict)
    variant: str = ""
    project_id: str = ""
    session_id: str = ""
    host: str = ""


@dataclass(frozen=True)
class Scope:
    """Keys a detector is authoritative for in one run.

    A key is in scope when it equals one of ``keys`` or starts with one of
    ``prefixes``, and, when ``project_id`` is not None, its row carries that
    project id.
    """

    prefixes: tuple[str, ...] = ()
    project_id: str | None = None
    keys: tuple[str, ...] = ()


class NoDataError(Exception):
    """Raised by a detector that could not look (offline, no cache): clear nothing."""


class Detector(Protocol):
    """Interface every detector implements."""

    name: str
    catalog_ids: tuple[str, ...]
    timing: frozenset[str]
    hosts: frozenset[str]
    timeout_s: float

    def applies(self, ctx: DetectContext) -> bool:
        """Extra per-request precondition (host and timing are checked by the runner)."""
        ...

    def scope(self, ctx: DetectContext) -> Scope:
        """Keys this run is authoritative for."""
        ...

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        """All hits in scope."""
        ...


@dataclass(frozen=True)
class DetectorRun:
    """Outcome of one detector: findings, or None when it failed or timed out."""

    detector: Detector
    scope: Scope
    findings: list[Finding] | None


def _registry() -> tuple[Detector, ...]:
    from aiteam.services.notices.detectors.api_version import ApiVersionDetector
    from aiteam.services.notices.detectors.channels import ChannelMentionDetector
    from aiteam.services.notices.detectors.codex_copies import CodexCopiesDetector
    from aiteam.services.notices.detectors.codex_trust import CodexTrustDetector
    from aiteam.services.notices.detectors.decisions import DecisionsDetector
    from aiteam.services.notices.detectors.host_versions import HostVersionsDetector
    from aiteam.services.notices.detectors.installed_copies import InstalledCopiesDetector
    from aiteam.services.notices.detectors.registration import RegistrationDetector
    from aiteam.services.notices.detectors.release import ReleaseDetector

    return (
        RegistrationDetector(),
        DecisionsDetector(),
        ReleaseDetector(),
        ChannelMentionDetector(),
        ApiVersionDetector(),
        CodexCopiesDetector(),
        CodexTrustDetector(),
        InstalledCopiesDetector(),
        HostVersionsDetector(),
    )


REGISTRY: tuple[Detector, ...] = _registry()


def selected(ctx: DetectContext, timing: str | None, *, fresh: bool,
             registry: Sequence[Detector] | None = None) -> list[Detector]:
    """Detectors to run for this request."""
    chosen = []
    for detector in registry if registry is not None else REGISTRY:
        if ctx.host not in detector.hosts:
            continue
        if not fresh and (timing is None or timing not in detector.timing):
            continue
        if not detector.applies(ctx):
            continue
        chosen.append(detector)
    return chosen


async def run_detectors(
    ctx: DetectContext,
    detectors: Sequence[Detector],
    *,
    budget_s: float | None = None,
) -> list[DetectorRun]:
    """Run detectors concurrently, each within its own timeout and the total budget."""

    async def one(detector: Detector) -> DetectorRun:
        scope = detector.scope(ctx)
        timeout = detector.timeout_s if budget_s is None else min(detector.timeout_s, budget_s)
        try:
            findings = await asyncio.wait_for(detector.detect(ctx), timeout=timeout)
        except TimeoutError:
            logger.info("notice detector %s timed out after %.2fs", detector.name, timeout)
            return DetectorRun(detector, scope, None)
        except NoDataError:
            return DetectorRun(detector, scope, None)
        except Exception:  # noqa: BLE001 - one detector never breaks the fetch
            logger.warning("notice detector %s failed", detector.name, exc_info=True)
            return DetectorRun(detector, scope, None)
        return DetectorRun(detector, scope, list(findings))

    if not detectors:
        return []
    return list(await asyncio.gather(*(one(detector) for detector in detectors)))
