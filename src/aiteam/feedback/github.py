"""GitHub for feedback, reached two ways: the user's gh, or OS's GitHub App user token.

* ``GhCli``: the user's own gh, found by absolute path (``AITEAM_GH_BIN``, else PATH
  and the usual install folders, aiteam.executables). Every call is ``gh api
  --hostname github.com`` with ``GH_PROMPT_DISABLED``, ``GH_NO_UPDATE_NOTIFIER`` and
  ``NO_COLOR`` set, a child process awaited with a time limit (past it the process
  group is killed and reaped). ``AITEAM_FEEDBACK_GH=off`` makes feedback act as if
  there were no gh (tests and live trials of the token path).
* ``AppToken``: the user access token of OS's GitHub App (aiteam.feedback.auth),
  sent to api.github.com with httpx. It reaches only the repositories the App is
  installed on, and only their issues and discussions.

Both carry the same calls of one ``GitHub``: dedupe, marker search, reconciling and
publishing do not know which one runs. Every body travels in the request (gh: on
standard input, never on the command line, where the process table shows it).
Issues go through REST, Discussions through GraphQL (``gh discussion`` is a preview
command older gh releases lack). Errors come back as GitHubError with a kind the
caller decides on (``missing``, ``unauthenticated``, ``forbidden``, ``not_found``,
``invalid``, ``rate_limited``, ``timeout``, ``failed``).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from aiteam import executables

GH_ENV = "AITEAM_GH_BIN"
GH_SWITCH_ENV = "AITEAM_FEEDBACK_GH"
REPO_ENV = "AITEAM_FEEDBACK_REPO"
DEFAULT_REPO = "CronusL-1141/AI-company"
HOST = "github.com"
API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "ai-team-os-feedback"
REPO_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
SEARCH_RESULTS = 5
_MAX_ERROR_CHARS = 300
_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}


@dataclass(frozen=True)
class Timeouts:
    """Seconds per kind of call (design: login 5, lookup 8, marker search 5, send 15)."""

    login: float = 5.0
    lookup: float = 8.0
    search: float = 5.0
    send: float = 15.0


class GitHubError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Posted:
    """Where something landed: its number (none for a comment), page and node id."""

    url: str
    number: int | None = None
    node_id: str = ""
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class Similar:
    kind: str  # issue / discussion
    number: int
    title: str
    state: str
    url: str


@dataclass(frozen=True)
class Lookup:
    """The ids a draft needs and what looks like it, from one GraphQL query."""

    repository_id: str
    categories: dict[str, str]  # slug -> id
    category_names: dict[str, str]  # name (lowercase) -> id
    similar: list[Similar] = field(default_factory=list)
    target_node_id: str = ""  # the issue or discussion a comment goes to
    target_title: str = ""

    def category_id(self, slug: str) -> str:
        return self.categories.get(slug) or self.category_names.get(slug.lower(), "")


def configured_repo() -> str:
    """The target repository: AITEAM_FEEDBACK_REPO of the API process (tests), else the public one."""
    repo = os.environ.get(REPO_ENV, "").strip() or DEFAULT_REPO
    if not REPO_PATTERN.fullmatch(repo):
        raise ValueError(f"{REPO_ENV} 须为 owner/name：{repo!r}")
    return repo


def locate() -> Path:
    """The gh to run; GitHubError(``missing``) when there is none, or feedback is told not to use one."""
    if os.environ.get(GH_SWITCH_ENV, "").strip().lower() == "off":
        raise GitHubError("missing", f"{GH_SWITCH_ENV}=off：反馈不使用 gh")
    try:
        found = executables.find("gh", env_var=GH_ENV)
    except executables.ExecutableConfigError as exc:
        raise GitHubError("missing", str(exc)) from None
    if found is None:
        raise GitHubError("missing", f"未找到 gh（已查 PATH 与 {'、'.join(executables.PATH_EXTRA_DIRS)}）；"
                                     f"可安装 GitHub CLI，或用 {GH_ENV} 指定绝对路径")
    return found


def _kind(text: str) -> str:
    """The error kind told by an answer's text (gh's stderr and stdout, or an HTTP status and body)."""
    lowered = text.lower()
    compact = lowered.replace(" ", "")  # a GraphQL error's "type", however the JSON is spaced
    for kind, signs in (("unauthenticated", ("auth login", "(http 401)", "bad credentials")),
                        ("rate_limited", ("rate limit", '"type":"rate_limited"')),
                        ("forbidden", ("(http 403)", '"type":"forbidden"')),
                        ("not_found", ("(http 404)", '"type":"not_found"')),
                        ("invalid", ("(http 422)",))):
        if any(sign in (compact if sign.startswith('"') else lowered) for sign in signs):
            return kind
    return "failed"


def _error(kind: str, message: str, *, who: str) -> GitHubError:
    if kind == "unauthenticated":
        return GitHubError(kind, "gh 未登录或登录已失效（gh auth login）" if who == "gh"
                           else "GitHub 授权已失效，需要重新授权")
    return GitHubError(kind, message[:_MAX_ERROR_CHARS])


def _kill(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _has_marker(text: str | None, token: str) -> bool:
    return bool(text) and token in text


class Transport(Protocol):
    """One way of sending a GitHub API request: ``endpoint`` is ``graphql`` or a REST path."""

    name: str

    async def request(self, method: str, endpoint: str, payload: dict[str, Any] | None, timeout: float) -> Any:
        ...


class GhCli:
    """The user's gh, run as ``gh api`` with the body on standard input."""

    name = "gh"

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    async def request(self, method: str, endpoint: str, payload: dict[str, Any] | None, timeout: float) -> Any:
        args = [endpoint] if endpoint == "graphql" else ["--method", method, endpoint]
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        if data is not None:
            args += ["--input", "-"]
        process = await asyncio.create_subprocess_exec(
            str(self.binary), "api", "--hostname", HOST, *args,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **_ENV}, start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout)
        except BaseException as exc:  # a timeout or a cancellation: never leave the child behind
            _kill(process)
            await process.wait()
            if isinstance(exc, TimeoutError):
                raise GitHubError("timeout", f"gh 在 {timeout:g} 秒内没有返回") from None
            raise
        out, err = stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
        if process.returncode != 0:
            kind = "unauthenticated" if process.returncode == 4 else _kind(f"{out}\n{err}")
            message = " ".join(err.strip().removeprefix("gh: ").split()) or f"gh 退出码 {process.returncode}"
            raise _error(kind, message, who="gh")
        try:
            return json.loads(out) if out.strip() else None
        except ValueError:
            raise GitHubError("failed", "gh 的输出不是 JSON") from None


