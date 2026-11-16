"""A feedback draft's life: checked and frozen, confirmed by the user, published, reconciled.

draft: the text goes through aiteam.feedback.redact, is rendered in the
repository's form (aiteam.feedback.forms) and stored as a draft with everything a
later step needs (the GitHub ids found by the same query that looked for similar
posts). The answer is the exact text that would be published. A text already
published from here (or being published) is not drafted again: the answer is that
post.

submit: only with the confirmation the API accepts (the MCP client's dialog, see
FeedbackSubmitRequest), only for a draft of this node that has not expired, and
only for the content hash the user was shown. The draft is claimed (rate limit and
the one-row-per-text index, aiteam.storage.feedback), the repository is searched
for the text's marker (a copy filed from another machine), then gh publishes it.
A gh call that times out may have reached GitHub: the row says unknown and nothing
is retried.

Reading a row settles it: a draft past 30 minutes expires; an unknown row (or one
left sending by a stopped API) is looked for on GitHub by its marker through the
list APIs, becomes sent when found and failed when still missing 10 minutes after
it was sent. No timer does any of this.

handoff: a prefilled GitHub page (or, when too long for a URL, the text in a local
file) for the user to submit there. Their click on GitHub is the confirmation.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import platform
import re
import socket
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiteam
from aiteam.clock import utc_now
from aiteam.feedback import forms, redact
from aiteam.feedback.auth import REVOKE_PAGE, AppAuth, DeviceStart, PollResult
from aiteam.feedback.github import GhCli, GitHub, GitHubError, Lookup, Posted, Timeouts, configured_repo, locate
from aiteam.ids import new_id
from aiteam.paths import DataDirMissing, cc_config_dir, codex_home, os_data_dir, own_cc_config_dir
from aiteam.services.notices import install_kind
from aiteam.storage.feedback import Duplicate, FeedbackRepository, NotClaimable, RateLimited
from aiteam.storage.partition import is_own
from aiteam.types import (
    EventType,
    FeedbackConfirmMethod,
    FeedbackDraftRequest,
    FeedbackKind,
    FeedbackMode,
    FeedbackRoute,
    FeedbackStatus,
    FeedbackSubmission,
    FeedbackSubmitRequest,
)

DRAFT_TTL = timedelta(minutes=30)
RATE_LIMIT = 5
RATE_WINDOW = timedelta(hours=24)
# An unknown row still missing on GitHub this long after it was sent did not land.
UNKNOWN_GIVE_UP = timedelta(minutes=10)
MAX_TEXT_CHARS = 20_000
RECONCILE_PER_READ = 3
SIMILAR_DAYS = 30
SIMILAR_LOCAL = 5
FALLBACK_DIR = "feedback"
_HOST_LABELS = {"cc": "Claude Code", "codex": "Codex"}
_LONG_FIELDS = ("body", "steps", "proposal", "alternatives", "original")
_FIELDS = {
    (FeedbackKind.BUG, FeedbackMode.NEW): ("title", "body", "steps", "original"),
    (FeedbackKind.IDEA, FeedbackMode.NEW): ("title", "body", "proposal", "alternatives", "original"),
    (FeedbackKind.BUG, FeedbackMode.COMMENT): ("body", "original"),
    (FeedbackKind.IDEA, FeedbackMode.COMMENT): ("body", "original"),
}
_REQUIRED = {
    (FeedbackKind.BUG, FeedbackMode.NEW): ("title", "body", "steps"),
    (FeedbackKind.IDEA, FeedbackMode.NEW): ("title", "body"),
}
_STOPWORDS = frozenset({"the", "and", "for", "with", "when", "does", "not", "from", "that", "this", "into",
                        "after", "before", "should", "would", "could", "can", "cannot", "add", "support"})
_KEYWORD = re.compile(r"\w{3,}")
MAX_KEYWORDS = 6


class FeedbackError(Exception):
    """A request the service turns down: an HTTP status and the reason."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Caller:
    """Who is asking: the session (CC) or MCP connection (HTTP hosts), the project and its folder."""

    session_id: str = ""
    mcp_session_id: str = ""
    project_id: str = ""
    project_dir: str = ""


@dataclass(frozen=True)
class _Probe:
    route: FeedbackRoute
    reason: str = ""
    login: str = ""
    lookup: Lookup | None = None
    keywords: str = ""
    search_failed: bool = False


