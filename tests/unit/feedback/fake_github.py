"""A fake GitHub held in a JSON file, served two ways: as ``gh`` (fake_gh.py) and over HTTP (HttpGitHub).

Not looser than gh and GitHub, so a request production gets wrong fails here too:

* bodies are checked as GitHub checks them: an issue title of at most 256 characters,
  a body of at most 65,536, ids the repository really has; failures come back in the
  shapes recorded from real gh calls (stdout JSON, ``gh: <message> (HTTP <code>)``
  on stderr, exit 1; exit 4 when not logged in) or, over HTTP, as GitHub answers them
  (the status with the same JSON; a GraphQL error is HTTP 200 with ``errors``);
* labels sent by a person without push access are dropped silently, as GitHub does;
* issues and discussions share one number sequence, as on GitHub;
* issue search must say ``is:issue`` or ``is:pull-request`` (REST answers 422 without it,
  as the live App token showed);
* over HTTP: the GitHub App's device flow and refresh as documented (client id checked,
  ``authorization_pending``, ``slow_down`` when polled before the interval,
  ``access_denied``, ``expired_token``; refresh tokens work once, a refresh retires the
  old access token, tokens run out after 8 hours of the test clock); an App token
  reaches only the repositories the App is installed on.

The world (FAKE_GH_STATE) also says where to hang (``hang``: an operation and ``before``
or ``after`` doing it). gh hangs for real (with ``signal`` set to a FIFO, "<pid> <op>"
is written there first); over HTTP the request times out instead. Every call is
appended to FAKE_GH_LOG with its operation (gh calls also with argv and stdin; no
token is ever logged).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import urllib.parse
from contextlib import contextmanager
from datetime import UTC, datetime

REQUIRED_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}
MAX_TITLE = 256
MAX_BODY = 65_536
NOT_LOGGED_IN = ("To get started with GitHub CLI, please run:  gh auth login\n"
                 "Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token.\n")


class Exit(Exception):  # noqa: N818
    """A failed call: gh's exit code, stdout and stderr, and the HTTP status GitHub answered with."""

    def __init__(self, code: int, stdout: str = "", stderr: str = "", status: int = 400) -> None:
        super().__init__(code)
        self.code, self.stdout, self.stderr, self.status = code, stdout, stderr, status


def http_error(status: int, message: str, doc: str = "https://docs.github.com/rest") -> Exit:
    body = json.dumps({"message": message, "documentation_url": doc, "status": str(status)})
    return Exit(1, body, f"gh: {message} (HTTP {status})\n", status)


def graphql_error(kind: str, message: str, data: dict | None = None) -> Exit:
    body = json.dumps({"data": data, "errors": [{"type": kind, "message": message}]})
    return Exit(1, body, f"gh: {message}\n", 200)


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def locked(path: str):
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class World:
    """The fake GitHub as one caller sees it: who acts (gh's login, or the App token's user)
    and which repositories are in reach (all for gh; the App's installations for its token)."""

    def __init__(self, state: dict, actor: str | None = None, scope: frozenset[str] | None = None) -> None:
        self.state = state
        self.actor = actor if actor is not None else state.get("login")
        self.scope = scope

    @property
    def login(self) -> str | None:
        return self.actor

    def reachable(self, full_name: str) -> bool:
        return full_name in self.state["repos"] and (self.scope is None or full_name in self.scope)

    def repo(self, full_name: str) -> dict:
        if not self.reachable(full_name):
            raise http_error(404, "Not Found")
        return self.state["repos"][full_name]

    def next_number(self, repo: dict) -> int:
        repo["next_number"] = repo.get("next_number", 1) + 1
        return repo["next_number"] - 1

    # -- rules every call meets ------------------------------------------------

    def gate(self, write: bool) -> None:
        if not self.login:
            raise Exit(4, "", NOT_LOGGED_IN)
        token = self.state.get("token", "ok")
        if token == "invalid":
            raise http_error(401, "Bad credentials")
        if token == "forbidden" and write:
            raise http_error(403, "Resource not accessible by personal access token")
        if self.state.get("secondary_rate_limit") and write:
            raise http_error(403, "You have exceeded a secondary rate limit and have been temporarily blocked "
                                  "from content creation.")


def check_text(title: str | None, body: str | None, *, need_title: bool) -> None:
    if need_title and (not isinstance(title, str) or not title.strip()):
        raise http_error(422, "Validation Failed")
    if need_title and len(title) > MAX_TITLE:
        raise http_error(422, f"title is too long (maximum is {MAX_TITLE} characters)")
    if not isinstance(body, str) or not body.strip():
        raise http_error(422, "Validation Failed")
    if len(body) > MAX_BODY:
        raise http_error(422, f"body is too long (maximum is {MAX_BODY} characters)")