def default_http() -> httpx.AsyncClient:
    """A client for api.github.com and github.com (proxy settings come from the environment)."""
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept": "application/json"})


class AppToken:
    """OS's GitHub App user token, sent to the REST and GraphQL API. Never logged or returned."""

    name = "app"

    def __init__(self, token: str, http: Callable[[], httpx.AsyncClient] = default_http,
                 api_url: str = API_URL) -> None:
        self._token = token
        self._http = http
        self._api_url = api_url.rstrip("/")

    def __repr__(self) -> str:
        return "AppToken(<hidden>)"

    async def request(self, method: str, endpoint: str, payload: dict[str, Any] | None, timeout: float) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "X-GitHub-Api-Version": API_VERSION,
                   "Accept": "application/vnd.github+json"}
        try:
            async with self._http() as client:
                response = await client.request(method, f"{self._api_url}/{endpoint}", json=payload,
                                                headers=headers, timeout=timeout)
        except httpx.TimeoutException:
            raise GitHubError("timeout", f"GitHub 在 {timeout:g} 秒内没有返回") from None
        except httpx.HTTPError as exc:
            raise GitHubError("failed", f"连不上 GitHub：{type(exc).__name__}") from None
        text = response.text
        try:
            body = response.json() if text.strip() else None
        except ValueError:
            body = None
        message = str(body.get("message") or "") if isinstance(body, dict) else ""
        if response.status_code >= 400:
            raise _error(_kind(f"(http {response.status_code}) {text}"), message or f"HTTP {response.status_code}",
                         who="app")
        if endpoint == "graphql" and isinstance(body, dict) and body.get("errors"):
            first = body["errors"][0] if isinstance(body["errors"], list) and body["errors"] else {}
            raise _error(_kind(json.dumps(body["errors"])), str(first.get("message") or "GraphQL 出错"), who="app")
        return body