def content_hash(kind: FeedbackKind, mode: FeedbackMode, repo: str, ref: str, fields: Mapping[str, str]) -> str:
    """The text's identity for dedupe: kind, destination and the user's fields, each NFKC-normalised,
    lowercased and whitespace-collapsed (the diagnostics, signature and marker do not count)."""

    def norm(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).lower().split())

    canonical = [str(kind), str(mode), norm(repo), norm(ref)] + [
        f"{name}={norm(fields[name])}" for name in sorted(fields) if fields[name]]
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode("utf-8")).hexdigest()


def _keywords(title: str) -> str:
    """Search terms from a title, joined with OR: search ANDs bare terms, and a report rarely
    shares every word of an earlier one (the best matches still rank first)."""
    words = [word for word in _KEYWORD.findall(title) if word.lower() not in _STOPWORDS]
    return " OR ".join(dict.fromkeys(words[:MAX_KEYWORDS]))


def _platform() -> str:
    """Operating system, its version and the CPU architecture, e.g. "macOS 15.1, arm64"."""
    system = platform.system()
    if system == "Darwin":
        name, version = "macOS", platform.mac_ver()[0]
    else:
        name, version = system, platform.release()
    return ", ".join(part for part in (f"{name} {version}".strip(), platform.machine()) if part)


def _hostnames() -> tuple[str, ...]:
    host = socket.gethostname()
    return tuple(dict.fromkeys(name for name in (host, host.split(".")[0]) if name))


def _login() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name to protect
        return ""


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _summary(row: FeedbackSubmission) -> dict[str, Any]:
    return {"id": row.id, "kind": row.kind, "mode": row.mode, "target_ref": row.target_ref, "title": row.title,
            "status": row.status, "route": row.route, "remote_number": row.remote_number,
            "remote_url": row.remote_url, "error": row.error, "origin_node": row.origin_node,
            "created_at": row.created_at.isoformat(), "sent_at": row.sent_at.isoformat() if row.sent_at else None}