def words(query: str) -> list[str]:
    return [w.strip('"').lower() for w in query.split() if ":" not in w and w.strip('"') and w != "OR"]


def matches(query: str, *texts: str) -> bool:
    """GitHub's search: bare terms are ANDed, terms joined by OR are alternatives."""
    haystack = " ".join(t or "" for t in texts).lower()
    terms = words(query)
    combine = any if " OR " in query else all
    return bool(terms) and combine(term in haystack for term in terms)


# ----------------------------------------------------------------------------
# REST
# ----------------------------------------------------------------------------

ISSUES = re.compile(r"^repos/([^/]+/[^/]+)/issues$")
ISSUE_COMMENTS = re.compile(r"^repos/([^/]+/[^/]+)/issues/(\d+)/comments$")


def require_issue_kind(query: str) -> None:
    if "is:issue" not in query and "is:pull-request" not in query:
        raise http_error(422, "Query must include 'is:issue' or 'is:pull-request'")


def rest(world: World, method: str, endpoint: str, payload: dict | None) -> object:
    path, _, query = endpoint.partition("?")
    params = dict(urllib.parse.parse_qsl(query))
    if path == "user" and method == "GET":
        world.gate(False)
        return {"login": world.login, "id": 1, "type": "User"}
    if path == "search/issues" and method == "GET":
        world.gate(False)
        require_issue_kind(params.get("q", ""))
        full = next((w.split(":", 1)[1] for w in params.get("q", "").split() if w.startswith("repo:")), "")
        items = [i for i in world.repo(full)["issues"] if matches(params["q"], i["title"], i["body"])] if full else []
        return {"total_count": len(items), "incomplete_results": False, "items": items}
    if (m := ISSUES.match(path)) and method == "POST":
        world.gate(True)
        return create_issue(world, world.repo(m.group(1)), m.group(1), payload)
    if (m := ISSUES.match(path)) and method == "GET":
        world.gate(False)
        if set(params) - {"creator", "since", "state", "per_page"}:
            raise http_error(422, "Validation Failed")
        repo = world.repo(m.group(1))
        return [i for i in reversed(repo["issues"]) if i["user"]["login"] == params.get("creator", i["user"]["login"])
                and i["updated_at"] >= params.get("since", "")][: int(params.get("per_page", 30))]
    if (m := ISSUE_COMMENTS.match(path)):
        repo = world.repo(m.group(1))
        issue = next((i for i in repo["issues"] if i["number"] == int(m.group(2))), None)
        world.gate(method == "POST")
        if issue is None:
            raise http_error(404, "Not Found")
        if method == "GET":
            if set(params) - {"since", "per_page"}:
                raise http_error(422, "Validation Failed")
            return [c for c in issue["comments"] if c["updated_at"] >= params.get("since", "")]
        if set(payload or {}) != {"body"}:
            raise http_error(422, "Validation Failed")
        check_text(None, payload["body"], need_title=False)
        repo["comment_seq"] = repo.get("comment_seq", 1000) + 1
        comment = {"id": repo["comment_seq"], "node_id": f"IC_{repo['comment_seq']}", "body": payload["body"],
                   "html_url": f"{issue['html_url']}#issuecomment-{repo['comment_seq']}",
                   "user": {"login": world.login}, "created_at": now(), "updated_at": now()}
        issue["comments"].append(comment)
        return comment
    raise http_error(404, "Not Found")


def create_issue(world: World, repo: dict, full_name: str, payload: dict | None) -> dict:
    if not isinstance(payload, dict) or set(payload) - {"title", "body", "labels"}:
        raise http_error(422, "Validation Failed")
    check_text(payload.get("title"), payload.get("body"), need_title=True)
    labels = payload.get("labels") or []
    if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
        raise http_error(422, "Validation Failed")
    # GitHub drops the labels of someone without push access, silently.
    kept = [label for label in labels if label in repo["labels"]] if world.state.get("collaborator") else []
    number = world.next_number(repo)
    issue = {"number": number, "node_id": f"I_{number}", "title": payload["title"], "body": payload["body"],
             "html_url": f"https://github.com/{full_name}/issues/{number}", "state": "open",
             "labels": [{"name": label} for label in kept], "user": {"login": world.login},
             "created_at": now(), "updated_at": now(), "comments": []}
    repo["issues"].append(issue)
    return {key: value for key, value in issue.items() if key != "comments"}


# ----------------------------------------------------------------------------
# GraphQL
# ----------------------------------------------------------------------------

OPERATIONS = {
    "FeedbackLookup": {"owner": str, "name": str, "issues": str, "discussions": str, "search": bool, "first": int,
                       "number": int, "issue": bool, "discussion": bool},
    "FeedbackMarker": {"issues": str, "discussions": str},
    "RecentDiscussions": {"owner": str, "name": str, "number": int, "comments": bool},
    "CreateDiscussion": {"input": dict},
    "AddDiscussionComment": {"input": dict},
}