class GitHub:
    """The calls feedback makes, for one repository, through one transport (gh or the App token)."""

    def __init__(self, repo: str, transport: Transport, timeouts: Timeouts = Timeouts()) -> None:
        self.repo = repo
        self.owner, self.name = repo.split("/", 1)
        self.transport = transport
        self.timeouts = timeouts

    @property
    def via(self) -> str:
        """``gh`` or ``app``: the transport, recorded with what it published."""
        return self.transport.name

    async def _graphql(self, query: str, variables: dict[str, Any], timeout: float) -> dict[str, Any]:
        answer = await self.transport.request("POST", "graphql", {"query": query, "variables": variables}, timeout)
        data = answer.get("data") if isinstance(answer, dict) else None
        if not isinstance(data, dict):
            raise GitHubError("failed", "GraphQL 没有返回 data")
        return data

    async def _rest(self, method: str, path: str, payload: dict[str, Any] | None, timeout: float) -> Any:
        return await self.transport.request(method, path, payload, timeout)

    # -- who and what ------------------------------------------------------------

    async def login(self) -> str:
        """The account the transport acts as; a call that succeeds also proves the credential works."""
        answer = await self._rest("GET", "user", None, self.timeouts.login)
        login = answer.get("login") if isinstance(answer, dict) else None
        if not isinstance(login, str) or not login:
            raise GitHubError("failed", "GET /user 没有返回登录名")
        return login

    async def lookup(self, keywords: str, comment_on: str = "") -> Lookup:
        """Repository and category ids, up to five similar issues and discussions, the comment target."""
        kind, _, number = comment_on.partition(":")
        scope = f"repo:{self.repo}"
        data = await self._graphql(LOOKUP_QUERY, {
            "owner": self.owner, "name": self.name,
            "issues": f"{scope} is:issue {keywords}", "discussions": f"{scope} {keywords}",
            "search": bool(keywords), "first": SEARCH_RESULTS,
            "number": int(number) if number else 0, "issue": kind == "issue", "discussion": kind == "discussion",
        }, self.timeouts.lookup)
        repository = data.get("repository") or {}
        nodes = (repository.get("discussionCategories") or {}).get("nodes") or []
        similar = [Similar("issue", n["number"], n["title"], str(n.get("state", "")).lower(), n["url"])
                   for n in (data.get("issues") or {}).get("nodes") or [] if n and "number" in n]
        similar += [Similar("discussion", n["number"], n["title"], "closed" if n.get("closed") else "open", n["url"])
                    for n in (data.get("discussions") or {}).get("nodes") or [] if n and "number" in n]
        target = repository.get("issue") or repository.get("discussion") or {}
        return Lookup(
            repository_id=str(repository.get("id") or ""),
            categories={str(n["slug"]): str(n["id"]) for n in nodes if n and n.get("slug")},
            category_names={str(n["name"]).lower(): str(n["id"]) for n in nodes if n and n.get("name")},
            similar=similar, target_node_id=str(target.get("id") or ""), target_title=str(target.get("title") or ""),
        )

    # -- publishing ----------------------------------------------------------------

    async def create_issue(self, title: str, body: str, labels: list[str]) -> Posted:
        answer = await self._rest("POST", f"repos/{self.repo}/issues",
                                  {"title": title, "body": body, "labels": labels}, self.timeouts.send)
        return Posted(url=answer["html_url"], number=answer["number"], node_id=answer.get("node_id", ""),
                      labels=tuple(label.get("name", "") for label in answer.get("labels") or []))

    async def comment_issue(self, number: int, body: str) -> Posted:
        answer = await self._rest("POST", f"repos/{self.repo}/issues/{number}/comments", {"body": body},
                                  self.timeouts.send)
        return Posted(url=answer["html_url"], node_id=answer.get("node_id", ""))

    async def create_discussion(self, repository_id: str, category_id: str, title: str, body: str) -> Posted:
        data = await self._graphql(CREATE_DISCUSSION, {"input": {
            "repositoryId": repository_id, "categoryId": category_id, "title": title, "body": body}},
            self.timeouts.send)
        discussion = (data.get("createDiscussion") or {}).get("discussion") or {}
        return Posted(url=discussion["url"], number=discussion["number"], node_id=discussion.get("id", ""))

    async def comment_discussion(self, discussion_id: str, body: str) -> Posted:
        data = await self._graphql(ADD_DISCUSSION_COMMENT, {"input": {"discussionId": discussion_id, "body": body}},
                                   self.timeouts.send)
        comment = (data.get("addDiscussionComment") or {}).get("comment") or {}
        return Posted(url=comment["url"], node_id=comment.get("id", ""))

    # -- finding a post again ------------------------------------------------------

    async def search_marker(self, token: str) -> Posted | None:
        """A post or comment of this repository carrying ``token`` (the marker's hash), via search.

        Search has an index delay: it finds what was posted minutes ago, not seconds. It
        looks for the hash alone (search splits ``h=`` off), then each hit is checked for
        the whole token.
        """
        term = f'repo:{self.repo} "{token.partition("=")[2] or token}"'
        # Issue search must say is:issue (REST answers 422 without it for an App token).
        data = await self._graphql(MARKER_QUERY, {"issues": f"{term} is:issue", "discussions": term},
                                   self.timeouts.search)
        for hits in ((data.get("issues") or {}).get("nodes") or [], (data.get("discussions") or {}).get("nodes") or []):
            for node in hits:
                if not node:
                    continue
                if _has_marker(node.get("body"), token):
                    return Posted(url=node["url"], number=node.get("number"), node_id=node.get("id", ""))
                for comment in (node.get("comments") or {}).get("nodes") or []:
                    if comment and _has_marker(comment.get("body"), token):
                        return Posted(url=comment["url"], node_id=comment.get("id", ""))
        return None

    async def find_issue(self, login: str, since: str, token: str) -> Posted | None:
        """An issue opened by ``login`` since ``since`` (ISO) with ``token``: the list API, no index delay."""
        query = urllib.parse.urlencode({"creator": login, "since": since, "state": "all", "per_page": 50})
        for issue in await self._rest("GET", f"repos/{self.repo}/issues?{query}", None, self.timeouts.search) or []:
            if _has_marker(issue.get("body"), token):
                return Posted(url=issue["html_url"], number=issue["number"], node_id=issue.get("node_id", ""))
        return None

    async def find_issue_comment(self, number: int, since: str, token: str) -> Posted | None:
        query = urllib.parse.urlencode({"since": since, "per_page": 100})
        for comment in await self._rest("GET", f"repos/{self.repo}/issues/{number}/comments?{query}", None,
                                        self.timeouts.search) or []:
            if _has_marker(comment.get("body"), token):
                return Posted(url=comment["html_url"], node_id=comment.get("node_id", ""))
        return None

    async def find_discussion(self, login: str, token: str, number: int = 0) -> Posted | None:
        """A discussion (``number`` 0) or a comment on discussion ``number``, by ``login``, with ``token``."""
        data = await self._graphql(RECENT_DISCUSSIONS, {
            "owner": self.owner, "name": self.name, "number": number, "comments": number > 0,
        }, self.timeouts.search)
        repository = data.get("repository") or {}
        if number:
            nodes = (((repository.get("discussion") or {}).get("comments")) or {}).get("nodes") or []
        else:
            nodes = (repository.get("discussions") or {}).get("nodes") or []
        for node in nodes:
            author = ((node or {}).get("author") or {}).get("login")
            if node and author == login and _has_marker(node.get("body"), token):
                return Posted(url=node["url"], number=node.get("number"), node_id=node.get("id", ""))
        return None


