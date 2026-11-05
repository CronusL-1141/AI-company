"""Provider mapping primitives shared by the journal projection and the local binding.

The counting rules that used to live in the raw log reader are covered from
native logs through the journal in ``test_codex_usage_projection``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aiteam.services import codex_local_usage as reader

START = datetime(2026, 9, 14, 10, tzinfo=UTC)
END = START + timedelta(minutes=10)


def alias_config(home: Path, *, alias: str = "decitron", minute: int = -5, extra: str = "",
                 endpoint: str = "https://chatgpt.com/backend-api/codex",
                 login: str = "chatgpt", requires_auth: str = "true", wire_api: str = "responses") -> Path:
    target = home / "config.toml"
    target.write_text(
        f'forced_login_method = "{login}"\n[model_providers.{alias}]\n'
        f'base_url = "{endpoint}"\nrequires_openai_auth = {requires_auth}\n'
        f'wire_api = "{wire_api}"\n{extra}', encoding="utf-8",
    )
    timestamp = START.timestamp() + minute * 60
    os.utime(target, (timestamp, timestamp))
    return target


def test_mapping_digest_ignores_unrelated_fields_and_provider_order(tmp_path):
    config = alias_config(tmp_path, extra='supports_websockets = true\nname = "Original name"\n')
    expected = reader.local_provider_mapping_digest(tmp_path)
    config.write_text(
        'model_provider = "openai"\nforced_login_method = "chatgpt"\n'
        '[model_providers.unused]\nbase_url = "https://example.org/v1"\n'
        '[model_providers.decitron]\nname = "Renamed"\nwire_api = "responses"\n'
        'requires_openai_auth = true\nsupports_websockets = false\n'
        'base_url = "https://chatgpt.com/backend-api/codex"\n'
        '[mcp_servers.example]\nurl = "http://localhost:8000/mcp"\n'
    )
    assert reader.local_provider_mapping_digest(tmp_path) == expected


@pytest.mark.parametrize("change", [
    'model_provider = "decitron"\n',
    'model_provider = "custom"\n',
    '[model_providers.openai]\nbase_url = "https://example.org/v1"\n',
    '[model_providers.openai]\nhttp_headers = { Authorization = "synthetic-fixture" }\n',
])
def test_mapping_digest_tracks_selected_provider_and_native_override(tmp_path, change):
    config = alias_config(tmp_path)
    expected = reader.local_provider_mapping_digest(tmp_path)
    current = config.read_text()
    config.write_text(current + change if change.startswith("[") else change + current)
    assert reader.local_provider_mapping_digest(tmp_path) != expected


def test_mapping_digest_tracks_login_even_without_aliases(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('forced_login_method = "chatgpt"\n')
    expected = reader.local_provider_mapping_digest(tmp_path)
    config.write_text('forced_login_method = "api"\n')
    assert reader.local_provider_mapping_digest(tmp_path) != expected


def test_local_binding_includes_mapping_digest_and_only_stats_auth(tmp_path, monkeypatch):
    from aiteam.services import local_plan_capture

    (tmp_path / "sessions").mkdir()
    (tmp_path / "auth.json").write_text("synthetic-auth-content-must-not-be-read")
    alias_config(tmp_path)
    original_open = os.open

    def guarded_open(path, *args, **kwargs):
        assert Path(path).name != "auth.json"
        return original_open(path, *args, **kwargs)

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(os, "open", guarded_open)
    real = local_plan_capture._local_source()
    monkeypatch.setattr(local_plan_capture, "local_provider_mapping_digest", lambda root: "different-mapping")
    changed = local_plan_capture._local_source()
    assert real[0] == changed[0] == tmp_path
    assert real[1] != changed[1]
