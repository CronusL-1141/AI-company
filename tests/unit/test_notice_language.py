"""Language precedence, host isolation and settings persistence over real HTTP."""

import asyncio
import json
import plistlib
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from aiteam.api import language
from aiteam.api.routes import settings

_SYSTEM_LANGUAGE = language._system_language


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    # AITEAM_DB_PATH does not isolate the JSON settings or host preferences.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(settings, "_CONFIG_PATH", tmp_path / "os/wake_config.json")
    monkeypatch.setattr(language, "_system_language", lambda: "en")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def client():
    app = FastAPI()
    app.include_router(settings.router)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        trust_env=False,
    )


async def test_manual_choice_persists_across_instances_and_follow_restores_cc(tmp_path):
    project = tmp_path / "project"
    write_json(project / ".claude/settings.local.json", {"language": "Chinese"})
    params = {"cwd": str(project), "host": "cc"}
    async with client() as first:
        response = await first.put("/api/settings/language", params=params, json={"mode": "en"})
        assert response.status_code == 200
        assert response.json() == {"mode": "en", "effective": "en", "source": "dashboard"}
    assert json.loads(settings._CONFIG_PATH.read_text())["language_mode"] == "en"
    async with client() as restarted:
        assert (await restarted.get("/api/settings/language", params=params)).json() == response.json()
        restored = await restarted.put("/api/settings/language", params=params, json={"mode": "follow"})
        assert restored.json() == {"mode": "follow", "effective": "zh", "source": "cc_settings"}
    async with client() as next_instance:
        assert (await next_instance.get("/api/settings/language", params=params)).json() == restored.json()


async def test_project_local_then_project_then_global_precedence(tmp_path):
    project = tmp_path / "project"
    local = project / ".claude/settings.local.json"
    shared = project / ".claude/settings.json"
    write_json(Path.home() / ".claude/settings.json", {"language": "Chinese"})
    write_json(shared, {"language": "English"})
    write_json(local, {"language": "中文"})
    assert (await language.resolve_language(cwd=str(project), host="cc"))["effective"] == "zh"
    write_json(local, {"other": "value"})
    assert (await language.resolve_language(cwd=str(project), host="cc"))["effective"] == "en"
    shared.write_text("{broken", encoding="utf-8")
    assert (await language.resolve_language(cwd=str(project), host="cc"))["effective"] == "zh"


@pytest.mark.parametrize("value,expected", [
    ("Chinese", "zh"), ("Chinese (Simplified)", "zh"), ("简体中文", "zh"),
    ("zh-TW", "zh"), ("ZH_cn.UTF-8", "zh"), ("English", "en"),
    ("en-GB", "en"), ("Japanese", "en"),
])
async def test_cc_language_normalization(value, expected):
    write_json(Path.home() / ".claude/settings.json", {"language": value})
    result = await language.resolve_language(host="cc", fallback_language="zh")
    assert result == {"mode": "follow", "effective": expected, "source": "cc_settings"}


async def test_cc_custom_config_dir_and_invalid_settings_fall_through(tmp_path, monkeypatch):
    custom = tmp_path / "cc-custom"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    write_json(Path.home() / ".claude/settings.json", {"language": "en"})
    write_json(custom / "settings.json", {"language": "zh"})
    project = tmp_path / "project"
    write_json(project / ".claude/settings.local.json", ["zh"])
    write_json(project / ".claude/settings.json", {"language": "en", "extra": "x" * 65536})
    assert (await language.resolve_language(cwd=str(project), host="cc"))["effective"] == "zh"


@pytest.mark.parametrize("host", ["codex", "system"])
async def test_host_isolation_and_dashboard_override(host, monkeypatch):
    monkeypatch.setattr(language, "_cc_language", lambda _: pytest.fail("must not inspect CC settings"))
    assert await language.resolve_language(host=host) == {
        "mode": "follow", "effective": "en", "source": "system",
    }
    settings._update_config({"language_mode": "zh"})
    assert await language.resolve_language(host=host, fallback_language="en") == {
        "mode": "zh", "effective": "zh", "source": "dashboard",
    }