class FeedbackService:
    """Drafts, confirmations and posts of one node; the clock and gh are injectable for tests."""

    def __init__(
        self,
        repo: FeedbackRepository,
        *,
        projects: Callable[[], Awaitable[list[tuple[str, str]]]],
        emit: Callable[..., Awaitable[Any]],
        data_dir: Path,
        target_repo: str | None = None,
        timeouts: Timeouts = Timeouts(),
        clock: Callable[[], datetime] = utc_now,
        auth: AppAuth | None = None,
    ) -> None:
        self.repo = repo
        self.projects = projects
        self.emit = emit
        self.data_dir = data_dir
        self.target_repo = target_repo or configured_repo()
        self.timeouts = timeouts
        self.clock = clock
        self.auth = auth

    # ------------------------------------------------------------------ GitHub

    def _gh(self) -> GitHub:
        """The repository through the user's gh; GitHubError(``missing``) without one."""
        return GitHub(self.target_repo, GhCli(locate()), self.timeouts)

    async def _app(self) -> GitHub | None:
        """The repository through OS's App token, refreshed when due; None without one."""
        if self.auth is None:
            return None
        credentials = await self.auth.token()
        return GitHub(self.target_repo, self.auth.transport(credentials), self.timeouts) if credentials else None

    async def _connect(self) -> tuple[GitHub, str]:
        """gh when it works, else the App token, with the account it acts as.

        GitHubError(``unauthenticated``) naming both reasons when neither does. A token
        GitHub turns down is dropped, so the next need authorizes again.
        """
        reasons = []
        try:
            github = self._gh()
            return github, await github.login()
        except GitHubError as exc:
            reasons.append(f"gh（{exc.kind}）：{exc}")
        try:
            github = await self._app()
            if github is not None:
                return github, await github.login()
            reasons.append("App 授权：本机还没有授权")
        except GitHubError as exc:
            if exc.kind == "unauthenticated" and self.auth is not None:
                await self.auth.forget()
            reasons.append(f"App 授权（{exc.kind}）：{exc}")
        raise GitHubError("unauthenticated", "；".join(reasons))

    async def _via(self, route: str) -> GitHub:
        """The transport a draft went by (publishing, reconciling), else whichever works now."""
        try:
            preferred = await self._app() if route == FeedbackRoute.APP else self._gh()
        except GitHubError:
            preferred = None
        if preferred is not None:
            return preferred
        github, _ = await self._connect()
        return github

    # ------------------------------------------------------------------ draft

    async def draft(self, request: FeedbackDraftRequest, caller: Caller) -> dict[str, Any]:
        if request.kind in (FeedbackKind.SECURITY, FeedbackKind.QUESTION):
            return self._not_filed(request.kind)
        mode = FeedbackMode.COMMENT if request.comment_on else FeedbackMode.NEW
        fields = self._fields(request, mode)
        draft_id = new_id()
        context, diagnostics = await self._context(request, caller)
        try:
            cleaned = await asyncio.to_thread(redact.clean, fields, context)
        except redact.Refused as refused:
            return await self._refuse(draft_id, request, mode, fields, refused.refusals, caller)
        draft = forms.Draft.of(request.kind, mode, cleaned.fields.get("title", ""),
                               {**cleaned.fields, "original_language": request.original_language})
        chash = content_hash(request.kind, mode, self.target_repo, request.comment_on, cleaned.fields)
        body = forms.render_body(draft, diagnostics, draft_id, chash)
        # The second line: the whole text as published, diagnostics included.
        final = redact.secret_hits({"title": draft.title, "body": body})
        if final:
            return await self._refuse(draft_id, request, mode, fields, final, caller)
        existing = await self._existing(chash)
        if existing is not None:
            return existing
        probe = await self._probe(draft, request.comment_on, request.dialog)
        lookup = probe.lookup
        row = await self.repo.insert(
            id=draft_id, kind=str(draft.kind), mode=str(mode), target_repo=self.target_repo,
            target_ref=request.comment_on, repository_id=lookup.repository_id if lookup else "",
            category_id=lookup.category_id(forms.IDEAS_CATEGORY) if lookup else "",
            target_node_id=lookup.target_node_id if lookup else "", title=draft.title, fields=draft.stored(),
            body=body, diagnostics=diagnostics.as_dict(), language=draft.original_language if draft.original else "en",
            content_hash=chash, status=FeedbackStatus.DRAFT, route=str(probe.route), error=probe.reason,
            gh_login=probe.login, session_id=caller.session_id, mcp_session_id=caller.mcp_session_id,
            author=request.author, project_id=caller.project_id, created_at=self.clock(),
        )
        return await self._preview(row, cleaned, probe)

    def _not_filed(self, kind: FeedbackKind) -> dict[str, Any]:
        if kind is FeedbackKind.SECURITY:
            return {"success": False, "status": "not_filed", "url": forms.security_url(self.target_repo),
                    "message": "安全问题不走公开渠道：请用户经这个链接私下报告（GitHub 私密漏洞报告）。"}
        return {"success": False, "status": "not_filed",
                "url": forms.category_url(self.target_repo, forms.QUESTIONS_CATEGORY),
                "message": "使用疑问不在这里提交：请用户到 Discussions 的 Q&A 分类提问。"}

    async def _preview(self, row: FeedbackSubmission, cleaned: redact.Cleaned, probe: _Probe) -> dict[str, Any]:
        """What the agent shows the user: the exact text, what was changed, what looks alike."""
        now = self.clock()
        handed = await self.repo.latest(self.target_repo, row.content_hash, FeedbackStatus.HANDED_OFF)
        local = await self.repo.sent_titles(row.kind, now - timedelta(days=SIMILAR_DAYS), SIMILAR_LOCAL)
        return {
            "success": True, "status": "draft", "draft_id": row.id,
            "expires_at": (row.created_at + DRAFT_TTL).isoformat(),
            "destination": forms.destination(row.kind, row.mode, row.target_ref, self.target_repo),
            "title": row.title, "body": row.body, "content_hash": row.content_hash,
            "diagnostics": row.diagnostics, "rewrites": cleaned.rewrites, "notes": cleaned.notes,
            "similar": [vars(item) for item in (probe.lookup.similar if probe.lookup else [])],
            "similar_sent_here": [{"title": r.title, "url": r.remote_url} for r in local],
            "searched_github_for": probe.keywords, "search_failed": probe.search_failed,
            "route": row.route, "route_reason": probe.reason,
            "previous_handoff": {"at": handed.updated_at.isoformat(), "draft_id": handed.id} if handed else None,
            "quota": {"used": await self.repo.count_live_since(now - RATE_WINDOW), "limit": RATE_LIMIT},
        }

    def _fields(self, request: FeedbackDraftRequest, mode: FeedbackMode) -> dict[str, str]:
        allowed = _FIELDS[(request.kind, mode)]
        given = {name: getattr(request, name) for name in ("title", *_LONG_FIELDS) if getattr(request, name).strip()}
        extra = sorted(set(given) - set(allowed))
        if extra:
            raise FeedbackError(400, f"{request.kind}/{mode} 不接受这些字段：{', '.join(extra)}")
        missing = [name for name in _REQUIRED.get((request.kind, mode), ("body",)) if name not in given]
        if missing:
            raise FeedbackError(400, f"缺少必填字段：{', '.join(missing)}")
        if sum(len(given.get(name, "")) for name in _LONG_FIELDS) > MAX_TEXT_CHARS:
            raise FeedbackError(400, f"正文合计超过 {MAX_TEXT_CHARS} 字符，请精简")
        return {name: given.get(name, "") for name in allowed}

    async def _context(self, request: FeedbackDraftRequest,
                       caller: Caller) -> tuple[redact.Context, forms.Diagnostics]:
        projects = await self.projects()
        return await asyncio.to_thread(self._read_context, request, caller, projects)

    def _read_context(self, request: FeedbackDraftRequest, caller: Caller,
                      projects: list[tuple[str, str]]) -> tuple[redact.Context, forms.Diagnostics]:
        """Files, names and versions of this machine (blocking reads: run in a worker thread)."""
        return self._redact_context(caller, projects), self._diagnostics(request)

    def _redact_context(self, caller: Caller, projects: list[tuple[str, str]]) -> redact.Context:
        try:
            terms = redact.load_terms(self.data_dir / redact.TERMS_FILE)
        except redact.TermListError as exc:
            raise FeedbackError(400, str(exc)) from None
        roots = [(caller.project_dir, "<project>")] + [(root, "<project>") for _, root in projects]
        roots += [(str(cc_config_dir()), "<claude-config>"), (str(own_cc_config_dir()), "<claude-config>"),
                  (str(codex_home()), "<codex-home>"), (str(self.data_dir), "<aiteam-data>")]
        try:
            roots.append((str(os_data_dir()), "<aiteam-data>"))
        except DataDirMissing:
            pass
        names = [name for name, _ in projects] + [Path(root).name for _, root in projects if root]
        return redact.Context(roots=tuple((path, label) for path, label in roots if path), home=str(Path.home()),
                              login=_login(), hostnames=_hostnames(), project_names=tuple(names), terms=terms)

    @staticmethod
    def _diagnostics(request: FeedbackDraftRequest) -> forms.Diagnostics:
        """The whitelist: OS version, install method, host and its version (from the client), platform."""
        installed = install_kind.detect_sync(request.host).kind
        return forms.Diagnostics(
            version=aiteam.__version__, install_method=forms.install_method(installed),
            host=f"{request.host_label or _HOST_LABELS[request.host]} {request.host_version}".strip(),
            platform=_platform())

    async def _refuse(self, draft_id: str, request: FeedbackDraftRequest, mode: FeedbackMode,
                      fields: Mapping[str, str], refusals: list[redact.Refusal], caller: Caller) -> dict[str, Any]:
        """Record the refusal (rules and lines, no text) and say what to change."""
        stored = [refusal.stored() for refusal in refusals]
        chash = content_hash(request.kind, mode, self.target_repo, request.comment_on, fields)
        await self.repo.insert(
            id=draft_id, kind=str(request.kind), mode=str(mode), target_repo=self.target_repo,
            target_ref=request.comment_on, content_hash=chash, status=FeedbackStatus.REFUSED, refusal=stored,
            session_id=caller.session_id, mcp_session_id=caller.mcp_session_id, author=request.author,
            project_id=caller.project_id, created_at=self.clock(),
        )
        await self.emit(EventType.FEEDBACK_REFUSED, "feedback",
                        {"draft_id": draft_id, "content_hash": chash, "refusal": stored,
                         "session_id": caller.session_id, "mcp_session_id": caller.mcp_session_id},
                        entity_id=draft_id, entity_type="feedback_submission")
        secrets = [r for r in refusals if r.rule != redact.PRIVATE_TERM]
        advice = ("疑似密钥：去掉命中的内容后重新起草（这里没有放行，目的地是公开仓库）。" if secrets else
                  "命中本机私有术语词表：请用户改写这些词后重新起草（不会自动替换）。")
        return {"success": False, "status": "refused", "draft_id": draft_id, "advice": advice,
                "refusal": [{**refusal.stored(), **({"term": refusal.term} if refusal.term else {})}
                            for refusal in refusals]}

    async def _existing(self, chash: str) -> dict[str, Any] | None:
        """The answer when this text is already published (or being published), else None."""
        rows = [await self._settle(row) for row in await self.repo.holding(self.target_repo, chash)]
        for row in rows:
            if row.status is FeedbackStatus.SENT:
                return {"success": True, "status": "duplicate", "draft_id": row.id, "remote_url": row.remote_url,
                        "remote_number": row.remote_number,
                        "message": "同样的内容已经发过，不再新建；把这个链接告诉用户。"}
        for row in rows:
            if row.status in (FeedbackStatus.SENDING, FeedbackStatus.UNKNOWN):
                where = "" if is_own(row.origin_node) else f"（来自节点 {row.origin_node}）"
                return {"success": False, "status": "pending", "draft_id": row.id,
                        "message": f"同样的内容正在发送或结果未知{where}；稍后用 feedback_list 查看结果，不要重发。"}
        return None

    async def _probe(self, draft: forms.Draft, ref: str, dialog: bool) -> _Probe:
        """How this draft can be published (gh, the App token, authorizing the App, the web),
        the ids it needs, and similar posts."""
        try:
            github, login = await self._connect()
        except GitHubError as exc:
            if dialog and self.auth is not None:
                return _Probe(FeedbackRoute.AUTH, f"需要用户授权 GitHub（{exc}）", search_failed=True)
            return _Probe(FeedbackRoute.WEB, f"gh 与 GitHub 授权都不可用：{exc}")
        return await self._lookup(github, login, draft, ref)

    async def _lookup(self, github: GitHub, login: str, draft: forms.Draft, ref: str) -> _Probe:
        route = FeedbackRoute(github.via)
        keywords = _keywords(draft.title) if draft.mode is FeedbackMode.NEW else ""
        try:
            lookup = await github.lookup(keywords, ref)
        except GitHubError as exc:
            if exc.kind == "not_found":
                raise FeedbackError(404, f"{self.target_repo} 里没有 {ref or '这个仓库'}：{exc}") from None
            return _Probe(FeedbackRoute.WEB, f"未能查询仓库与查重（{exc.kind}）：{exc}", login, keywords=keywords,
                          search_failed=True)
        if ref and not lookup.target_node_id:
            raise FeedbackError(404, f"{self.target_repo} 里没有 {ref}")
        if not lookup.repository_id:
            return _Probe(FeedbackRoute.WEB, "未能读到仓库 ID", login, lookup, keywords)
        if draft.kind is FeedbackKind.IDEA and draft.mode is FeedbackMode.NEW \
                and not lookup.category_id(forms.IDEAS_CATEGORY):
            return _Probe(FeedbackRoute.WEB, "仓库的 Discussions 里找不到 Ideas 分类", login, lookup, keywords)
        return _Probe(route, "", login, lookup, keywords)

    # ------------------------------------------------------------------ read

    async def view(self, draft_id: str) -> FeedbackSubmission:
        """A row's text for the read-only full-text page. Never settles the row (no write):
        missing, refused (no text kept) or a draft past its 30 minutes is not found."""
        row = await self.repo.get(draft_id)
        lapsed = row is not None and row.status is FeedbackStatus.DRAFT and row.created_at < self.clock() - DRAFT_TTL
        if row is None or not row.body or row.status is FeedbackStatus.EXPIRED or lapsed:
            raise FeedbackError(404, f"没有可查看的草稿：{draft_id}（不存在、已过期或已拒发）")
        return row

    async def get(self, draft_id: str) -> FeedbackSubmission:
        row = await self.repo.get(draft_id)
        if row is None:
            raise FeedbackError(404, f"没有这份草稿：{draft_id}")
        return await self._settle(row)

    async def ledger(self, status: FeedbackStatus | None, limit: int) -> list[dict[str, Any]]:
        now = self.clock()
        await self.repo.expire_drafts(now - DRAFT_TTL)
        for row in await self.repo.unsettled(now - self._sending_grace, RECONCILE_PER_READ):
            await self._reconcile(row)
        return [_summary(row) for row in await self.repo.recent(status, limit)]

    @property
    def _sending_grace(self) -> timedelta:
        """How long a row may honestly be sending: the marker search, the send and a margin."""
        return timedelta(seconds=self.timeouts.search + self.timeouts.send + 10)

    async def _own(self, draft_id: str) -> FeedbackSubmission:
        row = await self.get(draft_id)
        if not is_own(row.origin_node):
            raise FeedbackError(404, f"草稿 {draft_id} 不是本机起草的，只能在起草它的机器上发送")
        return row

    async def _settle(self, row: FeedbackSubmission) -> FeedbackSubmission:
        """Expire or reconcile a row of this node as reading it finds it."""
        if not is_own(row.origin_node):
            return row
        now = self.clock()
        if row.status is FeedbackStatus.DRAFT and row.created_at < now - DRAFT_TTL:
            return await self.repo.update(row.id, FeedbackStatus.EXPIRED) or row
        if row.status is FeedbackStatus.UNKNOWN or (
                row.status is FeedbackStatus.SENDING and row.sent_at and row.sent_at < now - self._sending_grace):
            return await self._reconcile(row)
        return row

    async def _reconcile(self, row: FeedbackSubmission) -> FeedbackSubmission:
        """Look for an unknown (or abandoned sending) row's post on GitHub; never post again."""
        token = forms.hash_token(row.content_hash)
        try:
            found = await self._find(await self._via(row.route), row, token)
        except GitHubError:
            found, checked = None, False
        else:
            checked = True
        if found is not None:
            settled = await self.repo.update(row.id, FeedbackStatus.SENT, remote_url=found.url,
                                             remote_number=found.number, error="") or row
            await self._audit(settled, Caller(row.session_id, row.mcp_session_id), reconciled=True)
            return settled
        sent_at = row.sent_at or row.updated_at
        if checked and sent_at < self.clock() - UNKNOWN_GIVE_UP:
            return await self.repo.update(row.id, FeedbackStatus.FAILED,
                                          error="发送结果未知，GitHub 上也找不到；可以重新起草") or row
        if row.status is FeedbackStatus.SENDING:
            return await self.repo.update(row.id, FeedbackStatus.UNKNOWN,
                                          error="发送中断，结果未知；下次读到时再核对") or row
        return row

    async def _find(self, github: GitHub, row: FeedbackSubmission, token: str) -> Posted | None:
        since = _iso((row.sent_at or row.created_at) - timedelta(minutes=2))
        kind, _, number = row.target_ref.partition(":")
        if row.mode is FeedbackMode.COMMENT:
            if kind == "issue":
                return await github.find_issue_comment(int(number), since, token)
            return await github.find_discussion(row.gh_login, token, int(number))
        if row.kind is FeedbackKind.BUG:
            return await github.find_issue(row.gh_login, since, token)
        return await github.find_discussion(row.gh_login, token)

    # ------------------------------------------------------------------ submit

    def _check_confirmable(self, row: FeedbackSubmission, request: FeedbackSubmitRequest) -> None:
        if row.status is FeedbackStatus.EXPIRED:
            raise FeedbackError(410, "草稿已过期（30 分钟），请重新起草")
        if row.status is not FeedbackStatus.DRAFT:
            raise FeedbackError(409, f"草稿状态为 {row.status}，不能发送")
        if request.content_hash != row.content_hash:
            raise FeedbackError(409, "内容哈希与草稿不一致：用户确认的不是这份草稿")
        if row.route == FeedbackRoute.WEB:
            raise FeedbackError(409, f"这份草稿只能由用户在 GitHub 网页上提交（{row.error}）")

    async def confirm(self, draft_id: str, request: FeedbackSubmitRequest) -> dict[str, Any]:
        """Record the user's confirmation of a draft that still waits for GitHub authorization,
        so a later submit (after the user authorized the App) does not ask again."""
        row = await self._own(draft_id)
        self._check_confirmable(row, request)
        confirmed = await self.repo.update(row.id, FeedbackStatus.DRAFT, confirm_method=str(request.confirm_method),
                                           confirmed_at=self.clock()) or row
        return {"success": True, "status": "draft", "draft_id": row.id, "route": str(confirmed.route),
                "confirmed_at": confirmed.confirmed_at.isoformat() if confirmed.confirmed_at else None}

    async def submit(self, draft_id: str, request: FeedbackSubmitRequest, caller: Caller) -> dict[str, Any]:
        row = await self._own(draft_id)
        if row.status is FeedbackStatus.SENT:
            return {"success": True, "status": "sent", "already": True, "remote_url": row.remote_url,
                    "remote_number": row.remote_number}
        if row.status in (FeedbackStatus.SENDING, FeedbackStatus.UNKNOWN):
            return {"success": False, "status": str(row.status),
                    "message": "发送结果尚未确定，不要重发；稍后用 feedback_list 查看"}
        self._check_confirmable(row, request)
        if row.route == FeedbackRoute.AUTH:
            row = await self._authorized(row)
        now = self.clock()
        try:
            row = await self.repo.claim(draft_id, since=now - RATE_WINDOW, limit=RATE_LIMIT, now=now, values={
                "confirm_method": str(request.confirm_method), "confirmed_at": now})
        except RateLimited as exc:
            raise FeedbackError(429, f"本机 24 小时内已发 {exc.count} 条反馈，达到上限 {exc.limit} 条；"
                                     "明天再发") from None
        except Duplicate as exc:
            existing = exc.existing
            return {"success": False, "status": "duplicate", "remote_url": existing.remote_url if existing else "",
                    "message": "同样的内容已经发过或正在发送，不再新建"}
        except NotClaimable as exc:
            raise FeedbackError(409, f"草稿状态为 {exc.status}，不能发送") from None
        return await self._publish(row, caller)

    async def _authorized(self, row: FeedbackSubmission) -> FeedbackSubmission:
        """A draft drafted before any credential: now that one exists, the ids its post needs."""
        try:
            github, login = await self._connect()
        except GitHubError as exc:
            raise FeedbackError(409, f"还没有 GitHub 授权，先完成授权再发送（{exc}）") from None
        draft = forms.Draft.of(row.kind, row.mode, row.title, row.fields)
        probe = await self._lookup(github, login, draft, row.target_ref)
        if probe.lookup is None or probe.route == FeedbackRoute.WEB:
            raise FeedbackError(503, f"授权后未能查询仓库：{probe.reason}")
        lookup = probe.lookup
        return await self.repo.update(
            row.id, FeedbackStatus.DRAFT, route=str(probe.route), gh_login=login, error="",
            repository_id=lookup.repository_id, category_id=lookup.category_id(forms.IDEAS_CATEGORY),
            target_node_id=lookup.target_node_id) or row

    # ------------------------------------------------------------------ authorization

    async def start_authorization(self, draft_id: str) -> DeviceStart:
        """GitHub's code for the user to authorize OS's App, for a draft waiting to be sent."""
        if self.auth is None:
            raise FeedbackError(409, "这台机器的 OS 没有配置 GitHub App 授权")
        row = await self._own(draft_id)
        if row.status is not FeedbackStatus.DRAFT:
            raise FeedbackError(409 if row.status is not FeedbackStatus.EXPIRED else 410,
                                f"草稿状态为 {row.status}，不需要授权")
        try:
            return await self.auth.start(draft_id)
        except GitHubError as exc:
            raise FeedbackError(502, f"向 GitHub 申请授权码失败：{exc}") from None

    async def poll_authorization(self, flow_id: str) -> PollResult:
        if self.auth is None:
            raise FeedbackError(409, "这台机器的 OS 没有配置 GitHub App 授权")
        try:
            return await self.auth.poll(flow_id)
        except GitHubError as exc:  # a network hiccup is not an answer: ask again next time
            return PollResult("pending", message=str(exc))

    async def account(self) -> dict[str, Any]:
        """Which way feedback would go now, and as whom; never a token."""
        gh_login, gh_reason = "", ""
        try:
            gh_login = await self._gh().login()
        except GitHubError as exc:
            gh_reason = f"{exc.kind}: {exc}"
        app = await self.auth.account() if self.auth is not None else {"app_login": "", "app_expires_at": None}
        route = "gh" if gh_login else ("app" if app.get("app_login") else "none")
        return {"route": route, "gh_login": gh_login, "gh_reason": gh_reason, **app}

    async def sign_out(self) -> dict[str, Any]:
        removed = await self.auth.sign_out() if self.auth is not None else False
        return {"signed_out": removed, "revoke_page": REVOKE_PAGE,
                "message": "本机的 App 授权已删除。要在 GitHub 上一并撤销，请打开 revoke_page。"}

    async def _publish(self, row: FeedbackSubmission, caller: Caller) -> dict[str, Any]:
        try:
            github = await self._via(row.route)
        except GitHubError as exc:
            return await self._failed(row, exc)
        try:
            copy = await github.search_marker(forms.hash_token(row.content_hash))
        except GitHubError:
            copy = None  # search is best effort: the local index and the preview's list are the other lines
        if copy is not None:
            await self.repo.update(row.id, FeedbackStatus.FAILED, remote_url=copy.url, remote_number=copy.number,
                                   error="duplicate: 仓库里已有带同一标记的帖子")
            return {"success": False, "status": "duplicate", "remote_url": copy.url,
                    "message": "仓库里已有同样内容的帖子（可能从另一台机器发出），不再新建"}
        try:
            posted = await self._send(github, row)
        except GitHubError as exc:
            if exc.kind == "timeout":
                await self.repo.update(row.id, FeedbackStatus.UNKNOWN, error=str(exc))
                return {"success": False, "status": "unknown",
                        "message": "GitHub 超时，帖子可能已经发出；不要重发，稍后用 feedback_list 查看核对结果"}
            if exc.kind == "unauthenticated" and github.via == FeedbackRoute.APP and self.auth is not None:
                await self.auth.forget()
            return await self._failed(row, exc)
        sent = await self.repo.update(row.id, FeedbackStatus.SENT, remote_url=posted.url, route=github.via,
                                      remote_number=posted.number, error="") or row
        await self._audit(sent, caller, reconciled=False)
        return {"success": True, "status": "sent", "remote_url": posted.url, "remote_number": posted.number,
                "labels": list(posted.labels)}

    async def _send(self, github: GitHub, row: FeedbackSubmission) -> Posted:
        kind, _, number = row.target_ref.partition(":")
        if row.mode is FeedbackMode.COMMENT:
            if kind == "issue":
                return await github.comment_issue(int(number), row.body)
            return await github.comment_discussion(row.target_node_id, row.body)
        if row.kind is FeedbackKind.BUG:
            return await github.create_issue(row.title, row.body, ["bug"])
        return await github.create_discussion(row.repository_id, row.category_id, row.title, row.body)

    async def _failed(self, row: FeedbackSubmission, exc: GitHubError) -> dict[str, Any]:
        stale = exc.kind == "not_found" and (row.mode is FeedbackMode.COMMENT or row.kind is FeedbackKind.IDEA)
        error = f"{exc.kind}: {exc}" + ("（GitHub 上的目标或分类已不存在，草稿作废，请重新起草）" if stale else "")
        await self.repo.update(row.id, FeedbackStatus.FAILED, error=error)
        web = exc.kind in ("missing", "unauthenticated", "forbidden")
        return {"success": False, "status": "failed", "error_kind": exc.kind, "error": error,
                "fallback": "web" if web else "", "draft_id": row.id}

    async def _audit(self, row: FeedbackSubmission, caller: Caller, *, reconciled: bool) -> None:
        await self.emit(EventType.FEEDBACK_SUBMITTED, "feedback", {
            "draft_id": row.id, "content_hash": row.content_hash, "target_repo": row.target_repo,
            "kind": str(row.kind), "mode": str(row.mode), "target_ref": row.target_ref,
            "remote_number": row.remote_number, "remote_url": row.remote_url,
            "confirm_method": row.confirm_method or str(FeedbackConfirmMethod.ELICITATION),
            "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
            "session_id": caller.session_id, "mcp_session_id": caller.mcp_session_id,
            "drafted_in_session": row.session_id, "gh_login": row.gh_login, "auth": str(row.route),
            "reconciled": reconciled,
        }, entity_id=row.id, entity_type="feedback_submission")

    # ------------------------------------------------------------------ handoff

    async def handoff(self, draft_id: str, reason: str) -> dict[str, Any]:
        row = await self._settle(await self._own(draft_id))
        if row.status is FeedbackStatus.EXPIRED:
            raise FeedbackError(410, "草稿已过期（30 分钟），请重新起草")
        if row.status not in (FeedbackStatus.DRAFT, FeedbackStatus.FAILED, FeedbackStatus.HANDED_OFF):
            raise FeedbackError(409, f"草稿状态为 {row.status}，不能转网页提交")
        draft = forms.Draft.of(row.kind, row.mode, row.title, row.fields)
        diagnostics = forms.Diagnostics(**row.diagnostics)
        file = ""
        if row.mode is FeedbackMode.COMMENT:
            url = forms.target_url(self.target_repo, row.target_ref)
            file = await self._write_fallback(row.id, forms.copyable_text(draft, diagnostics, row.body))
        else:
            url = forms.new_post_url(self.target_repo, draft, diagnostics)
            if not forms.fits(url):
                url = forms.new_post_url(self.target_repo, draft, diagnostics, prefill=False)
                file = await self._write_fallback(row.id, forms.copyable_text(draft, diagnostics, row.body))
        await self.repo.update(row.id, FeedbackStatus.HANDED_OFF, route=str(FeedbackRoute.WEB), error=reason)
        how = ("打开链接，按文件里各小节的内容逐项粘贴，检查后在 GitHub 上点提交" if file else
               "打开链接，检查 GitHub 页面上预填的内容，确认后由用户自己点提交")
        return {"success": True, "status": "handed_off", "url": url, "file": file, "reason": reason, "how": how}

    async def _write_fallback(self, draft_id: str, text: str) -> str:
        path = self.data_dir / FALLBACK_DIR / f"{draft_id}.md"
        await asyncio.to_thread(_write_private, path, text)
        return str(path)
