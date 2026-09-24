"""The notice update contract is stable, guarded and confined to a temporary HOME."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def adapter(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("AITEAM_API_URL", "http://127.0.0.1:49152")
    spec = importlib.util.spec_from_file_location("codex_adapter_notice", ROOT / "scripts/codex_adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def repo(tmp_path):
    """A temporary Git history; no project checkout, commit or install is touched."""
    repo = tmp_path / "source"
    subprocess.run(["git", "init", "--initial-branch=master", str(repo)], capture_output=True, check=True)
    # A synthetic empty commit makes real branch/detached/worktree checks possible
    # without committing any project content or running Git commit hooks.
    tree = _git(repo, "mktree", input="")
    commit = _git(repo, "-c", "user.name=Adapter fixture", "-c", "user.email=fixture@example.invalid",
                  "-c", "commit.gpgsign=false", "commit-tree", tree, input="Temporary adapter fixture\n")
    _git(repo, "update-ref", "refs/heads/master", commit)
    # Source fixtures are ignored, leaving the repository clean for upgrade.
    (repo / ".git/info/exclude").write_text("*\n!dirty.txt\n", encoding="utf-8")
    for relative in ("plugin/harness/codex", "src/aiteam/mcp"):
        shutil.copytree(ROOT / relative, repo / relative, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (repo / "scripts").mkdir()
    for name in ("codex_adapter.py", "codex_runtime.py"):
        shutil.copy2(ROOT / "scripts" / name, repo / "scripts" / name)
    shutil.copy2(ROOT / "pyproject.toml", repo / "pyproject.toml")
    return repo


def _git(repo, *args, input=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=input, capture_output=True,
                          text=True, check=True).stdout.strip()


def _is_git_read(command):
    return command[0] == "git" and command[3] in {"status", "rev-parse"}


def _files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _paths(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _change_source(repo, name="channel_unread_codex.py"):
    path = repo / "plugin/harness/codex/hooks" / name
    path.write_bytes(path.read_bytes() + b"\n# new released content\n")


def _retire_channel(repo):
    surface = repo / "plugin/harness/codex/surface.py"
    original = surface.read_text()
    changed = original.replace('        ("channel_unread_codex.py", "leader-codex", 3, 0),\n', "")
    assert changed != original
    surface.write_text(changed)
    source = repo / "plugin/harness/codex/hooks/channel_unread_codex.py"
    source.unlink()
    return original


@pytest.mark.parametrize("state", ["unchanged", "modified", "file-removed"],
                         ids=["retired-file", "retired-user-edit", "retired-declaration-only"])
def test_retired_distribution_stays_tracked_after_repeated_updates(adapter, repo, tmp_path, state):
    home = tmp_path / "home/.codex"
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    observer = home / "hooks" / adapter.OBSERVER_DIRNAME
    retired = observer / "channel_unread_codex.py"
    original_digest = hashlib.sha256(retired.read_bytes()).hexdigest()
    registration = (home / "hooks.json").read_bytes()
    if state == "modified":
        retired.write_bytes(retired.read_bytes() + b"\n# user edits must remain intact\n")
    elif state == "file-removed":
        retired.unlink()
    expected_bytes = retired.read_bytes() if retired.exists() else None
    _retire_channel(repo)

    preview = adapter.preview_update(repo, home)
    assert any("退役追踪" in warning and "不会删除" in warning for warning in preview["warnings"])
    assert all(item["path"] not in {str(retired), str(home / "hooks.json")} for item in preview["targets"])
    adapter.apply_update(repo, home, expected_preview=preview)
    first = adapter._receipt(observer)
    assert retired.name not in first["files"]
    assert first["retired_files"] == [retired.name]
    assert first["retired_sha256"] == {retired.name: original_digest}
    assert (home / "hooks.json").read_bytes() == registration
    assert (retired.read_bytes() if retired.exists() else None) == expected_bytes

    assert adapter.main(["update", "--repo-root", str(repo), "--codex-home", str(home)]) == 0
    assert adapter._receipt(observer) == first
    assert (home / "hooks.json").read_bytes() == registration
    assert (retired.read_bytes() if retired.exists() else None) == expected_bytes
    assert adapter.preview_update(repo, home)["nothing_to_do"] is True


@pytest.mark.parametrize("customized", [False, True], ids=["known-old-copy", "user-modified-old-copy"])
def test_reintroduced_distribution_uses_retired_digest_without_weakening_user_edit_guard(
    adapter, repo, tmp_path, customized,
):
    home = tmp_path / "home/.codex"
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    observer = home / "hooks" / adapter.OBSERVER_DIRNAME
    retired = observer / "channel_unread_codex.py"
    original = retired.read_bytes()
    surface_text = _retire_channel(repo)
    adapter.apply_update(repo, home, expected_preview=adapter.preview_update(repo, home))
    if customized:
        retired.write_bytes(original + b"\n# customized while retired\n")
    (repo / "plugin/harness/codex/surface.py").write_text(surface_text)
    source = repo / "plugin/harness/codex/hooks" / retired.name
    source.write_bytes(original + b"\n# redistributed implementation\n")
    command = ["update", "--repo-root", str(repo), "--codex-home", str(home)]
    if customized:
        before = _files(home)
        with pytest.raises(RuntimeError, match="用户修改"):
            adapter.main(command)
        assert _files(home) == before
        adapter.apply_update(repo, home, expected_preview=adapter.preview_update(repo, home))
    else:
        assert adapter.main(command) == 0
    receipt = adapter._receipt(observer)
    assert retired.name in receipt["files"]
    assert receipt["retired_files"] == [] and receipt["retired_sha256"] == {}
    assert retired.read_bytes() == source.read_bytes()


def test_library_import_preserves_process_sys_path(adapter):
    before = list(sys.path)
    spec = importlib.util.spec_from_file_location("codex_adapter_mcp_library", ROOT / "scripts/codex_adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert sys.path == before
    assert callable(module.preview_update) and callable(module.apply_update)


def test_install_and_update_persist_source_version_for_e15(adapter, repo, tmp_path, monkeypatch):
    import aiteam

    monkeypatch.setattr(aiteam, "__version__", "999.0.0")
    home = tmp_path / "home/.codex"
    project = repo / "pyproject.toml"
    project.write_text('[project]\nversion = "5.6.7"\n')
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    receipt_path = home / "hooks" / adapter.OBSERVER_DIRNAME / adapter.META_NAME
    assert json.loads(receipt_path.read_text())["aiteam_version"] == "5.6.7"
    assert json.loads(receipt_path.read_text())["source_branch"] == "master"

    project.write_text('[project]\nversion = "5.6.8"\n')
    preview = adapter.preview_update(repo, home)
    assert preview["baseline"]["version"] == "5.6.8"
    assert any(item["path"] == str(receipt_path) for item in preview["targets"])
    assert json.loads(receipt_path.read_text())["aiteam_version"] == "5.6.7"
    adapter.apply_update(repo, home, expected_preview=preview)
    assert json.loads(receipt_path.read_text())["aiteam_version"] == "5.6.8"
    assert adapter._receipt(receipt_path.parent)["aiteam_version"] == "5.6.8"


@pytest.mark.parametrize("metadata", [None, '[project]\nname = "unknown-version"\n'],
                         ids=["no-pyproject", "no-version"])
def test_receipt_version_stays_unknown_without_source_metadata(adapter, repo, tmp_path, monkeypatch, metadata):
    import aiteam

    monkeypatch.setattr(aiteam, "__version__", "999.0.0")
    project = repo / "pyproject.toml"
    if metadata is None:
        project.unlink()
    else:
        project.write_text(metadata)
    home = tmp_path / "home/.codex"
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    receipt = adapter._receipt(home / "hooks" / adapter.OBSERVER_DIRNAME)
    assert receipt["aiteam_version"] == ""
    assert adapter.preview_update(repo, home)["baseline"]["version"] == ""


@pytest.mark.parametrize("legacy", [False, True], ids=["mode-recorded", "legacy-mode-inferred"])
def test_update_preserves_hooks_only_receipt_and_custom_registration(adapter, repo, tmp_path, legacy):
    home = tmp_path / "home/.codex"
    home.mkdir()
    config = home / "config.toml"
    config.write_text('[mcp_servers.ai-team-os]\ncommand = "custom-stdio"\nargs = ["--keep"]\n')
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    receipt = home / "hooks" / adapter.OBSERVER_DIRNAME / adapter.META_NAME
    if legacy:
        body = json.loads(receipt.read_text())
        body.pop("hooks_only")
        receipt.write_text(json.dumps(body))
    hooks = home / "hooks.json"
    document = json.loads(hooks.read_text())
    handler = document["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    handler["command"] = handler["command"].replace("leader-codex", "custom-reader") + " explicit-project-id"
    hooks.write_text(json.dumps(document))
    before_config, before_hooks = config.read_bytes(), hooks.read_bytes()
    _change_source(repo)

    preview = adapter.preview_update(repo, home)
    assert preview["options"]["hooks_only"] is True
    assert not any(item["path"] == str(config) for item in preview["targets"])
    adapter.apply_update(repo, home, expected_preview=preview)

    assert config.read_bytes() == before_config
    assert hooks.read_bytes() == before_hooks
    assert json.loads(receipt.read_text())["hooks_only"] is True
    assert not (home / "bin").exists()


def test_full_update_preserves_python_url_runtime_and_foreign_config(adapter, repo, tmp_path, monkeypatch):
    home = tmp_path / "home/.codex"
    python = tmp_path / "same python"
    python.symlink_to(sys.executable)
    runtime = tmp_path / "my runtime"
    adapter.install(repo, home, python, dry_run=False, api_url="http://127.0.0.1:49153", runtime_dir=runtime)
    config = home / "config.toml"
    config.write_text(config.read_text() + 'tool_timeout_sec = 321\nbearer_token_env_var = "PRIVATE_ENV"\n')
    before = config.read_bytes()
    monkeypatch.setenv("AITEAM_API_URL", "http://127.0.0.1:49154")
    _change_source(repo)

    preview = adapter.preview_update(repo, home)
    assert preview["options"] == {"python": str(python), "api_url": "http://127.0.0.1:49153",
                                  "runtime_dir": str(runtime), "hooks_only": False}
    assert "PRIVATE_ENV" not in json.dumps(preview)
    adapter.apply_update(repo, home, expected_preview=preview)
    assert config.read_bytes() == before


def test_dry_run_json_is_exact_and_writes_nothing(adapter, repo, tmp_path, capsys):
    home = tmp_path / "home/.codex"
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    _change_source(repo)
    capsys.readouterr()
    before, paths = _files(tmp_path), _paths(tmp_path)
    assert adapter.main(["update", "--repo-root", str(repo), "--codex-home", str(home),
                         "--dry-run", "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert _files(tmp_path) == before
    assert _paths(tmp_path) == paths
    assert preview == adapter.preview_update(repo, home)
    assert preview["baseline"]["root"] == str(repo)
    assert preview["baseline"]["branch"] == "master"
    assert preview["baseline"]["version"]
    for item in preview["targets"]:
        assert item["action"] == "write"
        assert item["summary"]
        assert Path(item["path"]).is_absolute()
        assert item["before_sha256"] == hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()
        if item["source"]:
            assert item["after_sha256"] == hashlib.sha256(Path(item["source"]).read_bytes()).hexdigest()
    assert not list(repo.rglob("__pycache__"))


def test_apply_backs_up_every_changed_file_and_returns_verified_hashes(adapter, repo, tmp_path):
    home = tmp_path / "home/.codex"
    source = repo / "plugin/harness/codex/hooks/channel_unread_codex.py"
    source.chmod(0o750)
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    installed = home / "hooks" / adapter.OBSERVER_DIRNAME / source.name
    assert installed.stat().st_mode & 0o777 == 0o750
    installed.chmod(0o700)
    _change_source(repo)
    preview = adapter.preview_update(repo, home)
    before = {item["path"]: Path(item["path"]).read_bytes() for item in preview["targets"]}
    result = adapter.apply_update(repo, home, expected_preview=preview)
    assert result["mode"] == "applied"
    assert len(result["targets"]) >= 2  # changed hook and receipt
    for item in result["targets"]:
        assert Path(item["backup"]).read_bytes() == before[item["path"]]
        assert ".bak-aiteam-" in item["backup"]
        assert hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() == item["after_sha256"]
    assert installed.stat().st_mode & 0o777 == 0o700
    assert adapter.preview_update(repo, home)["nothing_to_do"] is True


def test_modified_hook_needs_explicit_preview_and_keeps_backup(adapter, repo, tmp_path, capsys):
    home = tmp_path / "home/.codex"
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    target = home / "hooks" / adapter.OBSERVER_DIRNAME / "channel_unread_codex.py"
    target.write_bytes(target.read_bytes() + b"\n# deliberate user edit\n")
    customized = target.read_bytes()
    command = ["update", "--repo-root", str(repo), "--codex-home", str(home)]
    before = _files(home)
    with pytest.raises(RuntimeError, match="用户修改"):
        adapter.main(command)
    assert _files(home) == before
    capsys.readouterr()
    assert adapter.main([*command, "--dry-run", "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert any(str(target) in warning and "覆盖" in warning for warning in preview["warnings"])
    assert any("普通 update 仍会拒绝" in warning for warning in preview["warnings"])
    preview_path = tmp_path / "approved-preview.json"
    preview_path.write_text(json.dumps(preview))
    assert adapter.main([*command, "--expected-preview", str(preview_path), "--json"]) == 0
    applied = json.loads(capsys.readouterr().out)
    item = next(item for item in applied["targets"] if item["path"] == str(target))
    assert Path(item["backup"]).read_bytes() == customized
    assert target.read_bytes() == (repo / "plugin/harness/codex/hooks" / target.name).read_bytes()


def test_missing_notice_support_is_created_and_unchanged_preview_is_clock_stable(
    adapter, repo, tmp_path, monkeypatch,
):
    from datetime import timedelta

    home = tmp_path / "home/.codex"
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    target = home / "hooks" / adapter.OBSERVER_DIRNAME / "user_notice.py"
    target.unlink()
    preview = adapter.preview_update(repo, home)
    current = adapter.utc_now()
    monkeypatch.setattr(adapter, "utc_now", lambda: current + timedelta(minutes=5))
    assert adapter.preview_update(repo, home) == preview
    item = next(item for item in preview["targets"] if item["path"] == str(target))
    assert item["action"] == "create" and item["before_sha256"] == ""
    result = adapter.apply_update(repo, home, expected_preview=preview)
    item = next(item for item in result["targets"] if item["path"] == str(target))
    assert item["backup"] == ""
    assert target.read_bytes() == (repo / "plugin/harness/codex/hooks/user_notice.py").read_bytes()


@pytest.mark.parametrize("drift", ["source", "foreign_registration", "options", "branch"],
                         ids=["source-bytes", "unchanged-file", "effective-options", "baseline"])
def test_apply_refuses_drift_before_any_backup_or_write(adapter, repo, tmp_path, monkeypatch, drift):
    home = tmp_path / "home/.codex"
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    _change_source(repo)
    preview = adapter.preview_update(repo, home)
    options = {}
    if drift == "source":
        _change_source(repo)
    elif drift == "foreign_registration":
        hooks = home / "hooks.json"
        document = json.loads(hooks.read_text())
        document["custom"] = "user changed an unrelated key"
        hooks.write_text(json.dumps(document))
    elif drift == "options":
        options["runtime_dir"] = tmp_path / "different-runtime"
    else:
        original = adapter._baseline
        monkeypatch.setattr(adapter, "_baseline", lambda root: {**original(root), "branch": "different"})
    before, paths = _files(home), _paths(home)
    with pytest.raises(RuntimeError, match="重新预览"):
        adapter.apply_update(repo, home, expected_preview=preview, **options)
    assert _files(home) == before
    assert _paths(home) == paths


def test_failed_update_rolls_back_all_written_targets(adapter, repo, tmp_path, monkeypatch):
    home = tmp_path / "home/.codex"
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    _change_source(repo)
    _change_source(repo, "hook_core.py")
    preview = adapter.preview_update(repo, home)
    before = {Path(item["path"]): Path(item["path"]).read_bytes() for item in preview["targets"]}
    original = adapter._write_bytes

    def fail_after_receipt(path, content):
        if path.name == "hook_core.py":
            raise OSError("injected mid-transaction failure")
        original(path, content)

    monkeypatch.setattr(adapter, "_write_bytes", fail_after_receipt)
    with pytest.raises(OSError, match="injected"):
        adapter.apply_update(repo, home, expected_preview=preview)
    assert {path: path.read_bytes() for path in before} == before


def test_upgrade_rejects_dirty_repo_without_running_mutations(adapter, repo, tmp_path, monkeypatch):
    home = tmp_path / "home/.codex"
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    (repo / "dirty.txt").write_text("unsaved work")
    original = adapter.subprocess.run
    commands = []

    def record(command, **kwargs):
        commands.append(command)
        return original(command, **kwargs)

    monkeypatch.setattr(adapter.subprocess, "run", record)
    with pytest.raises(RuntimeError, match="工作区不干净"):
        adapter.upgrade(repo, home, hooks_only=True)
    assert all(_is_git_read(command) for command in commands)
    assert commands[-1][3:] == ["status", "--porcelain", "--untracked-files=all"]


@pytest.mark.parametrize("hooks_only", [True, False], ids=["hooks-only", "full-mcp"])
def test_upgrade_orders_stub_commands_and_preserves_receipt(adapter, repo, tmp_path, monkeypatch, hooks_only):
    home = tmp_path / "home/.codex"
    python = tmp_path / "selected-python"
    python.symlink_to(sys.executable)
    runtime = tmp_path / "runtime"
    adapter.install(repo, home, python, dry_run=False, hooks_only=hooks_only,
                    api_url="http://127.0.0.1:49153", runtime_dir=runtime)
    original = adapter.subprocess.run
    commands = []

    def controlled(command, **kwargs):
        commands.append(command)
        if _is_git_read(command):
            return original(command, **kwargs)
        # Never run pip, pull, status's network probe or the installed adapter.
        assert kwargs["cwd"] == repo and kwargs["check"] is True
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(adapter.subprocess, "run", controlled)
    assert adapter.upgrade(repo, home) == 0
    writes = [command for command in commands if not _is_git_read(command)]
    assert writes[0] == ["git", "-C", str(repo), "pull", "--ff-only"]
    assert writes[1] == [str(python), "-m", "pip", "install", "-e", "."]
    assert [command[2] for command in writes[2:]] == ["update", "status"]
    for command in writes[2:]:
        assert command[:2] == [str(python), str(repo / "scripts/codex_adapter.py")]
        assert command[command.index("--python") + 1] == str(python)
        assert ("--hooks-only" in command) is hooks_only
        if not hooks_only:
            assert command[command.index("--api-url") + 1] == "http://127.0.0.1:49153"
            assert command[command.index("--runtime-dir") + 1] == str(runtime)


@pytest.mark.parametrize("fail_step", ["pull", "pip", "update"], ids=["pull-failed", "pip-failed", "update-failed"])
def test_upgrade_stops_on_first_failure(adapter, repo, tmp_path, monkeypatch, fail_step):
    home = tmp_path / "home/.codex"
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    original = adapter.subprocess.run
    steps = []

    def controlled(command, **kwargs):
        if _is_git_read(command):
            return original(command, **kwargs)
        step = "pull" if "pull" in command else "pip" if "pip" in command else command[2]
        steps.append(step)
        if step == fail_step:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(adapter.subprocess, "run", controlled)
    with pytest.raises(subprocess.CalledProcessError):
        adapter.upgrade(repo, home, hooks_only=True)
    assert steps == ["pull", "pip", "update"][:["pull", "pip", "update"].index(fail_step) + 1]


def test_upgrade_dry_run_only_checks_worktree(adapter, repo, tmp_path, monkeypatch):
    home = tmp_path / "home/.codex"
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    original = adapter.subprocess.run
    commands = []

    def readonly(command, **kwargs):
        commands.append(command)
        assert _is_git_read(command)
        return original(command, **kwargs)

    monkeypatch.setattr(adapter.subprocess, "run", readonly)
    before = _files(tmp_path)
    assert adapter.upgrade(repo, home, hooks_only=True, dry_run=True) == 0
    assert len(commands) == 3  # source, clean state, source again before the pip step
    assert _files(tmp_path) == before


@pytest.mark.parametrize(("case", "message"), [
    ("other-worktree", "不是回执中的安装树"),
    ("other-repo", "不是回执中的安装树"),
    ("detached", "detached HEAD"),
    ("unrecorded-branch", "未获安装回执认可"),
    ("legacy-branch", "未获安装回执认可"),
    ("nested-receipt", "Git 顶层目录不一致"),
    ("missing-receipt", "缺少有效的 repo_root"),
], ids=["other-worktree", "other-repo", "detached", "unrecorded-branch", "legacy-branch",
        "nested-receipt", "missing-receipt"])
def test_upgrade_rejects_unapproved_git_source_before_mutations(
    adapter, repo, tmp_path, monkeypatch, case, message,
):
    home = tmp_path / "home/.codex"
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    receipt_path = home / "hooks" / adapter.OBSERVER_DIRNAME / adapter.META_NAME
    receipt = json.loads(receipt_path.read_text())
    requested = repo
    if case == "other-worktree":
        requested = tmp_path / "alternate-worktree"
        _git(repo, "worktree", "add", "--detach", str(requested), "HEAD")
    elif case == "other-repo":
        requested = tmp_path / "other-repo"
        _git(repo, "init", "--initial-branch=master", str(requested))
    elif case == "detached":
        _git(repo, "checkout", "--detach", "HEAD")
    elif case in {"unrecorded-branch", "legacy-branch"}:
        _git(repo, "checkout", "-b", "unapproved-feature")
        if case == "legacy-branch":
            receipt.pop("source_branch")
            receipt_path.write_text(json.dumps(receipt))
    elif case == "nested-receipt":
        requested = repo / "nested-source"
        requested.mkdir()
        receipt["repo_root"] = str(requested)
        receipt_path.write_text(json.dumps(receipt))
    elif case == "missing-receipt":
        receipt_path.unlink()
    original = adapter.subprocess.run
    mutations = []

    def controlled(command, **kwargs):
        if _is_git_read(command):
            return original(command, **kwargs)
        # Keep even the inverse/mutant run from doing a real pull or pip install.
        mutations.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(adapter.subprocess, "run", controlled)
    before = _files(home)
    with pytest.raises(RuntimeError, match=message) as error:
        adapter.upgrade(requested, home, hooks_only=True)
    assert mutations == []
    assert _files(home) == before
    text = str(error.value)
    assert "--codex-home" in text and str(home) in text
    if case != "missing-receipt":
        assert "--repo-root" in text and str(receipt["repo_root"]) in text


@pytest.mark.parametrize("case", ["recorded-branch", "master", "legacy-master"],
                         ids=["recorded-branch", "master-with-recorded-branch", "legacy-master"])
def test_upgrade_allows_only_recorded_branch_or_master(adapter, repo, tmp_path, monkeypatch, case):
    home = tmp_path / "home/.codex"
    _git(repo, "checkout", "-b", "installed-release")
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    receipt_path = home / "hooks" / adapter.OBSERVER_DIRNAME / adapter.META_NAME
    receipt = json.loads(receipt_path.read_text())
    assert receipt["source_branch"] == "installed-release"
    if case != "recorded-branch":
        _git(repo, "checkout", "master")
    if case == "legacy-master":
        receipt.pop("source_branch")
        receipt_path.write_text(json.dumps(receipt))
    original = adapter.subprocess.run
    mutations = []

    def controlled(command, **kwargs):
        if _is_git_read(command):
            return original(command, **kwargs)
        mutations.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(adapter.subprocess, "run", controlled)
    assert adapter.upgrade(repo, home) == 0
    assert len(mutations) == 4
    assert mutations[1][1:5] == ["-m", "pip", "install", "-e"]


def test_upgrade_rechecks_branch_after_pull_before_pip(adapter, repo, tmp_path, monkeypatch):
    home = tmp_path / "home/.codex"
    adapter.install(repo, home, Path(sys.executable), dry_run=False, hooks_only=True)
    original = adapter.subprocess.run
    mutations = []

    def controlled(command, **kwargs):
        if _is_git_read(command):
            return original(command, **kwargs)
        mutations.append(command)
        if command[0] == "git" and "pull" in command:
            original(["git", "-C", str(repo), "checkout", "--detach", "HEAD"],
                     check=True, capture_output=True)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(adapter.subprocess, "run", controlled)
    with pytest.raises(RuntimeError, match="detached HEAD"):
        adapter.upgrade(repo, home)
    assert len(mutations) == 1 and "pull" in mutations[0]
