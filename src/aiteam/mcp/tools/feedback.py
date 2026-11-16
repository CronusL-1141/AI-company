"""Feedback to the OS maintainers on GitHub: draft, publish after the user's confirmation, list.

Publishing is confirmed by the MCP client's own dialog (elicitation), which shows the
user where the text goes, its title, its hash, a link to a read-only full-text page
(a client folds a long message) and the text; an agent cannot answer it. The dialog
has no fields: the client's own Accept publishes, Decline publishes nothing and
keeps the draft. Where there is no dialog (the client does not offer
one, nobody answers it in five minutes, it is cancelled) nothing is sent either: the
user gets a prefilled GitHub page and submits it there. The draft text is fetched
from the API here, never taken from the agent.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from functools import partial
from typing import Any

import anyio
from fastmcp import Context
from mcp import types as mcp_types

from aiteam.mcp._base import _api_call, _get_api_url, caller_host

# The dialog waits this long for the user; then the draft goes to the web instead.
ELICIT_TIMEOUT_SECONDS = 300.0
# One call waits this long for the user to authorize OS's GitHub App, then answers
# "authorizing" (the next feedback_submit goes on waiting without asking again). Kept
# under two minutes: a client may move a longer tool call to the background.
AUTH_WAIT_SECONDS = 90.0
_sleep = anyio.sleep  # the wait between polls (tests replace it)
# A longer body is shown by its head in the dialog (the full text is in the chat preview, same hash).
MAX_DIALOG_BODY_CHARS = 8000
_CLIENT_LABELS = {"claude-code": "Claude Code", "codex": "Codex", "codex-mcp-client": "Codex"}

# The dialog asks nothing but Accept or Decline: no field, so nothing to misread
# (a checkbox was tried first; Claude Code's "valid field" tick read as "checked").
# Sent as the empty form schema directly: FastMCP's ctx.elicit(response_type=None)
# sends the same schema but is deprecated there.
EMPTY_FORM: dict[str, Any] = {"type": "object", "properties": {}}
LAST_LINE = "Accept = publish now / 立即公开发布; Decline = don't publish / 不发"


def _client(ctx: Context) -> tuple[str, str]:
    """The MCP client's own name and version, from its handshake."""
    params = getattr(ctx.session, "client_params", None)
    info = getattr(params, "clientInfo", None)
    if info is None:
        return "", ""
    name = str(getattr(info, "name", "") or "")
    label = _CLIENT_LABELS.get(name.lower()) or str(getattr(info, "title", "") or name)
    return label[:64], str(getattr(info, "version", "") or "")[:64]


def _offers_dialog(ctx: Context) -> bool:
    try:
        return ctx.session.check_client_capability(
            mcp_types.ClientCapabilities(elicitation=mcp_types.ElicitationCapability()))
    except Exception:  # noqa: BLE001 - no session, no dialog
        return False


def _offers_url(ctx: Context) -> bool:
    """Whether the client declared URL-mode dialogs (it shows a link and opens it on consent)."""
    params = getattr(ctx.session, "client_params", None)
    elicitation = getattr(getattr(params, "capabilities", None), "elicitation", None)
    return getattr(elicitation, "url", None) is not None


def dialog_text(draft: dict[str, Any], destination: str, view_url: str) -> str:
    """The full-text page, where and hash, title: the three lines Claude Code 2.1 shows
    before it folds the message (no key unfolds it); then the text and what the buttons do."""
    body = draft.get("body") or ""
    if len(body) > MAX_DIALOG_BODY_CHARS:
        body = (body[:MAX_DIALOG_BODY_CHARS] + f"\n\n... ({len(body) - MAX_DIALOG_BODY_CHARS} more characters: "
                "see the full-text page above, with the same content hash)")
    lines = [f"Full text / 全文: {view_url}",
             f"To / 发往: {destination} | Hash / 哈希: {draft['content_hash'][:12]}"]
    if draft.get("title"):
        lines.append(f"Title / 标题: {draft['title']}")
    if draft.get("gh_login"):
        lines.append(f"Public, as @{draft['gh_login']} / 以 @{draft['gh_login']} 公开发布")
    return "\n".join([*lines, "", body, "", LAST_LINE])


