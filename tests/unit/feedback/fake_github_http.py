"""The fake GitHub of fake_github.py over HTTP, for OS's GitHub App token (an httpx MockTransport).

github.com: the device flow and the refresh, as GitHub documents them (and as the live
App answered: no scope sent, a refresh token returned anyway, 8-hour tokens).
api.github.com: REST and GraphQL with ``Authorization: Bearer <token>``; the token acts
as the user who authorized it and reaches only the repositories the App is installed
on. Times come from the test clock, so nothing here waits.

The world's ``app`` section: ``client_id``, ``device_flow`` (enabled), ``installed``
(repositories), ``user`` (who authorizes), ``interval`` (seconds GitHub asks between
polls), ``code_seconds`` (how long a user code lives), ``script`` (what successive polls
of a new flow answer: pending, slow_down, denied, ok; then pending forever), and the
flows, tokens and refresh tokens it issued.
"""

from __future__ import annotations

import json
import secrets
import urllib.parse
from collections.abc import Callable
from datetime import datetime

import httpx

from .fake_github import Exit, World, call, graphql, log, operation, read_state, rest

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
ACCESS_SECONDS = 28800
REFRESH_SECONDS = 15897600
CODE_SECONDS = 899


def app_state(client_id: str, installed: list[str], user: str) -> dict:
    return {"client_id": client_id, "device_flow": True, "installed": installed, "user": user, "interval": 0,
            "code_seconds": CODE_SECONDS, "script": ["ok"], "flows": {}, "tokens": {}, "refresh": {}}


class HttpGitHub:
    """github.com and api.github.com of the fake world, through httpx."""

    def __init__(self, state_path: str, log_path: str, clock: Callable[[], datetime]) -> None:
        self.state_path, self.log_path, self.clock = state_path, log_path, clock

    def client(self) -> httpx.AsyncClient:
        """The production code's ``http`` factory, pointed at this fake."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            return self._oauth(request)
        if request.url.host == "api.github.com":
            return self._api(request)
        return httpx.Response(404, json={"message": "Not Found"})

    # -- github.com --------------------------------------------------------------

    def _oauth(self, request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        now = self.clock().timestamp()
        path = request.url.path
        log(self.log_path, {"op": self._oauth_op(path, form.get("grant_type", "")), "via": "oauth"})
        if request.method != "POST" or request.headers.get("accept") != "application/json":
            return httpx.Response(400, json={"error": "stub: POST with Accept: application/json"})
        answer = call(self.state_path, lambda state: state["app"], "", lambda app: (
            self._device_code(app, form, now) if path == "/login/device/code" else
            self._access_token(app, form, now) if path == "/login/oauth/access_token" else
            {"error": "not_found"}))
        return httpx.Response(200, json=answer)

    @staticmethod
    def _oauth_op(path: str, grant: str) -> str:
        """oauth:code (a new user code), oauth:poll (asking for the token), oauth:refresh."""
        if path == "/login/device/code":
            return "oauth:code"
        return {DEVICE_GRANT: "oauth:poll", "refresh_token": "oauth:refresh"}.get(grant, f"oauth:{path}")

    @staticmethod
    def _device_code(app: dict, form: dict, now: float) -> dict:
        if form.get("client_id") != app["client_id"]:
            return {"error": "incorrect_client_credentials"}
        if not app["device_flow"]:
            return {"error": "device_flow_disabled"}
        code = secrets.token_hex(20)
        user_code = f"{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"
        lifetime = app["code_seconds"]
        app["flows"][code] = {"user_code": user_code, "expires_at": now + lifetime, "interval": app["interval"],
                              "last": None, "script": list(app["script"])}
        return {"device_code": code, "user_code": user_code, "verification_uri": "https://github.com/login/device",
                "expires_in": lifetime, "interval": app["interval"]}

    def _access_token(self, app: dict, form: dict, now: float) -> dict:
        if form.get("client_id") != app["client_id"]:
            return {"error": "incorrect_client_credentials"}
        grant = form.get("grant_type")
        if grant == DEVICE_GRANT:
            return self._poll(app, form.get("device_code", ""), now)
        if grant == "refresh_token":
            held = app["refresh"].get(form.get("refresh_token", ""))
            if held is None or held["used"] or now >= held["expires_at"]:
                return {"error": "bad_refresh_token"}
            held["used"] = True
            app["tokens"].pop(held["access"], None)  # the old access token stops working too
            return self._issue(app, held["login"], now)
        return {"error": "unsupported_grant_type"}

    def _poll(self, app: dict, device_code: str, now: float) -> dict:
        flow = app["flows"].get(device_code)
        if flow is None:
            return {"error": "incorrect_device_code"}
        if now >= flow["expires_at"]:
            return {"error": "expired_token"}
        if flow["last"] is not None and now < flow["last"] + flow["interval"]:
            flow["interval"] += 5
            flow["last"] = now
            return {"error": "slow_down", "interval": flow["interval"]}
        flow["last"] = now
        outcome = flow["script"].pop(0) if flow["script"] else "pending"
        if outcome == "slow_down":
            flow["interval"] += 5
            return {"error": "slow_down", "interval": flow["interval"]}
        if outcome == "pending":
            return {"error": "authorization_pending"}
        del app["flows"][device_code]
        if outcome == "denied":
            return {"error": "access_denied"}
        return self._issue(app, app["user"], now)

    @staticmethod
    def _issue(app: dict, login: str, now: float) -> dict:
        access, refresh = f"ghu_{secrets.token_hex(18)}", f"ghr_{secrets.token_hex(38)}"
        app["tokens"][access] = {"login": login, "expires_at": now + ACCESS_SECONDS}
        app["refresh"][refresh] = {"login": login, "access": access, "used": False,
                                   "expires_at": now + REFRESH_SECONDS}
        return {"access_token": access, "expires_in": ACCESS_SECONDS, "refresh_token": refresh,
                "refresh_token_expires_in": REFRESH_SECONDS, "token_type": "bearer", "scope": ""}

    # -- api.github.com ------------------------------------------------------------

    def _api(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.raw_path.decode().lstrip("/")
        payload = json.loads(request.content) if request.content else None
        op = operation(request.method, endpoint, payload)
        log(self.log_path, {"op": op, "via": "app"})
        state = read_state(self.state_path)
        app = state["app"]
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        held = app["tokens"].get(token)
        if held is None or self.clock().timestamp() >= held["expires_at"]:
            return httpx.Response(401, json={"message": "Bad credentials", "status": "401"})
        if request.headers.get("x-github-api-version") is None:
            return httpx.Response(400, json={"message": "stub: production sends X-GitHub-Api-Version"})
        if state.get("hang", {}).get(op) == "before":
            raise httpx.ReadTimeout("hung before", request=request)

        def world(current: dict) -> World:
            return World(current, actor=held["login"], scope=frozenset(current["app"]["installed"]))

        try:
            result = call(self.state_path, world, op, lambda w: graphql(w, payload) if endpoint == "graphql"
                          else rest(w, request.method, endpoint, payload))
        except Exit as exc:
            return httpx.Response(exc.status, content=exc.stdout.encode(),
                                  headers={"content-type": "application/json"})
        if read_state(self.state_path).get("hang", {}).get(op) == "after":
            raise httpx.ReadTimeout("hung after", request=request)
        status = 201 if request.method == "POST" and endpoint != "graphql" else 200
        return httpx.Response(status, json=result)
