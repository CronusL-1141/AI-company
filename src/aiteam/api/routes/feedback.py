"""Feedback to the OS maintainers on GitHub (aiteam.feedback): draft, read, publish, hand off, list.

``/drafts/{id}/view`` is the one HTML page: the draft's full text, read only (GET,
no side effect, no script or form), linked from the confirmation dialog because
clients fold a long dialog message.

``/github/*`` is OS's own GitHub authorization (a GitHub App user token from the
device flow), for when the user's gh cannot publish: this machine's credential, so
those routes are local. No answer ever carries the token or the device code.

Publishing takes one kind of confirmation only, the MCP client's own dialog
(FeedbackSubmitRequest): there is no field for an agent to say the user agreed.

Every route serves the session face: the gh login is on this machine, so drafts are
written, published and reconciled here, and the rows go up to the center afterwards
like any session row. Filing feedback never waits for the center.
"""

from __future__ import annotations

from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from aiteam.api import cc_config_scope
from aiteam.api.deps import get_feedback_service
from aiteam.feedback import forms
from aiteam.feedback.service import Caller, FeedbackError, FeedbackService
from aiteam.sync.surface import route_surface
from aiteam.types import (
    FeedbackDeviceRequest,
    FeedbackDraftRequest,
    FeedbackHandoffRequest,
    FeedbackStatus,
    FeedbackSubmitRequest,
    RouteSurface,
)

router = APIRouter(prefix="/api/feedback", tags=["feedback"])
ROUTE_SURFACE = RouteSurface.session()


def _caller(request: Request) -> Caller:
    headers = request.headers
    return Caller(session_id=headers.get(cc_config_scope.SESSION_HEADER, ""),
                  mcp_session_id=headers.get("X-Aiteam-Mcp-Session-Id", ""),
                  project_id=headers.get("X-Project-Id", ""), project_dir=unquote(headers.get("X-Project-Dir", "")))


async def _answer(call) -> dict:
    try:
        return await call
    except FeedbackError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


@router.post("/drafts")
async def create_draft(body: FeedbackDraftRequest, request: Request,
                       service: FeedbackService = Depends(get_feedback_service)) -> dict:
    """Check, clean and freeze a draft; answers the exact text that would be published."""
    return await _answer(service.draft(body, _caller(request)))


@router.get("/drafts/{draft_id}")
async def read_draft(draft_id: str, service: FeedbackService = Depends(get_feedback_service)) -> dict:
    """A draft as stored: the text, its hash, where it goes and where it stands."""
    row = await _answer(service.get(draft_id))
    return {"success": True, "data": row.model_dump(mode="json"),
            "destination": forms.destination(row.kind, row.mode, row.target_ref, row.target_repo)}


@router.get("/drafts/{draft_id}/view", response_class=HTMLResponse)
async def view_draft(draft_id: str, service: FeedbackService = Depends(get_feedback_service)) -> HTMLResponse:
    """The draft's full text as a read-only HTML page (the confirmation dialog links to it)."""
    row = await _answer(service.view(draft_id))
    page = forms.view_page(destination=forms.destination(row.kind, row.mode, row.target_ref, row.target_repo),
                           title=row.title, content_hash=row.content_hash, status=str(row.status), body=row.body)
    return HTMLResponse(page, headers=forms.VIEW_HEADERS)


@router.post("/drafts/{draft_id}/confirm")
async def confirm_draft(draft_id: str, body: FeedbackSubmitRequest,
                        service: FeedbackService = Depends(get_feedback_service)) -> dict:
    """Record the dialog's confirmation of a draft that waits for GitHub authorization."""
    return await _answer(service.confirm(draft_id, body))


@router.post("/github/device")
@route_surface(RouteSurface.local())
async def start_authorization(body: FeedbackDeviceRequest,
                              service: FeedbackService = Depends(get_feedback_service)) -> dict:
    """The code for the user to authorize OS's GitHub App (the draft's open flow, or a new one)."""
    start = await _answer(service.start_authorization(body.draft_id))
    return {"success": True, "flow_id": start.flow_id, "user_code": start.user_code,
            "verification_uri": start.verification_uri, "expires_in": start.expires_in,
            "interval": start.interval, "existing": start.existing}


@router.post("/github/device/{flow_id}/poll")
@route_surface(RouteSurface.local())
async def poll_authorization(flow_id: str, service: FeedbackService = Depends(get_feedback_service)) -> dict:
    """Ask GitHub once whether the user has authorized: pending, ok, denied, expired or failed."""
    result = await _answer(service.poll_authorization(flow_id))
    return {"success": result.status in ("pending", "ok"), "status": result.status, "interval": result.interval,
            "login": result.login, "message": result.message}


@router.get("/github/account")
@route_surface(RouteSurface.local())
async def github_account(service: FeedbackService = Depends(get_feedback_service)) -> dict:
    """Which way feedback would go now (gh or the App token) and as whom."""
    return {"success": True, "data": await service.account()}


@router.delete("/github/account")
@route_surface(RouteSurface.local())
async def github_sign_out(service: FeedbackService = Depends(get_feedback_service)) -> dict:
    """Delete this machine's App token (revoking it on GitHub is the user's, on the page returned)."""
    return {"success": True, "data": await service.sign_out()}


@router.post("/drafts/{draft_id}/submit")
async def submit_draft(draft_id: str, body: FeedbackSubmitRequest, request: Request,
                       service: FeedbackService = Depends(get_feedback_service)) -> dict:
    """Publish a draft the user confirmed in the client's dialog."""
    return await _answer(service.submit(draft_id, body, _caller(request)))


@router.post("/drafts/{draft_id}/handoff")
async def hand_off_draft(draft_id: str, body: FeedbackHandoffRequest,
                         service: FeedbackService = Depends(get_feedback_service)) -> dict:
    """A prefilled GitHub page for the user to submit the draft there."""
    return await _answer(service.handoff(draft_id, body.reason))


@router.get("")
async def list_feedback(status: FeedbackStatus | None = Query(default=None),
                        limit: int = Query(default=20, ge=1, le=100),
                        service: FeedbackService = Depends(get_feedback_service)) -> dict:
    """The ledger, newest first: summary rows (no text). Settles this node's open rows first."""
    return {"success": True, "data": await service.ledger(status, limit)}