def view_url(draft_id: str) -> str:
    """The read-only full-text page of a draft, on the API this MCP server talks to."""
    return _get_api_url() + _path(draft_id, "/view")


async def _ask(ctx: Context, message: str) -> mcp_types.ElicitResult:
    """Show the dialog; the client's answer as it came (action, and content when accepted)."""
    return await ctx.session.elicit_form(message=message, requestedSchema=EMPTY_FORM,
                                         related_request_id=ctx.request_id)


def authorization_text(start: dict[str, Any]) -> str:
    """The code first (a client may fold all but the first lines), then where and what it grants."""
    return "\n".join([
        # Claude Code shows the message as plain text (no bold, no colour): wide brackets
        # and spaces make the code stand out, and leave it copyable as typed.
        f"GitHub code / 授权码:  【 {start['user_code']} 】",
        f"Open / 打开: {start['verification_uri']}",
        'Grants "AI Team OS Feedback": issues and discussions of the feedback repository only / '
        "只授权反馈仓库的 issue 与讨论",
        "",
        "Enter the code on that page and approve; the feedback is then sent. / 在该页输入授权码并同意，之后自动发出。",
        "Accept = authorize now / 去授权; Decline = don't send / 不发",
    ])


def _not_sent(draft_id: str, outcome: str, message: str) -> dict[str, Any]:
    return {"success": False, "status": "draft", "outcome": outcome, "draft_id": draft_id,
            "message": f"{message}，什么都没有发出；草稿保留到过期。"}


def _path(draft_id: str, tail: str = "") -> str:
    return f"/api/feedback/drafts/{urllib.parse.quote(draft_id, safe='')}{tail}"