def graphql(world: World, payload: dict | None) -> object:
    if not isinstance(payload, dict) or set(payload) != {"query", "variables"}:
        raise http_error(400, "Problems parsing JSON")
    match = re.match(r"\s*(query|mutation)\s+(\w+)", payload["query"])
    name = match.group(2) if match else ""
    expected = OPERATIONS.get(name)
    variables = payload["variables"]
    if expected is None or set(variables) != set(expected) or not all(
            type(variables[key]) is kind for key, kind in expected.items()):
        raise graphql_error("INVALID", f"stub: unexpected operation or variables {name} {sorted(variables)}")
    world.gate(match.group(1) == "mutation")
    return {"data": globals()["op_" + name](world, variables)}


def repo_by_owner(world: World, owner: str, name: str) -> dict:
    full = f"{owner}/{name}"
    if not world.reachable(full):
        raise graphql_error("NOT_FOUND", f"Could not resolve to a Repository with the name '{full}'.",
                            {"repository": None})
    return world.state["repos"][full]


def discussion_node(d: dict, with_comments: bool = False) -> dict:
    node = {"id": d["id"], "number": d["number"], "title": d["title"], "url": d["url"], "closed": d["closed"],
            "body": d["body"], "author": {"login": d["author"]}}
    if with_comments:
        node["comments"] = {"nodes": [{"id": c["id"], "url": c["url"], "body": c["body"],
                                       "author": {"login": c["author"]}} for c in d["comments"]]}
    return node


def op_FeedbackLookup(world: World, v: dict) -> dict:  # noqa: N802
    repo = repo_by_owner(world, v["owner"], v["name"])
    repository = {"id": repo["id"], "discussionCategories": {"nodes": repo["categories"]}}
    if v["issue"]:
        issue = next((i for i in repo["issues"] if i["number"] == v["number"]), None)
        if issue is None:
            raise graphql_error("NOT_FOUND", f"Could not resolve to an Issue with the number of {v['number']}.",
                                {"repository": {**repository, "issue": None}})
        repository["issue"] = {"id": issue["node_id"], "number": issue["number"], "title": issue["title"],
                               "url": issue["html_url"], "state": issue["state"].upper()}
    if v["discussion"]:
        found = next((d for d in repo["discussions"] if d["number"] == v["number"]), None)
        if found is None:
            raise graphql_error("NOT_FOUND", f"Could not resolve to a Discussion with the number of {v['number']}.",
                                {"repository": {**repository, "discussion": None}})
        repository["discussion"] = {k: discussion_node(found)[k] for k in ("id", "number", "title", "url", "closed")}
    data: dict = {"repository": repository}
    if v["search"]:
        require_graphql_issue_kind(v["issues"])
        if world.state.get("search_down"):
            raise graphql_error("SERVICE_UNAVAILABLE", "Something went wrong while executing your query.")
        data["issues"] = {"nodes": [{"number": i["number"], "title": i["title"], "state": i["state"].upper(),
                                     "url": i["html_url"]} for i in repo["issues"]
                                    if matches(v["issues"], i["title"], i["body"])][: v["first"]]}
        data["discussions"] = {"nodes": [{k: discussion_node(d)[k] for k in ("number", "title", "closed", "url")}
                                         for d in repo["discussions"]
                                         if matches(v["discussions"], d["title"], d["body"])][: v["first"]]}
    return data


def require_graphql_issue_kind(query: str) -> None:
    try:
        require_issue_kind(query)
    except Exit:
        raise graphql_error("INVALID", "Query must include 'is:issue' or 'is:pull-request'") from None


def op_FeedbackMarker(world: World, v: dict) -> dict:  # noqa: N802
    require_graphql_issue_kind(v["issues"])
    full = next(w.split(":", 1)[1] for w in v["issues"].split() if w.startswith("repo:"))
    if not world.reachable(full):
        return {"issues": {"nodes": []}, "discussions": {"nodes": []}}
    repo = world.state["repos"][full]
    if not world.state.get("search_indexed", True):
        return {"issues": {"nodes": []}, "discussions": {"nodes": []}}
    issues = [i for i in repo["issues"] if matches(v["issues"], i["body"], *[c["body"] for c in i["comments"]])]
    discussions = [d for d in repo["discussions"]
                   if matches(v["discussions"], d["body"], *[c["body"] for c in d["comments"]])]
    return {
        "issues": {"nodes": [{"id": i["node_id"], "number": i["number"], "url": i["html_url"], "body": i["body"],
                              "comments": {"nodes": [{"id": c["node_id"], "url": c["html_url"], "body": c["body"]}
                                                     for c in i["comments"]]}} for i in issues[:5]]},
        "discussions": {"nodes": [discussion_node(d, True) for d in discussions[:5]]},
    }


