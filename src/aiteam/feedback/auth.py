"""OS's own GitHub authorization for feedback: a GitHub App user token from the device flow.

Used only when the user's gh cannot publish (not installed, not logged in, its token
dead) and the client can show a dialog. The App (``FEEDBACK_APP_CLIENT_ID``, public;
``AITEAM_FEEDBACK_APP_CLIENT_ID`` of the API process overrides it) is installed only
on the feedback repository and may only read and write its issues and discussions,
so its token reaches nothing else. The device flow and the refresh of a device-flow
token need no client secret, so none exists anywhere.

* The device flow: ``start`` asks github.com for a code the user enters at
  github.com/login/device; ``poll`` asks once whether the user has authorized it,
  never sooner than the interval GitHub gave (``slow_down`` adds to it). The device
  code stays in this process's memory, keyed by an opaque flow id; one flow per draft.
* The token: 8 hours, with a refresh token of 6 months that works once. ``token``
  refreshes it shortly before it runs out, under a lock, and stores the new pair
  before anyone uses it, so two requests never spend one refresh token twice.
* Where it is kept (``default_store``): the macOS Keychain through
  ``/usr/bin/security`` (the secret goes on standard input, never on a command line),
  the Secret Service through ``secret-tool`` on Linux with a session bus, else a 0600
  file in the OS data folder. Only this process reads it; it never reaches a log, the
  ledger, an event or an answer.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import httpx

from aiteam.clock import parse_utc, utc_now
from aiteam.feedback.github import API_URL, AppToken, GitHub, GitHubError, Timeouts, default_http

FEEDBACK_APP_CLIENT_ID = "Iv23liEF2wMXFtJ1nIS4"
CLIENT_ID_ENV = "AITEAM_FEEDBACK_APP_CLIENT_ID"
WEB_URL = "https://github.com"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SLOW_DOWN_STEP = 5
# A token this close to its end is refreshed before use.
REFRESH_MARGIN = timedelta(minutes=5)
HTTP_TIMEOUT = 10.0
REVOKE_PAGE = "https://github.com/settings/apps/authorizations"
SERVICE = "ai-team-os.feedback.github"
ACCOUNT = "default"
TOKEN_FILE = "github-token.json"


def configured_client_id() -> str:
    return os.environ.get(CLIENT_ID_ENV, "").strip() or FEEDBACK_APP_CLIENT_ID


@dataclass(frozen=True)
class Credentials:
    """A user access token of the App and what goes with it. The tokens never show in a repr."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: datetime
    refresh_expires_at: datetime | None
    login: str
    client_id: str

    def to_json(self) -> str:
        return json.dumps({
            "access_token": self.access_token, "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat(),
            "refresh_expires_at": self.refresh_expires_at.isoformat() if self.refresh_expires_at else None,
            "login": self.login, "client_id": self.client_id,
        })

    @classmethod
    def from_json(cls, text: str) -> Credentials | None:
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                return None
            expires = parse_utc(data.get("expires_at"))
            if expires is None or not data.get("access_token"):
                return None
            return cls(access_token=str(data["access_token"]), refresh_token=str(data.get("refresh_token") or ""),
                       expires_at=expires, refresh_expires_at=parse_utc(data.get("refresh_expires_at")),
                       login=str(data.get("login") or ""), client_id=str(data.get("client_id") or ""))
        except (ValueError, KeyError, TypeError):
            return None


class TokenStore(Protocol):
    name: str

    async def load(self) -> Credentials | None: ...

    async def save(self, credentials: Credentials) -> None: ...

    async def delete(self) -> bool: ...


class FileStore:
    """A 0600 file (in a 0700 folder) of the OS data folder: the store where there is no keyring."""

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> Credentials | None:
        try:
            return Credentials.from_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def _write(self, text: str) -> None:
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, self.path)

    def _remove(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True

    async def load(self) -> Credentials | None:
        return await asyncio.to_thread(self._read)

    async def save(self, credentials: Credentials) -> None:
        await asyncio.to_thread(self._write, credentials.to_json())

    async def delete(self) -> bool:
        return await asyncio.to_thread(self._remove)


async def _run(argv: list[str], stdin: bytes | None = None, timeout: float = 10.0) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(stdin), timeout)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, stdout