async def _post(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(_api_call, "POST", path, body)


async def _dialog(ask) -> tuple[str, str]:
    """Show a dialog: (outcome, reason) with outcome accept, decline or web (cancelled, unanswered, impossible)."""
    reply = None
    try:
        # anyio's own deadline: the session underneath is anyio, and an asyncio
        # cancellation from outside its scopes can leave the request waiting forever.
        with anyio.move_on_after(ELICIT_TIMEOUT_SECONDS):
            reply = await ask()
    except Exception as exc:  # noqa: BLE001 - a client that cannot show it: the web is the way
        return "web", f"确认弹框不可用：{type(exc).__name__}"
    if reply is None:
        return "web", "确认弹框 5 分钟无人应答"
    if reply.action == "cancel":
        return "web", "确认弹框被取消（可能无人在场）"
    return ("accept", "") if reply.action == "accept" else ("decline", "")


async def _authorize(ctx: Context, draft_id: str) -> dict[str, Any] | None:
    """Have the user authorize OS's GitHub App for a confirmed draft; None once authorized.

    Asks once per flow: a later call finds the flow open and only waits. URL-mode dialogs
    (the client opens github.com/login/device on consent) where the client offers them,
    else a dialog with the code and the link.
    """
    start = await _post("/api/feedback/github/device", {"draft_id": draft_id})
    if not start.get("success"):
        return start
    flow_id = start["flow_id"]
    url_mode = False
    if not start["existing"]:
        url_mode = _offers_url(ctx)
        message = authorization_text(start)
        if url_mode:
            ask = partial(ctx.session.elicit_url, message=message, url=start["verification_uri"],
                          elicitation_id=flow_id, related_request_id=ctx.request_id)
        else:
            ask = partial(_ask, ctx, message)
        outcome, reason = await _dialog(ask)
        if outcome == "web":
            return await _hand_off(draft_id, reason)
        if outcome == "decline":
            return _not_sent(draft_id, "auth_declined", "用户拒绝了 GitHub 授权")
    waited, interval = 0.0, float(start["interval"])
    while True:
        poll = await _post(f"/api/feedback/github/device/{urllib.parse.quote(flow_id, safe='')}/poll")
        status = poll.get("status")
        if status == "ok":
            if url_mode:
                await ctx.session.send_elicit_complete(flow_id, related_request_id=ctx.request_id)
            return None
        if status == "denied":
            return _not_sent(draft_id, "auth_denied", "用户在 GitHub 上拒绝了授权")
        if status != "pending":
            return await _hand_off(draft_id, f"GitHub 授权没有完成：{poll.get('message') or poll.get('error')}")
        interval = float(poll.get("interval") or interval)
        if waited + interval > AUTH_WAIT_SECONDS:
            return {"success": False, "status": "authorizing", "draft_id": draft_id,
                    "user_code": start["user_code"], "verification_uri": start["verification_uri"],
                    "message": "用户还没在 GitHub 上完成授权。用户说已完成后，再调一次 feedback_submit(draft_id)："
                               "不会再问是否发布，也不会换授权码。"}
        await ctx.report_progress(waited, AUTH_WAIT_SECONDS, "Waiting for GitHub authorization / 等待 GitHub 授权")
        await _sleep(interval)
        waited += interval


async def _hand_off(draft_id: str, reason: str) -> dict[str, Any]:
    answer = await asyncio.to_thread(_api_call, "POST", _path(draft_id, "/handoff"), {"reason": reason})
    if answer.get("success"):
        answer["message"] = ("没有直接发出。把链接给用户：用户在 GitHub 页面上检查内容并亲手点提交，"
                             "这一下就是确认。")
    return answer


def register(mcp):
    """Register the feedback tools."""

    @mcp.tool()
    async def feedback_draft(
        kind: str,
        ctx: Context,
        title: str = "",
        body: str = "",
        steps: str = "",
        proposal: str = "",
        alternatives: str = "",
        original: str = "",
        original_language: str = "",
        comment_on: str = "",
        author: str = "",
    ) -> dict[str, Any]:
        """Draft feedback to the AI Team OS maintainers: a bug as a GitHub Issue, an idea in Discussions (Ideas).

        Only for AI Team OS itself (its hooks, MCP tools, API, Dashboard, installer),
        and only after the user said they want to send feedback; feedback about Claude
        Code, Codex or a model goes to their own channels. You may offer it when you
        hit an OS error. Decide the kind with the user: "broken" (expected one thing,
        got another) is bug; "I want" is idea; if a remark could be either, ask. A
        remark with both: two drafts. Security problems: kind="security" (returns the
        private reporting link, files nothing). Usage questions: kind="question".
        Before drafting, search the direction memories for private-term rules and keep
        such terms out. Write title and text in English; put the user's own words, when
        not English, in original. Paths, user and host names, e-mails and URL
        credentials are rewritten; a suspected secret or a private term refuses the draft.

        Then you MUST: show the user the returned title and body verbatim (no summary,
        no rewording) with the content hash; list the similar posts and let the user
        choose: file new (feedback_submit), add to one of them (draft again with
        comment_on), or not send. Any change means a new draft.

        Args:
            kind: "bug", "idea", "security" or "question"
            ctx: The MCP context (injected; carries the client's name and version)
            title: English title of a new Issue or Discussion (not for comments)
            body: What happened (bug), the problem (idea), or the comment text
            steps: Steps to reproduce (bug only, required for a new bug)
            proposal: How the user imagines it working (idea only)
            alternatives: Workarounds tried or tools that almost do it (idea only)
            original: The user's own words when not in English (shown folded)
            original_language: Language code of original, e.g. "zh"
            comment_on: Empty for a new post; "issue:12" or "discussion:5" to comment on one
            author: Your agent name (sub-agents: the OS name given at start)
        """
        label, version = _client(ctx)
        payload = {"kind": kind, "title": title, "body": body, "steps": steps, "proposal": proposal,
                   "alternatives": alternatives, "original": original, "original_language": original_language,
                   "comment_on": comment_on, "author": author, "host": caller_host(), "host_label": label,
                   "host_version": version, "dialog": _offers_dialog(ctx)}
        return await asyncio.to_thread(_api_call, "POST", "/api/feedback/drafts", payload)

    @mcp.tool()
    async def feedback_submit(draft_id: str, ctx: Context) -> dict[str, Any]:
        """Publish a draft from feedback_draft, once the user confirms it in the client's own dialog.

        The dialog shows where it goes, the title, the content hash and the stored text;
        the user's Accept there publishes at once, Decline sends nothing and keeps the
        draft. When neither the user's gh nor an earlier authorization can publish, a
        second dialog has the user authorize OS's GitHub App on github.com (a code to
        enter); the post then goes out. "authorizing": the user has not finished yet;
        call again when they say so (no new question, same code). Without a dialog (not
        supported, cancelled, unanswered for five minutes) nothing is sent and you get a
        prefilled GitHub link instead: give it to the user, who submits on GitHub.
        After publishing, tell the user the URL.

        Args:
            draft_id: The draft_id returned by feedback_draft
            ctx: The MCP context (injected; shows the confirmation dialog)
        """
        answer = await asyncio.to_thread(_api_call, "GET", _path(draft_id))
        if not answer.get("success"):
            return answer
        draft = answer["data"]
        if draft["status"] != "draft":
            return {"success": draft["status"] == "sent", "status": draft["status"], "draft_id": draft_id,
                    "remote_url": draft.get("remote_url", ""), "error": draft.get("error", "")}
        if draft["route"] == "web":
            return await _hand_off(draft_id, draft.get("error") or "gh 不可用")
        if not _offers_dialog(ctx):
            return await _hand_off(draft_id, "客户端不支持确认弹框（MCP elicitation）")
        confirmation = {"confirm_method": "elicitation", "action": "accept", "content_hash": draft["content_hash"]}
        awaiting_auth = draft["route"] == "auth"
        # A draft waiting for authorization that the user already confirmed is not asked again.
        if not (awaiting_auth and draft.get("confirm_method") == "elicitation"):
            message = dialog_text(draft, answer.get("destination", ""), view_url(draft_id))
            outcome, reason = await _dialog(partial(_ask, ctx, message))
            if outcome == "web":
                return await _hand_off(draft_id, reason)
            if outcome != "accept":
                return _not_sent(draft_id, "declined", "用户在弹框里拒绝了")
            if awaiting_auth:
                confirmed = await _post(_path(draft_id, "/confirm"), confirmation)
                if not confirmed.get("success"):
                    return confirmed
        if awaiting_auth:
            pending = await _authorize(ctx, draft_id)
            if pending is not None:
                return pending
        result = await _post(_path(draft_id, "/submit"), confirmation)
        if result.get("fallback") == "web":
            return {**result, "web": await _hand_off(draft_id, f"发送失败（{result.get('error_kind')}）")}
        return result

    @mcp.tool()
    def feedback_account(action: str = "status") -> dict[str, Any]:
        """See which way feedback goes to GitHub and as whom, or sign OS's own GitHub authorization out.

        "status": gh (the user's gh, logged in), app (OS's GitHub App authorization, made in
        a feedback_submit dialog when gh could not publish) or none, with the account and
        when the App authorization runs out. Never shows a token. "sign_out": deletes this
        machine's App authorization; to revoke it on GitHub as well, give the user the
        returned revoke_page.

        Args:
            action: "status" (default) or "sign_out"
        """
        if action == "sign_out":
            return _api_call("DELETE", "/api/feedback/github/account")
        if action != "status":
            return {"success": False, "error": 'action 只能是 "status" 或 "sign_out"'}
        return _api_call("GET", "/api/feedback/github/account")

    @mcp.tool()
    def feedback_list(status: str = "", limit: int = 20) -> dict[str, Any]:
        """List the feedback filed from OS, newest first: kind, title, status, GitHub number and URL.

        Summary rows only (no text). Reading also settles open rows: an expired draft,
        or a send whose result was unknown (checked against GitHub, never resent).

        Args:
            status: Only this status (draft, sending, sent, unknown, failed, handed_off, refused, expired); empty: all
            limit: How many rows, 1 to 100 (default 20)
        """
        query = urllib.parse.urlencode({key: value for key, value in (("status", status), ("limit", limit)) if value})
        return _api_call("GET", f"/api/feedback?{query}")
