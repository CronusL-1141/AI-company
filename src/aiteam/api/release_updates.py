"""On-demand public Release checks. No Git, installation or background workers."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import anyio
import httpx

from aiteam.types import ReleaseUpdateStatus

RELEASE_API = "https://api.github.com/repos/CronusL-1141/AI-company/releases/latest"
RELEASE_BASE = "https://github.com/CronusL-1141/AI-company/releases/tag/"
SUCCESS_TTL = 6 * 3600
FAILURE_TTL = 15 * 60
_VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")


def _version(value: object) -> tuple[int, ...] | None:
    match = _VERSION.fullmatch(value) if isinstance(value, str) and len(value) < 64 else None
    return tuple(map(int, match.groups())) if match else None


def notice_language(language: str | None = None, accept_language: str | None = None) -> str:
    """Resolve explicit locale first, then weighted supported HTTP languages."""
    if language:
        return "zh" if language.lower().replace("_", "-").split("-")[0] == "zh" else "en"
    candidates = []
    for item in (accept_language or "").split(","):
        parts = item.strip().split(";")
        tag = parts[0].lower().replace("_", "-").split("-")[0]
        quality = 1.0
        try:
            for parameter in parts[1:]:
                if parameter.strip().startswith("q="):
                    quality = float(parameter.strip()[2:])
        except ValueError:
            continue
        if tag in {"zh", "en"} and 0 < quality <= 1:
            candidates.append((quality, tag))
    return max(candidates, key=lambda item: item[0])[1] if candidates else "en"


class ReleaseChecker:
    """Share one check across callers and persist metadata across API restarts."""

    def __init__(self, cache: Path, *, transport: httpx.AsyncBaseTransport | None = None):
        self.cache = cache
        self.transport = transport
        self.lock = asyncio.Lock()
        self.state: dict | None = None

    async def _load(self) -> dict:
        try:
            path = anyio.Path(self.cache)
            if (await path.stat()).st_size > 8192:
                return {}
            state = json.loads(await path.read_text())
            if not isinstance(state, dict) or state.get("source") != RELEASE_API:
                return {}
            for key in ("attempted_at", "checked_at"):
                value = state.get(key, 0)
                if not isinstance(value, (int, float)) or not 0 <= value <= time.time() + 60:
                    return {}
            return state if state.get("version") is None or _version(state["version"]) else {}
        except (OSError, ValueError):
            return {}

    async def _save(self) -> None:
        path = anyio.Path(self.cache)
        temporary = anyio.Path(self.cache.with_suffix(f".{os.getpid()}.tmp"))
        try:
            await path.parent.mkdir(parents=True, exist_ok=True)
            await temporary.write_text(json.dumps(self.state))
            await temporary.replace(path)
        except OSError:
            pass  # An unwritable cache must not prevent a session from starting.

    async def _fetch(self) -> dict:
        # Bound the whole request (not just time between response chunks).
        with anyio.fail_after(1.0):
            async with httpx.AsyncClient(transport=self.transport, timeout=0.8) as client:
                async with client.stream("GET", RELEASE_API, headers={
                    "Accept": "application/vnd.github+json", "User-Agent": "AI-Team-OS-release-check",
                }) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 262144:
                            raise ValueError("release response too large")
        release = json.loads(raw)
        if (not isinstance(release, dict) or release.get("draft") is not False
                or release.get("prerelease") is not False or not _version(release.get("tag_name"))):
            raise ValueError("not a stable release")
        return release

    async def check(
        self, current_version: str, language: str = "en", installation: str = "cc-source",
    ) -> ReleaseUpdateStatus:
        async with self.lock:
            if self.state is None:
                self.state = await self._load()
            now = time.time()
            ttl = FAILURE_TTL if self.state.get("failed") else SUCCESS_TTL
            age = now - self.state.get("attempted_at", 0)
            if not self.state or age < 0 or age >= ttl:
                try:
                    release = await self._fetch()
                    self.state = {"source": RELEASE_API, "version": release["tag_name"].lstrip("v"),
                                  "tag": release["tag_name"], "checked_at": now}
                except (httpx.HTTPError, OSError, ValueError, TimeoutError):
                    self.state["failed"] = True
                self.state.update(source=RELEASE_API, attempted_at=now)
                await self._save()
            state = dict(self.state)

        latest = state.get("version")
        result = ReleaseUpdateStatus(current_version=current_version, latest_version=latest,
                                     checked_at=state.get("checked_at"), stale=bool(state.get("failed")),
                                     language=notice_language(language))
        current, remote = _version(current_version), _version(latest)
        if current is None or remote is None:
            return result
        # Construct the URL locally; never inject release body/name/remote links into a prompt.
        tag = state.get("tag", "v" + latest)
        if _version(tag) != remote:
            tag = "v" + latest
        result.release_url = RELEASE_BASE + tag
        result.status = "update_available" if remote > current else "up_to_date" if remote == current else "ahead"
        if state.get("failed") and result.status == "up_to_date":
            result.status = "unknown"
        if result.status == "update_available":
            _render_notice(result, installation)
        return result


def _render_notice(result: ReleaseUpdateStatus, installation: str) -> None:
    """Keep the user line short; put prerequisites, URL and ownership in model context."""
    chinese = result.language == "zh"
    prefix = f"[AI Team OS] v{result.latest_version}: "
    if installation == "cc-plugin":
        action = "claude plugin update ai-team-os"
        ending = "；重启。" if chinese else "; restart."
        steps = (
            "执行 claude plugin update ai-team-os，完成后重启 Claude Code。"
            if chinese else "Run claude plugin update ai-team-os, then restart Claude Code."
        )
    elif installation == "codex":
        action = "python3 scripts/codex_adapter.py update"
        ending = "（先更新源码和依赖）" if chinese else " (after source/deps update)"
        steps = (
            "在原安装源码目录确认工作区干净，再 git pull --ff-only，使用系统解释器执行 "
            "python3 -m pip install -e .，然后 python3 scripts/codex_adapter.py update。"
            "原 stdio/仅 Hook 安装使用 update --hooks-only，保持原 MCP 传输配置。"
            "执行 python3 scripts/codex_adapter.py status 核对副本与运行版本；按现有授权与所有权"
            "协调 API 重启，再重新连接 Codex，不停止其他会话使用的共享服务。"
            if chinese else
            "In the original installation checkout, verify a clean working tree, run git pull --ff-only, "
            "then python3 -m pip install -e . with the system interpreter, followed by "
            "python3 scripts/codex_adapter.py update. For an existing stdio/hooks-only installation, "
            "use update --hooks-only to preserve MCP transport. Run python3 scripts/codex_adapter.py status; "
            "coordinate any API restart with its owner and current authorization, then reconnect Codex. "
            "Do not stop shared services used by other sessions."
        )
    else:
        action = "git pull --ff-only && python3 -m pip install -e ."
        ending = "；重启。" if chinese else "; restart."
        steps = (
            "在原安装源码目录确认工作区干净，再执行 git pull --ff-only && python3 -m pip install -e .，"
            "使用系统解释器；按既有源码安装流程同步已安装 Hook，协调 API 所有者重启并重启 Claude Code。"
            if chinese else
            "In the original installation checkout, verify a clean working tree, then run "
            "git pull --ff-only && python3 -m pip install -e . using the system interpreter. "
            "Refresh installed hooks through the existing source installation workflow; "
            "coordinate an API restart with its owner and restart Claude Code."
        )
    result.notice = prefix + action + ending
    if result.stale:
        result.notice += "（缓存）" if chinese else " [cached]"
    if chinese:
        result.additional_context = (
            f"用户已看到更新提醒：{result.notice}\n"
            f"当前运行 v{result.current_version}，正式新版 v{result.latest_version}。发布页：{result.release_url}\n"
            + ("上次检查结果；当前暂无法联网核验。\n" if result.stale else "")
            + "仅提醒，不自动更新；用户明确要求更新后再按安装方式操作。" + steps
            + "磁盘文件更新不代表运行服务已更新，完成后核对运行版本。"
        )
    else:
        result.additional_context = (
            f"The user has seen this update notice: {result.notice}\n"
            f"Running v{result.current_version}; stable release v{result.latest_version}. "
            f"Release: {result.release_url}\n"
            + ("Cached check; online verification is currently unavailable.\n" if result.stale else "")
            + "Notify only; do not update automatically. Act only after the user requests an update. " + steps
            + " Updated files do not prove the running service is updated; verify the running version afterward."
        )


release_checker = ReleaseChecker(
    Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ai-team-os" / "release-check.json"
)