def op_RecentDiscussions(world: World, v: dict) -> dict:  # noqa: N802
    repo = repo_by_owner(world, v["owner"], v["name"])
    if v["comments"]:
        found = next((d for d in repo["discussions"] if d["number"] == v["number"]), None)
        if found is None:
            raise graphql_error("NOT_FOUND", "Could not resolve to a Discussion.", {"repository": {"discussion": None}})
        return {"repository": {"discussion": {"comments": discussion_node(found, True)["comments"]}}}
    nodes = [discussion_node(d) for d in reversed(repo["discussions"])][:30]
    return {"repository": {"discussions": {"nodes": nodes}}}


def op_CreateDiscussion(world: World, v: dict) -> dict:  # noqa: N802
    data = v["input"]
    if set(data) != {"repositoryId", "categoryId", "title", "body"}:
        raise graphql_error("INVALID", "stub: unexpected input fields")
    full, repo = next(((n, r) for n, r in world.state["repos"].items()
                       if r["id"] == data["repositoryId"] and world.reachable(n)), (None, None))
    if repo is None:
        raise graphql_error("NOT_FOUND", f"Could not resolve to a node with the global id of '{data['repositoryId']}'")
    if data["categoryId"] not in {c["id"] for c in repo["categories"]}:
        raise graphql_error("NOT_FOUND", f"Could not resolve to a node with the global id of '{data['categoryId']}'")
    check_graphql_text(data["title"], data["body"])
    number = world.next_number(repo)
    discussion = {"id": f"D_{number}", "number": number, "title": data["title"], "body": data["body"],
                  "url": f"https://github.com/{full}/discussions/{number}", "closed": False,
                  "category_id": data["categoryId"], "author": world.login, "comments": []}
    repo["discussions"].append(discussion)
    return {"createDiscussion": {"discussion": {"id": discussion["id"], "number": number, "url": discussion["url"]}}}


def op_AddDiscussionComment(world: World, v: dict) -> dict:  # noqa: N802
    data = v["input"]
    if set(data) != {"discussionId", "body"}:
        raise graphql_error("INVALID", "stub: unexpected input fields")
    found = next((d for n, r in world.state["repos"].items() if world.reachable(n) for d in r["discussions"]
                  if d["id"] == data["discussionId"]), None)
    if found is None:
        raise graphql_error("NOT_FOUND", f"Could not resolve to a node with the global id of '{data['discussionId']}'")
    check_graphql_text(None, data["body"])
    comment_id = f"DC_{found['number']}_{len(found['comments']) + 1}"
    comment = {"id": comment_id, "url": f"{found['url']}#discussioncomment-{len(found['comments']) + 1}",
               "body": data["body"], "author": world.login}
    found["comments"].append(comment)
    return {"addDiscussionComment": {"comment": {"id": comment_id, "url": comment["url"]}}}


def check_graphql_text(title: str | None, body: str) -> None:
    try:
        check_text(title, body, need_title=title is not None)
    except Exit as exc:
        raise graphql_error("UNPROCESSABLE", json.loads(exc.stdout)["message"]) from None


# ----------------------------------------------------------------------------



def operation(method: str, endpoint: str, payload: dict | None) -> str:
    """A short name for the call (what the world's ``hang`` and the tests refer to)."""
    if endpoint == "graphql":
        match = re.match(r"\s*(?:query|mutation)\s+(\w+)", (payload or {}).get("query", ""))
        return match.group(1) if match else "graphql"
    path = endpoint.partition("?")[0]
    if ISSUES.match(path):
        return "create_issue" if method == "POST" else "list_issues"
    if ISSUE_COMMENTS.match(path):
        return "comment_issue" if method == "POST" else "list_issue_comments"
    return path


def hang(state: dict, op: str, when: str) -> None:
    if state.get("hang", {}).get(op) != when:
        return
    fifo = state.get("signal")
    if fifo:
        with open(fifo, "w") as pipe:
            pipe.write(f"{os.getpid()} {op}\n")
    threading.Event().wait()  # until the caller kills this process


def log(path: str, entry: dict) -> None:
    with locked(path):
        with open(path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")


def read_state(path: str) -> dict:
    with locked(path):
        with open(path) as fh:
            return json.load(fh)


def call(state_path: str, world_of, op: str, run) -> object:
    """Run one call on the world in the file, writing it back whatever happens (hang "before" is the caller's)."""
    with locked(state_path):
        with open(state_path) as fh:
            state = json.load(fh)
        try:
            return run(world_of(state))
        finally:
            with open(state_path, "w") as fh:
                json.dump(state, fh)
