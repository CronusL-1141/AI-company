"""Workflow asset backup (aiteam.assets): enroll a config folder, see its state, run it, answer the scan.

Only a Claude Code config folder the path section knows can be enrolled: the
requesting session's (the folder its MCP server reports, ``cc_config_scope``), the
API's own, or one an installer recorded. Any other folder is refused. The API only
reads an enrolled folder: nothing here writes into one.

Every answer to a scan hit is recorded as an ``asset.scan_resolved`` event with the
session that gave it, the file, the content hash and the user's own words.

Every write here runs on the machine whose folder it concerns (the session face of
the sync). On an edge of the multi-host sync the backup is paused until its
cross-machine stage: these writes answer 409 with the reason.
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from aiteam.api import cc_config_scope
from aiteam.api.deps import get_asset_backup, get_event_bus
from aiteam.api.event_bus import EventBus
from aiteam.assets.service import AssetBackup, EnrollRefusedError
from aiteam.ids import normalize_root_path
from aiteam.paths import cc_config_dir, install_records, own_cc_config_dir
from aiteam.types import (
    AssetBackupRunRequest,
    AssetEnrollRequest,
    AssetHost,
    AssetRootStatus,
    AssetScanDisposition,
    AssetScanResolveRequest,
    EventType,
    RouteSurface,
)

router = APIRouter(prefix="/api/assets", tags=["assets"])
# Sync face of these routes (aiteam.sync.surface): a folder is read and enrolled by
# the machine it is on, and its scan hits answered there, so its requests are
# handled there (the asset rows travel in the backup's cross-machine stage).
ROUTE_SURFACE = RouteSurface.session()


def _writable_backup(backup: AssetBackup = Depends(get_asset_backup)) -> AssetBackup:
    """The backup, for a write; 409 with the reason while it is paused on this node."""
    if backup.paused:
        raise HTTPException(status_code=409, detail=backup.paused)
    return backup

# How long a manual run is waited for before the answer says it is still running.
RUN_WAIT_SECONDS = 20.0
# Install records name the host of the folder they installed into.
_CC_HOST = "cc"


def _known_folders() -> set[str]:
    """Claude Code config folders the path section knows: this request's, the API's, installed ones."""
    folders = {str(cc_config_dir()), str(own_cc_config_dir())}
    folders |= {str(record["config_dir"]) for record in install_records()
                if record.get("host") == _CC_HOST and isinstance(record.get("config_dir"), str)}
    return {normalize_root_path(folder) for folder in folders}


async def _enrollable(path: str) -> str:
    """The folder a request names (its ``path``, else the session's) when it may be enrolled."""
    chosen = cc_config_scope.accept(path) if path else cc_config_dir()
    if chosen is None:
        raise HTTPException(status_code=400, detail="path 须为绝对路径，且不含 ..")
    folder = normalize_root_path(str(chosen))
    if folder not in await asyncio.to_thread(_known_folders):  # the install records are a file
        raise HTTPException(status_code=400, detail=(
            f"只能登记 Claude Code 配置目录（本会话的、OS 服务自己的或安装记录里的），{folder} 不是"))
    return folder


@router.get("/status")
async def backup_status(host: AssetHost | None = Query(default=None),
                        backup: AssetBackup = Depends(get_asset_backup)) -> dict:
    """Folders known here (this machine's and others'), what each has backed up, and what it has not."""
    status = await backup.status(host)
    session_root = backup.folder_of(cc_config_dir())
    return {"success": True, "data": {**status, "session_folder": str(cc_config_dir()),
                                      "session_root_id": session_root.id if session_root else None}}


@router.post("/roots")
async def enroll_root(body: AssetEnrollRequest, backup: AssetBackup = Depends(_writable_backup)) -> dict:
    """Enroll a config folder into a profile (and compare it at once), or mark it ignored."""
    if body.host != AssetHost.CLAUDE:
        raise HTTPException(status_code=400, detail="Codex home 的备份在 Codex 线的阶段提供，当前只支持 claude")
    path = await _enrollable(body.path)
    if not await asyncio.to_thread(os.path.isdir, path):
        raise HTTPException(status_code=400, detail=f"配置目录不存在：{path}")
    profile = body.profile or await backup.known_profile(body.host, path)
    if not profile:
        raise HTTPException(status_code=400, detail="首次登记须指定 profile（配置档名，如 main）")
    status = AssetRootStatus.ENROLLED if body.action == "enroll" else AssetRootStatus.IGNORED
    try:
        root = await backup.enroll(host=body.host, path=path, profile=profile, status=status,
                                   categories=body.categories)
    except EnrollRefusedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"success": True, "data": root.model_dump(mode="json")}


@router.post("/run")
async def run_backup(body: AssetBackupRunRequest, backup: AssetBackup = Depends(_writable_backup)) -> dict:
    """Compare an enrolled folder now (not throttled) and wait a little for the outcome."""
    root_id = body.root_id
    if not root_id:
        root = backup.folder_of(cc_config_dir())
        if root is None:
            raise HTTPException(status_code=404, detail="本会话的配置目录尚未登记，先调 asset_root_enroll")
        root_id = root.id
    try:
        summary = await backup.run_now(root_id, timeout=RUN_WAIT_SECONDS)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"不是本机已登记的配置目录：{root_id}") from None
    if summary is None:
        return {"success": True, "data": {"root_id": root_id, "running": True}}
    return {"success": not summary.error, "data": {**vars(summary), "running": False}}


@router.post("/findings/resolve")
async def resolve_finding(body: AssetScanResolveRequest, request: Request,
                          backup: AssetBackup = Depends(_writable_backup),
                          bus: EventBus = Depends(get_event_bus)) -> dict:
    """The user's answer to a scan hit: allow that content (it uploads), or exclude the path for good."""
    disposition = AssetScanDisposition.ALLOWED if body.action == "allow" else AssetScanDisposition.EXCLUDED
    resolved = await backup.resolve(body.finding_id, disposition)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"没有这条扫描命中：{body.finding_id}")
    finding, changed = resolved
    session_id = request.headers.get(cc_config_scope.SESSION_HEADER, "")
    await bus.emit(
        EventType.ASSET_SCAN_RESOLVED, f"session:{session_id}" if session_id else "assets",
        {"finding_id": finding.id, "root_id": finding.root_id, "rel_path": finding.rel_path,
         "content_hash": finding.content_hash, "rule": finding.rule, "disposition": str(disposition),
         "session_id": session_id, "mcp_session_id": request.headers.get("X-Aiteam-Mcp-Session-Id", ""),
         "user_quote": body.user_quote},
        entity_id=finding.id, entity_type="asset_scan_finding",
    )
    return {"success": True, "data": {"finding_id": finding.id, "rel_path": finding.rel_path,
                                      "disposition": disposition, "findings_changed": changed}}
