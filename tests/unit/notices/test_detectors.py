"""API detectors against real storage (E07, E08, E09, E10, E14)."""

from __future__ import annotations

from datetime import timedelta

import httpx

from aiteam.clock import utc_now
from aiteam.services.notices import ledger
from aiteam.services.notices.detectors import DetectContext, NoDataError, api_version, registration
from aiteam.services.notices.detectors.api_version import ApiVersionDetector
from aiteam.services.notices.detectors.channels import ChannelMentionDetector
from aiteam.services.notices.detectors.decisions import DecisionsDetector
from aiteam.services.notices.detectors.registration import RegistrationDetector
from aiteam.services.notices.detectors.release import ReleaseDetector
from aiteam.types import NoticeStatus

from .conftest import request, write_json


def _ctx(repo, **values):
    base = dict(host="cc", event="SessionStart", source="startup", session_id="s1", cwd="", project_id="",
                facts={}, now=utc_now(), repo=repo, reader="")
    base.update(values)
    return DetectContext(**base)


async def test_decisions_key_changes_with_the_set_and_clears_when_empty(repo):
    detector = DecisionsDetector()
    first = await repo.create_briefing(title="一", project_id="p1")
    [hit] = await detector.detect(_ctx(repo, project_id="p1"))
    assert hit.params == {"n": 1, "title": "一"} and hit.project_id == "p1"
    await repo.create_briefing(title="二")
    [second] = await detector.detect(_ctx(repo, project_id="p1"))
    assert second.key != hit.key and second.params["n"] == 2 and second.params["title"] == "二"
    other = await detector.detect(_ctx(repo, project_id="p2"))
    assert other[0].key != second.key and other[0].params["n"] == 1
    await repo.resolve_briefing(first.id, "done")
    items = await repo.list_briefings(status="pending")
    for item in items:
        await repo.dismiss_briefing(item.id)
    assert await detector.detect(_ctx(repo, project_id="p1")) == []
    assert not detector.applies(_ctx(repo))  # no project and no folder: nobody to tell


async def test_registration_detector_scopes_to_the_folder(repo, tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    real = registration.real_dir(str(work))
    ctx = _ctx(repo, cwd=str(work), facts={"real_dir": real})
    [hit] = await RegistrationDetector().detect(ctx)
    assert hit.key == f"unregistered_dir:{real}" and hit.project_id == f"dir:{real}"
    await registration.dismiss_dir(str(work))
    assert await RegistrationDetector().detect(ctx) == []


async def test_registration_notice_is_offered_only_in_its_folder(repo, tmp_path):
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()
    there.mkdir()
    await repo.create_project(name="there", root_path=str(there))
    first = await ledger.pending(repo, request().model_copy(update={"cwd": str(here)}))
    assert "未登记" in first.user_text or "not a registered" in first.user_text
    elsewhere = await ledger.pending(repo, request(session="s2").model_copy(update={"cwd": str(there)}))
    assert elsewhere.user_text == ""


async def test_api_version_detector(repo, monkeypatch):
    monkeypatch.setattr(api_version, "_disk_version", lambda: "99.0.0")
    [hit] = await ApiVersionDetector().detect(_ctx(repo))
    assert hit.params["ver"] == "v99.0.0" and hit.key.endswith(":99.0.0")
    import aiteam

    monkeypatch.setattr(api_version, "_disk_version", lambda: aiteam.__version__)
    assert await ApiVersionDetector().detect(_ctx(repo)) == []
    monkeypatch.setattr(api_version, "_disk_version", lambda: None)
    try:
        await ApiVersionDetector().detect(_ctx(repo))
    except NoDataError:
        pass
    else:
        raise AssertionError("an unreadable version must not clear the notice")


def test_disk_version_rereads_the_package_file(tmp_path, monkeypatch):
    init = tmp_path / "__init__.py"
    init.write_text('__version__ = "1.2.3"\n')
    monkeypatch.setattr(api_version, "_package_init", lambda: init)
    api_version._cache.clear()
    assert api_version._disk_version() == "1.2.3"
    init.write_text('"""doc"""\n__version__ = "1.2.4"\n')
    import os

    os.utime(init, ns=(1, 10**18))
    assert api_version._disk_version() == "1.2.4"


async def test_release_detector_uses_install_kind_and_keeps_data_when_offline(repo, tmp_path, monkeypatch,
                                                                             isolated_home):
    from aiteam.api import release_updates

    write_json(isolated_home / ".claude/plugins/installed_plugins.json",
               {"plugins": {"ai-team-os@m": [{"version": "1"}]}})
    online = release_updates.ReleaseChecker(tmp_path / "r.json", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"tag_name": "v99.1.0", "draft": False, "prerelease": False})))
    monkeypatch.setattr(release_updates, "release_checker", online)
    [hit] = await ReleaseDetector().detect(_ctx(repo))
    assert hit.variant == "cc_plugin" and hit.key == "release_available:cc:99.1.0"
    assert hit.params["url"].endswith("/v99.1.0") and hit.params["ver"] == "v99.1.0"
    [codex] = await ReleaseDetector().detect(_ctx(repo, host="codex"))
    assert codex.variant == "codex" and codex.key.startswith("release_available:codex:")

    await ledger.apply_runs(repo, [ledger.DetectorRun(ReleaseDetector(), ReleaseDetector().scope(_ctx(repo)),
                                                      [hit])], utc_now())
    monkeypatch.setattr(release_updates, "release_checker", release_updates.ReleaseChecker(
        tmp_path / "empty.json", transport=httpx.MockTransport(lambda _: httpx.Response(503))))
    offline = await ledger.run_detectors(_ctx(repo), [ReleaseDetector()])
    assert offline[0].findings is None
    await ledger.apply_runs(repo, offline, utc_now())
    assert (await repo.get_notice(hit.key)).status == NoticeStatus.ACTIVE


