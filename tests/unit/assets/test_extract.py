"""Extractors read only their keys and project them through a whitelist."""

from __future__ import annotations

import json

import pytest

from aiteam.assets import extract
from aiteam.assets.extract import REDACTED, redact, redact_args, redact_url
from aiteam.assets.os_manifest import build_claude_manifest, is_our_hook

OS_COMMAND = '"/usr/bin/python3" "${CLAUDE_CONFIG_DIR:-$HOME/x}/hooks/ai-team-os/send_event.py" Stop'
USER_COMMAND = "bash ~/bin/notify.sh"


def _settings(**hooks) -> bytes:
    return json.dumps({"env": {"API_TOKEN": "value-never-kept"}, "permissions": {"allow": ["Bash"]},
                       "hooks": hooks}).encode()


def test_settings_keeps_only_the_users_hook_registrations():
    data = _settings(Stop=[{"hooks": [{"type": "command", "command": OS_COMMAND},
                                      {"type": "command", "command": USER_COMMAND, "timeout": 5}]}],
                     PreToolUse=[{"matcher": "*", "hooks": [{"type": "command", "command": OS_COMMAND}]}])
    [found] = extract.settings_hooks(data, is_our_hook)
    assert found.rel_path == extract.HOOK_REGISTRATION
    kept = json.loads(found.content)
    assert kept == {"Stop": [{"hooks": [{"type": "command", "command": USER_COMMAND, "timeout": 5}]}]}
    assert b"value-never-kept" not in found.content and b"permissions" not in found.content


def test_only_os_hooks_means_nothing_to_keep():
    assert extract.settings_hooks(_settings(Stop=[{"hooks": [{"command": OS_COMMAND}]}]), is_our_hook) == []
    assert extract.settings_hooks(None, is_our_hook) == []


def test_a_file_that_does_not_parse_is_unknown_not_empty():
    """Caught mid-write it must not read as "every hook, server and plugin was removed"."""
    assert extract.settings_hooks(b"{\"hooks\": {\"Stop\": [", is_our_hook) is None
    assert extract.mcp_servers(b"not json", "") is None
    assert extract.plugin_manifest(b"{}", b"[1, 2") is None
    assert extract.mcp_servers(b"", "") == [] and extract.mcp_servers(b"  \n", "") == []


def test_mcp_definitions_lose_secret_values_and_os_own_server():
    own = build_claude_manifest().mcp_server_name
    config = {
        "oauthAccount": {"emailAddress": "someone@example.com"}, "userID": "u-123",
        "mcpServers": {
            own: {"command": "python3", "args": ["-m", "aiteam.mcp.entry"]},
            "search": {"type": "stdio", "command": "npx", "args": ["-y", "tool"],
                       "env": {"SEARCH_KEY": "secret-env-value"}},
            "remote": {"type": "http", "url": "https://user:hunter2pass@mcp.example.com/v1?x=1",
                       "headers": {"Authorization": "Bearer secret-header-value"}},
        },
        "projects": {"/home/me/app": {"mcpServers": {"db": {"command": "db-mcp"}}, "lastCost": 1.5}},
    }
    found = {(item.owner_path, item.rel_path): json.loads(item.content)
             for item in extract.mcp_servers(json.dumps(config).encode(), own)}
    assert set(found) == {("", "mcp/search.json"), ("", "mcp/remote.json"), ("/home/me/app", "mcp/db.json")}
    assert found[("", "mcp/search.json")] == {"type": "stdio", "command": "npx", "args": ["-y", "tool"],
                                              "env_keys": ["SEARCH_KEY"]}
    assert found[("", "mcp/remote.json")] == {"type": "http", "url": "https://mcp.example.com/v1?x=<redacted>",
                                              "headers_keys": ["Authorization"]}
    text = json.dumps(list(found.values()))
    for secret in ("secret-env-value", "secret-header-value", "hunter2pass", "someone@example.com", "u-123"):
        assert secret not in text


def test_server_names_become_safe_file_names():
    [item] = extract.mcp_servers(json.dumps({"mcpServers": {"a/b c": {"command": "x"}}}).encode(), "")
    assert item.rel_path == "mcp/a%2Fb%20c.json"