# Named operations: the GraphQL a call sends is fixed here, its values are variables.
LOOKUP_QUERY = """query FeedbackLookup($owner: String!, $name: String!, $issues: String!, $discussions: String!,
  $search: Boolean!, $first: Int!, $number: Int!, $issue: Boolean!, $discussion: Boolean!) {
  repository(owner: $owner, name: $name) {
    id
    discussionCategories(first: 25) { nodes { id name slug } }
    issue(number: $number) @include(if: $issue) { id number title url state }
    discussion(number: $number) @include(if: $discussion) { id number title url closed }
  }
  issues: search(query: $issues, type: ISSUE, first: $first) @include(if: $search) {
    nodes { ... on Issue { number title state url } }
  }
  discussions: search(query: $discussions, type: DISCUSSION, first: $first) @include(if: $search) {
    nodes { ... on Discussion { number title closed url } }
  }
}"""

CREATE_DISCUSSION = """mutation CreateDiscussion($input: CreateDiscussionInput!) {
  createDiscussion(input: $input) { discussion { id number url } }
}"""

ADD_DISCUSSION_COMMENT = """mutation AddDiscussionComment($input: AddDiscussionCommentInput!) {
  addDiscussionComment(input: $input) { comment { id url } }
}"""

MARKER_QUERY = """query FeedbackMarker($issues: String!, $discussions: String!) {
  issues: search(query: $issues, type: ISSUE, first: 5) {
    nodes { ... on Issue { id number url body comments(last: 50) { nodes { id url body } } } }
  }
  discussions: search(query: $discussions, type: DISCUSSION, first: 5) {
    nodes { ... on Discussion { id number url body comments(last: 50) { nodes { id url body } } } }
  }
}"""

RECENT_DISCUSSIONS = """query RecentDiscussions($owner: String!, $name: String!, $number: Int!, $comments: Boolean!) {
  repository(owner: $owner, name: $name) {
    discussions(first: 30, orderBy: {field: CREATED_AT, direction: DESC}) @skip(if: $comments) {
      nodes { id number url body author { login } }
    }
    discussion(number: $number) @include(if: $comments) {
      comments(last: 50) { nodes { id url body author { login } } }
    }
  }
}"""
