"""Only the opted-in MCP helper and startup budget may change during setup."""
import importlib.util
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("codex_runtime", ROOT / "scripts/codex_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def config(home):
    home.mkdir()
    path = home / "config.toml"
    text = '''model = "existing-model"
# Keep unrelated host configuration.
[mcp_servers.ai-team-os]
url = "http://127.0.0.1:8123/mcp/"
http_headers_helper = "old helper"
[mcp_servers.other]
command = "leave-alone"
'''
    path.write_text(text)
    return path, text


def test_setup_preserves_host_and_other_mcp_settings_and_backs_up(tmp_path):
    home = tmp_path / "codex"
    path, original = config(home)
    result = runtime.configure(home, [path], tmp_path / "runtime", "http://127.0.0.1:8123")
    before, after = tomllib.loads(original), tomllib.loads(path.read_text())
    helper = after["mcp_servers"]["ai-team-os"].pop("http_headers_helper")
    assert after["mcp_servers"]["ai-team-os"].pop("startup_timeout_sec") == 45
    before["mcp_servers"]["ai-team-os"].pop("http_headers_helper")
    assert after == before
    assert "--runtime-script" in helper and "--runtime-dir" in helper
    assert (Path(result["backup"]) / "config.toml").read_text() == original
    assert (home / "bin/aiteam-http-headers.py").read_bytes() == (ROOT / "src/aiteam/mcp/http_headers.py").read_bytes()


def test_failed_setup_restores_config_and_helper(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    path, original = config(home)
    helper = home / "bin/aiteam-http-headers.py"
    helper.parent.mkdir()
    helper.write_text("original helper")
    real_write = runtime.write_bytes
    failed = False

    def injected(target, data):
        nonlocal failed
        if target == path and not failed:
            failed = True
            raise OSError("injected")
        real_write(target, data)

    monkeypatch.setattr(runtime, "write_bytes", injected)
    with pytest.raises(OSError, match="injected"):
        runtime.configure(home, [path], tmp_path / "runtime", "http://127.0.0.1:8123")
    assert path.read_text() == original
    assert helper.read_text() == "original helper"


def test_setup_refuses_foreign_url_or_linked_config(tmp_path):
    home = tmp_path / "codex"
    path, original = config(home)
    with pytest.raises(ValueError):
        runtime.configure(home, [path], tmp_path / "runtime", "http://127.0.0.1:9123")
    assert path.read_text() == original
    other = tmp_path / "shared.toml"
    other.write_text(original)
    path.unlink()
    path.symlink_to(other)
    with pytest.raises(ValueError, match="非链接"):
        runtime.configure(home, [path], tmp_path / "runtime", "http://127.0.0.1:8123")
    assert other.read_text() == original


def test_setup_rejects_parent_traversal_and_preserves_external_config(tmp_path):
    home = tmp_path / "codex"
    _, original = config(home)
    outside = tmp_path / "other"
    outside.mkdir()
    foreign = outside / "config.toml"
    foreign.write_text(original)
    with pytest.raises(ValueError, match="上级路径"):
        runtime.configure(home, [home / ".." / "other/config.toml"], tmp_path / "runtime",
                          "http://127.0.0.1:8123")
    assert foreign.read_text() == original
