"""Error responses carry a fixed text; the exception's own words stay in the server log.

An exception message can hold internals the caller has no business seeing: codec
details (the offending character and its offset in some internal string), file
paths, SQL. Each case forces the failure inside the real route and checks the body.
"""

from __future__ import annotations

from types import SimpleNamespace

from aiteam.api.routes import ecosystem as ecosystem_routes
from aiteam.storage.repository import StorageRepository
from tests.unit.test_meeting_security import _make_client, _teardown

SECRET = "secret /Users/someone/internal/path.db"


def test_a_unicode_error_answers_a_fixed_400(monkeypatch):
    client, _, _ = _make_client()
    try:
        async def boom(self, **kwargs):
            raise UnicodeEncodeError("utf-8", "x" + chr(0xD800) + SECRET, 1, 2, "surrogates not allowed")

        monkeypatch.setattr(StorageRepository, "create_briefing", boom)
        resp = client.post("/api/leader-briefings", json={"title": "t"})
        assert resp.status_code == 400
        assert "surrogates" not in resp.text and "secret" not in resp.text and "codec" not in resp.text, resp.text
        assert resp.json()["detail"] == "文本含无法编码的字符，详情见服务端日志"
    finally:
        _teardown()


def _pending_batch(snapshot: str):
    async def get_batch(self, batch_id):
        return SimpleNamespace(id=batch_id, status="pending_approval", candidates_snapshot_json=snapshot)
    return get_batch


def test_a_corrupt_batch_snapshot_does_not_echo_the_parser_error(monkeypatch):
    client, _, _ = _make_client()
    try:
        monkeypatch.setattr(StorageRepository, "get_shallow_batch", _pending_batch("{" + SECRET))
        resp = client.post("/api/ecosystem/shallow_batches/b1/approve", json={})
        assert resp.status_code == 500
        assert "secret" not in resp.text and "Expecting" not in resp.text, resp.text
    finally:
        _teardown()


def test_a_failed_batch_dispatch_does_not_echo_the_exception(monkeypatch):
    client, _, _ = _make_client()
    try:
        monkeypatch.setattr(StorageRepository, "get_shallow_batch", _pending_batch('["r1"]'))

        class Worker:
            async def dispatch_batch(self, candidate_ids, batch_id):
                raise RuntimeError(SECRET)

        monkeypatch.setattr(ecosystem_routes, "_get_shallow_worker", lambda repo: Worker())
        resp = client.post("/api/ecosystem/shallow_batches/b1/approve", json={})
        assert resp.status_code == 500
        assert "secret" not in resp.text, resp.text
    finally:
        _teardown()
