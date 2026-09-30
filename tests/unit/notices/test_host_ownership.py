"""Each host is told only about itself (task 5b7fbaea, docs/user-notice-design.md §5.3).

E13 codex_copy_stale reaches Codex sessions only. E15 host_version_mismatch
reaches only the older side, in its own words; the newer side and a pair
without an order stay quiet. E16 codex_untrusted reaches no session (it is
listed on demand). Findings go through the production catalog and
ledger, and each host's session reads what another request stored.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from aiteam.clock import utc_now
from aiteam.services.notices import ledger
from aiteam.services.notices.catalog import CATALOG, render_entry
from aiteam.services.notices.detectors import Finding, host_versions
from aiteam.services.notices.detectors.host_versions import HostVersionsDetector, older_side
from aiteam.types import NoticeStatus

from .conftest import StubDetector, request, write_json

E13_VARIANTS = sorted(CATALOG["codex_copy_stale"].variants)


@pytest.mark.parametrize("variant", E13_VARIANTS, ids=lambda variant: variant or "default")
async def test_codex_copy_stale_reaches_codex_sessions_only(repo, variant):
    stub = StubDetector("copies", ["codex_copy_stale"], [Finding(
        catalog_id="codex_copy_stale", key="codex_copy_stale:abcd1234", variant=variant, params={"n": 2},
    )], timing=("session_start",))
    # A Claude Code start runs the detector (the Dashboard stays current) but shows nothing.
    cc = await ledger.pending(repo, request(host="cc", session="c1"), registry=[stub])
    assert cc.user_text == "" and cc.delivery_ids == [] and "Codex" not in cc.model_text
    [row] = (await repo.list_notices(key_prefix="codex_copy_stale:"))[0]
    assert row.status == NoticeStatus.ACTIVE
    again = await ledger.pending(repo, request(host="cc", session="c2"), registry=[])
    assert again.user_text == ""

    codex = await ledger.pending(repo, request(host="codex", session="x1"), registry=[])
    expected = render_entry(CATALOG["codex_copy_stale"], variant=variant, language="en", host="codex",
                            params={"n": 2}).plain
    assert expected in codex.user_text and len(codex.delivery_ids) == 1
    assert 'Tell Codex "' in codex.user_text


@pytest.mark.parametrize("variant", sorted(CATALOG["codex_untrusted"].variants), ids=lambda v: v or "default")
async def test_codex_untrusted_reaches_no_session(repo, variant):
    """E16 is listed on demand only: Codex asks for the review itself, Claude Code is not told."""
    stub = StubDetector("trust", ["codex_untrusted"], [Finding(
        catalog_id="codex_untrusted", key="codex_untrusted:abcd1234", variant=variant,
    )], timing=("session_start", "prompt"))
    for host in ("cc", "codex"):
        start = await ledger.pending(repo, request(host=host, session=f"{host}-1"), registry=[stub])
        prompt = await ledger.pending(repo, request(
            host=host, session=f"{host}-1", event="UserPromptSubmit", emitted=start.delivery_ids,
        ), registry=[stub])
        for response in (start, prompt):
            assert response.user_text == "" and response.delivery_ids == [], host
            assert "/hooks" not in response.model_text, host
    [row] = (await repo.list_notices(key_prefix="codex_untrusted:"))[0]
    assert row.status == NoticeStatus.ACTIVE  # still listed for a diagnosis


@pytest.mark.parametrize("catalog_id", ["codex_copy_stale", "codex_untrusted"], ids=str)
async def test_a_line_shown_before_the_move_is_not_repeated_on_the_old_host(repo, monkeypatch, catalog_id):
    """A Claude Code resume that showed E13/E16 under the old catalog: the next prompt does not repeat it."""
    current = CATALOG[catalog_id]
    stub = StubDetector("s", [catalog_id], [Finding(
        catalog_id=catalog_id, key=f"{catalog_id}:abcd1234", params={"n": 2} if "n" in current.params else {},
    )], timing=("session_start",))
    monkeypatch.setitem(CATALOG, catalog_id, replace(
        current, hosts=frozenset({"cc"}), render_at=frozenset({"session_start"}),
    ))
    resumed = await ledger.pending(repo, request(host="cc", source="resume"), registry=[stub])
    assert len(resumed.delivery_ids) == 1  # unreliable channel, no transcript: a repeat candidate
    monkeypatch.setitem(CATALOG, catalog_id, current)
    prompt = await ledger.pending(repo, request(event="UserPromptSubmit", emitted=resumed.delivery_ids), registry=[])
    assert prompt.user_text == "" and prompt.delivery_ids == []


def _plugin(home, version):
    write_json(home / ".claude" / "plugins" / "installed_plugins.json", {
        "plugins": {"ai-team-os@market": [{"installPath": str(home / "p"), "version": version}]},
    })


def _receipt(home, version):
    fields = {"aiteam_version": version} if version else {}
    write_json(home / ".codex" / "hooks" / host_versions.CODEX_OBSERVER_DIRNAME / host_versions.CODEX_RECEIPT_NAME,
               {"schema": 1, **fields})


@pytest.fixture()
def unhurried_versions(monkeypatch):
    # Production budgets (0.3s, 1.5s) time out on a busy machine: that is "no data", not a pass.
    monkeypatch.setattr(HostVersionsDetector, "timeout_s", 10.0)
    monkeypatch.setitem(ledger.DEADLINE_S, "SessionStart", 10.0)
    monkeypatch.delenv("CODEX_HOME", raising=False)


# (plugin version or None for no plugin, receipt version, host told or None)
E15_CASES = {
    # "1.10.0" < "1.9.0" as strings: only a numeric comparison finds Claude Code older.
    "cc_older": ("1.9.0", "1.10.0", "cc"),
    "codex_older": ("1.14.0", "1.13.1", "codex"),
    "same": ("1.14", "1.14.0", None),
    "codex_missing": ("1.14.0", "", None),
    "cc_missing": (None, "1.13.1", None),
    "no_order": ("1.15.0", "1.15.0rc1", None),
}


@pytest.mark.parametrize("case", sorted(E15_CASES), ids=str)
async def test_host_version_mismatch_reaches_only_the_older_side(repo, isolated_home, unhurried_versions, case):
    plugin, receipt, told = E15_CASES[case]
    if plugin is not None:
        _plugin(isolated_home, plugin)
    _receipt(isolated_home, receipt)
    registry = [HostVersionsDetector()]
    # Claude Code starts first, so each side is seen both as the first and as the second asker.
    cc = await ledger.pending(repo, request(host="cc", session="c1"), registry=registry)
    codex = await ledger.pending(repo, request(host="codex", session="x1"), registry=registry)
    responses = {"cc": cc, "codex": codex}
    for host, response in responses.items():
        if host != told:
            assert "OS v" not in response.user_text and response.delivery_ids == [], host
    if told is None:
        rows, _ = await repo.list_notices(statuses=["active"], key_prefix="host_version_mismatch:")
        assert rows == []
        return
    older, newer = (plugin, receipt) if told == "cc" else (receipt, plugin)
    shown = responses[told]
    assert len(shown.delivery_ids) == 1
    assert f"OS v{older} in {'Claude Code' if told == 'cc' else 'Codex'} is older than v{newer}" in shown.user_text
    other_app = "Codex" if told == "cc" else "Claude Code"
    assert other_app not in shown.user_text, "the line speaks only of this side"
    command = ("claude plugin marketplace update ai-team-os" if told == "cc"
               else "python3 scripts/codex_adapter.py upgrade")
    assert command in shown.model_text
    wrong = "python3 scripts/codex_adapter.py" if told == "cc" else "claude plugin update"
    assert wrong not in shown.model_text
    [row] = (await repo.list_notices(key_prefix="host_version_mismatch:"))[0]
    assert row.host == told and row.variant == ("cc_plugin" if told == "cc" else "codex")
    # A later session of the newer side still gets nothing (read back from the database).
    newer_host = "codex" if told == "cc" else "cc"
    later = await ledger.pending(repo, request(host=newer_host, session="n2"), registry=[])
    assert later.user_text == ""


async def test_the_older_side_catching_up_clears_the_notice(repo, isolated_home, unhurried_versions):
    _plugin(isolated_home, "1.14.0")
    _receipt(isolated_home, "1.13.1")
    registry = [HostVersionsDetector()]
    await ledger.pending(repo, request(host="cc", session="c1"), registry=registry)
    _receipt(isolated_home, "1.14.0")
    await ledger.pending(repo, request(host="codex", session="x1"), registry=registry)
    rows, _ = await repo.list_notices(statuses=["active"], key_prefix="host_version_mismatch:")
    assert rows == []


async def test_a_row_from_before_the_binding_reaches_neither_host(repo, isolated_home, unhurried_versions):
    """Older builds stored E15 for every host (host "", params cc/cx): it is nobody's now."""
    await repo.upsert_notice(key="host_version_mismatch:1.14.0:1.13.1", catalog_id="host_version_mismatch",
                             now=utc_now(), params={"cc": "v1.14.0", "cx": "v1.13.1"}, source="host_versions")
    for host in ("cc", "codex"):
        response = await ledger.pending(repo, request(host=host, session=f"{host}-old"), registry=[])
        assert response.user_text == "" and response.delivery_ids == [], host
    # The detector rebinds the same key to its older side, with the new parameters.
    _plugin(isolated_home, "1.14.0")
    _receipt(isolated_home, "1.13.1")
    await ledger.pending(repo, request(host="cc", session="c1"), registry=[HostVersionsDetector()])
    row = await repo.get_notice("host_version_mismatch:1.14.0:1.13.1")
    assert row.host == "codex" and row.params == {"mine": "v1.13.1", "other": "v1.14.0"}
    codex = await ledger.pending(repo, request(host="codex", session="x1"), registry=[])
    assert "OS v1.13.1 in Codex is older than v1.14.0" in codex.user_text


@pytest.mark.parametrize(("cc", "codex", "expected"), [
    ("1.9.0", "1.10.0", "cc"),
    ("1.10.0", "1.9.9", "codex"),
    ("2.0", "1.99.99", "codex"),
    ("1.14", "1.14.0", None),
    ("1.14.0", "1.14.0", None),
    ("1.15.0", "1.15.0rc1", None),
    ("1.15.0.dev0", "1.14.2", "codex"),
    ("abc", "1.0.0", None),
    ("1.0.0", "", None),
], ids=lambda value: str(value))
def test_versions_compare_as_numbers(cc, codex, expected):
    assert older_side(cc, codex) == expected