class KeychainStore:
    """The macOS Keychain, through ``security``. The value is the credentials' JSON in base64
    (no quoting to get wrong), written with ``security -i`` so it is read from standard input."""

    name = "keychain"

    def __init__(self, security: str = "/usr/bin/security", service: str = SERVICE, account: str = ACCOUNT) -> None:
        self.security, self.service, self.account = security, service, account

    async def load(self) -> Credentials | None:
        code, out = await _run([self.security, "find-generic-password", "-s", self.service, "-a", self.account, "-w"])
        if code != 0:
            return None
        try:
            return Credentials.from_json(base64.b64decode(out.strip()).decode("utf-8"))
        except ValueError:
            return None

    async def save(self, credentials: Credentials) -> None:
        secret = base64.b64encode(credentials.to_json().encode("utf-8")).decode("ascii")
        command = f"add-generic-password -U -s {self.service} -a {self.account} -w {secret}\n"
        code, _ = await _run([self.security, "-i"], stdin=command.encode("ascii"))
        if code != 0:
            raise GitHubError("failed", f"钥匙串写入失败（security 退出码 {code}）")

    async def delete(self) -> bool:
        code, _ = await _run([self.security, "delete-generic-password", "-s", self.service, "-a", self.account])
        return code == 0


class SecretToolStore:
    """The Secret Service (GNOME Keyring, KWallet) through ``secret-tool``; the secret on standard input."""

    name = "secret-service"

    def __init__(self, secret_tool: str, service: str = SERVICE, account: str = ACCOUNT) -> None:
        self.tool, self.attributes = secret_tool, ["service", service, "account", account]

    async def load(self) -> Credentials | None:
        code, out = await _run([self.tool, "lookup", *self.attributes])
        return Credentials.from_json(out.decode("utf-8")) if code == 0 and out.strip() else None

    async def save(self, credentials: Credentials) -> None:
        code, _ = await _run([self.tool, "store", "--label=AI Team OS feedback", *self.attributes],
                             stdin=credentials.to_json().encode("utf-8"))
        if code != 0:
            raise GitHubError("failed", f"Secret Service 写入失败（secret-tool 退出码 {code}）")

    async def delete(self) -> bool:
        code, _ = await _run([self.tool, "clear", *self.attributes])
        return code == 0


def default_store(data_dir: Path) -> TokenStore:
    """The Keychain on macOS, the Secret Service on Linux with a session bus, else a 0600 file."""
    if sys.platform == "darwin" and os.access("/usr/bin/security", os.X_OK):
        return KeychainStore()
    secret_tool = shutil.which("secret-tool")
    if sys.platform.startswith("linux") and secret_tool and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return SecretToolStore(secret_tool)
    return FileStore(data_dir / "feedback" / TOKEN_FILE)


@dataclass(frozen=True)
class DeviceStart:
    """What the user needs to authorize: the code and where to enter it. No device code."""

    flow_id: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    existing: bool


@dataclass(frozen=True)
class PollResult:
    status: str  # pending / ok / denied / expired / failed
    interval: int = 0
    login: str = ""
    message: str = ""


@dataclass
class _Flow:
    draft_id: str
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    expires_at: datetime
    interval: int
    next_poll_at: datetime