def test_plugin_manifest_drops_local_paths():
    installed = {"version": 2, "plugins": {"lsp@official": [{
        "scope": "user", "installPath": "/home/me/.cache/p", "version": "1.2.0", "gitCommitSha": "abc"}]}}
    markets = {"official": {"source": {"source": "github", "repo": "org/plugins"},
                            "installLocation": "/home/me/.cache/m", "lastUpdated": "2026-11-01"}}
    [item] = extract.plugin_manifest(json.dumps(installed).encode(), json.dumps(markets).encode())
    assert json.loads(item.content) == {
        "plugins": [{"name": "lsp", "marketplace": "official", "versions": ["1.2.0"]}],
        "marketplaces": [{"name": "official", "source": {"source": "github", "repo": "org/plugins"}}],
    }
    assert b"/home/me" not in item.content


def test_plugin_manifest_redacts_a_marketplace_url_credential():
    markets = {"private": {"source": {"source": "git",
                                      "url": "https://someone:" + "Pw9xQ2mK7zL4" + "@git.example.com/org/plugins.git"}}}
    [item] = extract.plugin_manifest(b'{"plugins": {}}', json.dumps(markets).encode())
    [market] = json.loads(item.content)["marketplaces"]
    assert market["source"]["url"] == "https://git.example.com/org/plugins.git"
    assert b"someone" not in item.content and b"Pw9xQ2mK7zL4" not in item.content


def test_canonical_json_ignores_key_order():
    a = extract.mcp_servers(b'{"mcpServers": {"s": {"command": "x", "args": ["1"]}}}', "")
    b = extract.mcp_servers(b'{"mcpServers": {"s": {"args": ["1"], "command": "x"}}}', "")
    assert a[0].content == b[0].content


SECRET = "Qz7Xk2Lp9Rm4Tv8Wn3Bc6Hd1Fg5Js0Qz7Xk2Lp9R"


@pytest.mark.parametrize(("args", "expected"), [
    (["--header", f"Authorization: Bearer {SECRET}"], ["--header", REDACTED]),
    (["-H", "X-Api-Key: abc"], ["-H", REDACTED]),
    (["--api-key", SECRET, "--verbose"], ["--api-key", REDACTED, "--verbose"]),
    ([f"--api_key={SECRET}", f"--access-token={SECRET}", f"--client-secret={SECRET}"],
     [f"--api_key={REDACTED}", f"--access-token={REDACTED}", f"--client-secret={REDACTED}"]),
    (["--password", "hunter2", "--port", "8080"], ["--password", REDACTED, "--port", "8080"]),
    (["run", f"Bearer {SECRET}"], ["run", f"Bearer {REDACTED}"]),
    ([f"https://u:p@h.example/x/{SECRET}?key={SECRET}"], [f"https://h.example/x/{REDACTED}?key={REDACTED}"]),
], ids=["header-split", "short-H", "api-key-split", "flag-equals", "password", "bearer", "url"])
def test_args_lose_flag_values_and_embedded_credentials(args, expected):
    assert redact_args(args) == expected


def test_urls_keep_shape_and_lose_credentials():
    assert redact_url("https://user:pw@mcp.example.com:8443/v1/sse?mode=sse&token=t") == \
        f"https://mcp.example.com:8443/v1/sse?mode={REDACTED}&token={REDACTED}"
    assert redact_url("https://mcp.example.com/v1/tools") == "https://mcp.example.com/v1/tools"


def test_hook_entries_lose_header_and_env_values():
    entry = {"type": "http", "url": "https://hooks.example.com/x?token=t",
             "headers": {"Authorization": f"Bearer {SECRET}"}, "env": {"KEY": SECRET},
             "command": f"curl -H 'Authorization: Bearer {SECRET}' https://x.example/y"}
    redacted = redact(entry)
    assert redacted["headers"] == {"Authorization": REDACTED} and redacted["env"] == {"KEY": REDACTED}
    assert SECRET not in json.dumps(redacted) and redacted["url"] == f"https://hooks.example.com/x?token={REDACTED}"