async def test_default_dashboard_has_no_cc_context_and_old_override_is_ignored(monkeypatch):
    write_json(Path.home() / ".claude/settings.json", {"language": "zh"})
    monkeypatch.setenv("AITEAM_NOTICE_LANGUAGE", "zh")
    async with client() as api:
        assert (await api.get("/api/settings/language")).json() == {
            "mode": "follow", "effective": "en", "source": "system",
        }


async def test_hook_locale_fallback_then_default(monkeypatch):
    monkeypatch.setattr(language, "_system_language", lambda: None)
    assert await language.resolve_language(host="codex", fallback_language="zh-CN") == {
        "mode": "follow", "effective": "zh", "source": "system",
    }
    assert await language.resolve_language(host="codex") == {
        "mode": "follow", "effective": "en", "source": "default",
    }


def test_apple_languages_precede_shell_locale(monkeypatch):
    monkeypatch.setattr(language.sys, "platform", "darwin")
    for key in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    path = Path.home() / "Library/Preferences/.GlobalPreferences.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps({"AppleLanguages": ["zh-Hans-CN", "en-US"]}))
    assert _SYSTEM_LANGUAGE() == "zh"
    path.write_bytes(b"broken")
    assert _SYSTEM_LANGUAGE() == "en"
    monkeypatch.setenv("LANGUAGE", "zh_CN:en_US")
    assert _SYSTEM_LANGUAGE() == "zh"
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert _SYSTEM_LANGUAGE() == "en"


@pytest.mark.parametrize("body", [{"mode": "de"}, {"mode": None}, {}, {"mode": ["en"]}])
async def test_invalid_mode_is_rejected_without_mutation(body):
    settings._update_config({"language_mode": "zh"})
    async with client() as api:
        assert (await api.put("/api/settings/language", json=body)).status_code == 422
        assert (await api.get("/api/settings/language")).json()["mode"] == "zh"


async def test_invalid_host_is_rejected():
    async with client() as api:
        assert (await api.get("/api/settings/language?host=other")).status_code == 422


async def test_wake_and_language_updates_preserve_each_other_under_concurrency():
    wake = {"interval": "1h", "prompt_template": "keep me", "autonomy_level": "consult"}
    settings._update_config({"unrelated": {"enabled": True}})
    async with client() as api:
        await api.put("/api/settings/language", json={"mode": "zh"})
        assert (await api.put("/api/settings/wake-config", json=wake)).status_code == 200
        assert (await api.get("/api/settings/language")).json()["mode"] == "zh"
        responses = await asyncio.gather(*(
            api.put(path, json=body)
            for _ in range(24)
            for path, body in (("/api/settings/language", {"mode": "en"}),
                               ("/api/settings/wake-config", wake))
        ))
        assert all(response.status_code == 200 for response in responses)
    async with client() as fresh:
        assert (await fresh.get("/api/settings/language")).json()["mode"] == "en"
        persisted = (await fresh.get("/api/settings/wake-config")).json()
        assert all(persisted[key] == value for key, value in wake.items())
        assert persisted["unrelated"] == {"enabled": True}


async def test_write_failure_does_not_claim_or_apply_success(monkeypatch):
    settings._update_config({"language_mode": "zh"})

    def unavailable(_):
        raise OSError("read-only settings")

    monkeypatch.setattr(settings, "_save_config", unavailable)
    async with client() as api:
        assert (await api.put("/api/settings/language", json={"mode": "en"})).status_code == 500
        assert (await api.get("/api/settings/language")).json()["mode"] == "zh"


async def test_file_io_does_not_block_the_request_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_load():
        started.set()
        assert release.wait(2), "file I/O blocked the request loop"
        return {"language_mode": "en"}

    monkeypatch.setattr(settings, "_load_config", slow_load)
    async with client() as api:
        pending = asyncio.create_task(api.get("/api/settings/language"))
        try:
            assert await asyncio.to_thread(started.wait, 1)
        finally:
            release.set()
        assert (await pending).status_code == 200