async def test_channel_detector_and_reminder_cadence(repo):
    await repo.create_channel_message(channel="global", sender="bob", content="请回执\n第二行",
                                      mentions=["leader-cc"], project_id="p1")
    ctx = _ctx(repo, event="UserPromptSubmit", reader="leader-cc", project_id="p1")
    [hit] = await ChannelMentionDetector().detect(ctx)
    assert hit.params["sender"] == "bob" and hit.params["n"] == 1
    assert 'channel_read_ack(channel="global", reader="leader-cc", project_id="p1"' in hit.params["details"]
    assert "last_read_at=<created_at of the last message you read>" in hit.params["details"]

    def prompt(emitted=()):
        return request(event="UserPromptSubmit", emitted=list(emitted)).model_copy(
            update={"reader": "leader-cc", "project_id": "p1"})

    now = utc_now()
    first = await ledger.pending(repo, prompt(), now=now)
    assert "bob mentioned you in global (1 new), passed to Claude" in first.user_text
    assert "channel_read_ack" in first.model_text
    reminders = []
    emitted = first.delivery_ids
    for index in range(1, 7):
        response = await ledger.pending(repo, prompt(emitted), now=now + timedelta(seconds=index))
        emitted = []
        assert response.user_text == ""
        reminders.append("[channel unread]" in response.model_text)
    assert reminders == [False, True, False, False, True, False]
    # A newer message is a new key: shown again with a user line.
    await repo.create_channel_message(channel="global", sender="amy", content="again",
                                      mentions=["leader-cc"], project_id="p1")
    newer = await ledger.pending(repo, prompt(), now=now + timedelta(seconds=30))
    assert "amy" in newer.user_text
    cursor_at = utc_now() + timedelta(seconds=1)
    await repo.set_channel_cursor(reader="leader-cc", channel="global", project_id="p1", last_read_at=cursor_at)
    await ledger.pending(repo, prompt(newer.delivery_ids), now=now + timedelta(seconds=40))
    rows, _ = await repo.list_notices(statuses=["active"], catalog_ids=["channel_mention"])
    assert rows == []


async def test_channel_detector_needs_reader_and_project(repo):
    detector = ChannelMentionDetector()
    assert not detector.applies(_ctx(repo, reader="leader-cc"))
    assert not detector.applies(_ctx(repo, project_id="p1"))
    assert detector.applies(_ctx(repo, reader="leader-cc", project_id="p1"))