def _seconds(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


class AppAuth:
    """The App's device flows and stored token for this API process (one instance per process)."""

    def __init__(self, store: TokenStore, *, client_id: str | None = None,
                 http: Callable[[], httpx.AsyncClient] = default_http, web_url: str = WEB_URL,
                 api_url: str = API_URL, clock: Callable[[], datetime] = utc_now) -> None:
        self.store = store
        self.client_id = client_id or configured_client_id()
        self.http, self.web_url, self.api_url, self.clock = http, web_url.rstrip("/"), api_url, clock
        self._flows: dict[str, _Flow] = {}
        self._lock = asyncio.Lock()

    def transport(self, credentials: Credentials) -> AppToken:
        return AppToken(credentials.access_token, self.http, self.api_url)

    async def _oauth(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        try:
            async with self.http() as client:
                response = await client.post(f"{self.web_url}/{path}", data=data, timeout=HTTP_TIMEOUT,
                                             headers={"Accept": "application/json"})
        except httpx.TimeoutException:
            raise GitHubError("timeout", "GitHub 授权服务在时限内没有返回") from None
        except httpx.HTTPError as exc:
            raise GitHubError("failed", f"连不上 GitHub：{type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict) or (response.status_code >= 400 and "error" not in body):
            raise GitHubError("failed", f"GitHub 授权服务返回 HTTP {response.status_code}")
        return body

    # -- the device flow ---------------------------------------------------------

    def _live_flow(self, draft_id: str) -> tuple[str, _Flow] | None:
        """The draft's flow still open (flows past their end are dropped on the way)."""
        now = self.clock()
        for flow_id, flow in list(self._flows.items()):
            if flow.expires_at <= now:
                del self._flows[flow_id]
        return next(((key, flow) for key, flow in self._flows.items() if flow.draft_id == draft_id), None)

    async def start(self, draft_id: str) -> DeviceStart:
        """The draft's open flow, or a new one: GitHub's code for the user to enter."""
        live = self._live_flow(draft_id)
        if live is not None:
            flow_id, flow = live
            remaining = int((flow.expires_at - self.clock()).total_seconds())
            return DeviceStart(flow_id, flow.user_code, flow.verification_uri, remaining, flow.interval, True)
        body = await self._oauth("login/device/code", {"client_id": self.client_id})
        if "error" in body or not body.get("device_code"):
            raise GitHubError("failed", f"GitHub 没有给出授权码：{body.get('error', '')}")
        now = self.clock()
        expires_in = _seconds(body.get("expires_in"), 900)
        interval = _seconds(body.get("interval"), 5)
        flow_id = uuid4().hex
        self._flows[flow_id] = _Flow(draft_id, str(body["device_code"]), str(body["user_code"]),
                                     str(body.get("verification_uri") or f"{self.web_url}/login/device"),
                                     now + timedelta(seconds=expires_in), interval, now)
        flow = self._flows[flow_id]
        return DeviceStart(flow_id, flow.user_code, flow.verification_uri, expires_in, interval, False)

    async def poll(self, flow_id: str) -> PollResult:
        """Ask GitHub once whether the user has authorized (never sooner than its interval)."""
        flow = self._flows.get(flow_id)
        now = self.clock()
        if flow is None or flow.expires_at <= now:
            self._flows.pop(flow_id, None)
            return PollResult("expired", message="授权码已过期")
        if now < flow.next_poll_at:
            return PollResult("pending", flow.interval)
        body = await self._oauth("login/oauth/access_token", {
            "client_id": self.client_id, "device_code": flow.device_code, "grant_type": DEVICE_GRANT})
        error = str(body.get("error") or "")
        if error == "slow_down":
            flow.interval = max(_seconds(body.get("interval"), 0), flow.interval + SLOW_DOWN_STEP)
        flow.next_poll_at = self.clock() + timedelta(seconds=flow.interval)
        if error in ("authorization_pending", "slow_down"):
            return PollResult("pending", flow.interval)
        self._flows.pop(flow_id, None)
        if error in ("expired_token", "token_expired"):
            return PollResult("expired", message="授权码已过期")
        if error == "access_denied":
            return PollResult("denied", message="用户在 GitHub 上拒绝了授权")
        if error or not body.get("access_token"):
            return PollResult("failed", message=f"GitHub 授权失败：{error or '没有返回令牌'}")
        credentials = await self._credentials(body, login="")
        return PollResult("ok", login=credentials.login)

    async def _credentials(self, body: dict[str, Any], login: str) -> Credentials:
        """Store a token answer (after asking GitHub who it belongs to when not known)."""
        now = self.clock()
        refresh_in = body.get("refresh_token_expires_in")
        fresh = Credentials(
            access_token=str(body["access_token"]), refresh_token=str(body.get("refresh_token") or ""),
            expires_at=now + timedelta(seconds=_seconds(body.get("expires_in"), 28800)),
            refresh_expires_at=now + timedelta(seconds=_seconds(refresh_in, 0)) if refresh_in else None,
            login=login, client_id=self.client_id)
        if not login:
            login = await GitHub("_/_", self.transport(fresh), Timeouts()).login()
            fresh = replace(fresh, login=login)
        await self.store.save(fresh)
        return fresh

    # -- the stored token ----------------------------------------------------------

    async def token(self) -> Credentials | None:
        """Usable credentials, refreshed first when close to their end; None when there are none."""
        async with self._lock:
            credentials = await self.store.load()
            if credentials is None or credentials.client_id != self.client_id:
                return None
            now = self.clock()
            if credentials.expires_at - REFRESH_MARGIN > now:
                return credentials
            if not credentials.refresh_token or (
                    credentials.refresh_expires_at is not None and credentials.refresh_expires_at <= now):
                await self.store.delete()
                return None
            body = await self._oauth("login/oauth/access_token", {
                "client_id": self.client_id, "grant_type": "refresh_token",
                "refresh_token": credentials.refresh_token})
            if body.get("error") or not body.get("access_token"):
                await self.store.delete()  # spent, revoked or expired: authorize again
                return None
            return await self._credentials(body, login=credentials.login)

    async def forget(self) -> None:
        """GitHub turned the token down (revoked): drop it, so the next need authorizes again."""
        async with self._lock:
            await self.store.delete()

    async def account(self) -> dict[str, Any]:
        """Who the stored token acts as and until when; never the token."""
        credentials = await self.store.load()
        if credentials is None or credentials.client_id != self.client_id:
            return {"app_login": "", "app_expires_at": None, "store": self.store.name}
        return {"app_login": credentials.login, "app_expires_at": credentials.expires_at.isoformat(),
                "store": self.store.name}

    async def sign_out(self) -> bool:
        async with self._lock:
            return await self.store.delete()
