"""Codex 已安装 hook 漂移比对脚本自身的用例 —— 证明它抓得到漂移，不是一条永远绿的装饰。

守的失效模式：`~/.codex/hooks/` 下的副本与仓内源码各走各的，没人看得见。机检写歪了
（路径拼错、比对比了个寂寞、少扫一个安装目录）就会永远报绿，比没有机检更坏 —— 所以这里
既测"该红时真红"（漂移/仅装侧 → 退出码 1），也测"该绿时真绿"（全同源 → 0），还测
"该闭嘴时闭嘴"（这台机器没装 Codex 副本 → 打一行说明退出 0）。

两个安装目录是一等公民：`ai-team-os`（手工镜像，源码或适配器任一相等都算同源）与
`ai-team-os-observer`（适配器安装位，只认适配器）。同名文件装在两处必须各成一行、各算
各的注册 —— 合成一行就会让一处的注册替另一处顶包，正是这批用例要钉死的。

全部用 tmp_path 造假目录树，经 --codex-home / --repo-root / --installed 注入，绝不读
真实 ~/.codex。断言跨边界：不只看返回值，也看 stdout 里那一行到底印了什么。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "scripts" / "check_codex_installed_hooks.py"

PYTHON = "/usr/bin/python3"

MIRROR = "ai-team-os"
OBSERVER = "ai-team-os-observer"


def _load():
    spec = importlib.util.spec_from_file_location("_codex_hook_drift_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析注解时会回查 sys.modules[__module__]，先登记再 exec，否则 AttributeError
    sys.modules[spec.name] = module
    # 被测脚本对外承诺只读，测它的时候也别往仓里落 .pyc
    prior = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prior
    return module


check = _load()


def _write_all(directory: Path, files: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (directory / name).write_text(body, encoding="utf-8")


def installed_dir_of(tmp_path: Path, name: str = MIRROR) -> Path:
    return tmp_path / "codex" / "hooks" / name


def _write_registries(
    repo: Path,
    support_modules: tuple[str, ...] | str | None,
    verbatim_copies: tuple[str, ...] | str | None,
) -> None:
    """在假仓里造出两份配套模块登记。传 None = 这份登记不存在（测降级），传 str = 原样写。"""
    if support_modules is not None:
        path = repo / "plugin" / "harness" / "codex" / "surface.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(support_modules, str):
            body = support_modules
        else:
            listed = "".join(f"    {name!r},\n" for name in support_modules)
            body = f"CODEX_SUPPORT_MODULES = (\n{listed})\n"
        path.write_text(body, encoding="utf-8")
    if verbatim_copies is not None:
        path = repo / "scripts" / "check_codex_isolation.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(verbatim_copies, str):
            body = verbatim_copies
        else:
            listed = "".join(
                f'    Path("hooks/{name}"): Path("plugin/hooks/{name}"),\n'
                for name in verbatim_copies
            )
            body = f"from pathlib import Path\n\nVERBATIM_COPIES = {{\n{listed}}}\n"
        path.write_text(body, encoding="utf-8")


def make_tree(
    tmp_path: Path,
    *,
    installed: dict[str, str] | None = None,
    observer: dict[str, str] | None = None,
    source: dict[str, str] | None = None,
    adapter: dict[str, str] | None = None,
    manifest: dict | str | None = None,
    support_modules: tuple[str, ...] | str | None = (),
    verbatim_copies: tuple[str, ...] | str | None = (),
    create_installed_dir: bool = True,
) -> tuple[Path, Path]:
    """造一棵假的 (codex_home, repo_root)。

    `installed` 落在 `hooks/ai-team-os/`，`observer` 落在 `hooks/ai-team-os-observer/`；
    传 None 的那个目录**不创建**，用来分别测"两个装侧都在"与"只有一个在"。
    manifest 传 dict 写 JSON，传 str 原样写。
    两份配套模块登记默认造成空的（走正常路径），传 None 则整份文件不存在（走降级路径）。
    """
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    if create_installed_dir:
        _write_all(installed_dir_of(tmp_path, MIRROR), installed or {})
    if observer is not None:
        _write_all(installed_dir_of(tmp_path, OBSERVER), observer)

    repo = tmp_path / "repo"
    _write_all(repo / "src" / "aiteam" / "hooks", source or {})
    _write_all(repo / "plugin" / "harness" / "codex" / "hooks", adapter or {})
    _write_registries(repo, support_modules, verbatim_copies)

    if manifest is not None:
        path = codex_home / "hooks.json"
        path.write_text(
            manifest if isinstance(manifest, str) else json.dumps(manifest),
            encoding="utf-8",
        )
    return codex_home, repo


def manifest_for(installed_dir: Path, mapping: dict[str, list[str]]) -> dict:
    """按真实 hooks.json 的形状造清单：事件 -> 组 -> hooks -> command 整条命令行。"""
    hooks: dict[str, list] = {}
    for name, events in mapping.items():
        for event in events:
            command = f'"{PYTHON}" \'{installed_dir / name}\' {event}'
            hooks.setdefault(event, []).append(
                {"matcher": "*", "hooks": [{"type": "command", "command": command}]}
            )
    return {"hooks": hooks}


def merge_manifests(*manifests: dict) -> dict:
    """把几份清单拼成一份 —— 真机的 hooks.json 就是两个安装目录混在同一批事件里。"""
    hooks: dict[str, list] = {}
    for manifest in manifests:
        for event, groups in manifest["hooks"].items():
            hooks.setdefault(event, []).extend(groups)
    return {"hooks": hooks}


def run(capsys, codex_home: Path, repo: Path, installed: list[Path] | None = None) -> tuple[int, str]:
    argv = ["--codex-home", str(codex_home), "--repo-root", str(repo)]
    for directory in installed or []:
        argv += ["--installed", str(directory)]
    code = check.main(argv)
    return code, capsys.readouterr().out


def rows_of(out: str, name: str) -> list[str]:
    """表里所有以该文件名开头的行 —— 同名装在两个目录时会有两行。"""
    return [line for line in out.splitlines() if line.startswith(name + " ")]


def row_of(out: str, name: str) -> str:
    found = rows_of(out, name)
    if not found:
        raise AssertionError(f"输出里找不到 {name} 这一行:\n{out}")
    return found[0]


def row_in(out: str, name: str, label: str) -> str:
    """指名安装位取那一行 —— 同名双目录时不能靠"第一条"蒙对。"""
    for line in rows_of(out, name):
        if f" {label} " in line or line.rstrip().endswith(f" {label}"):
            return line
    raise AssertionError(f"输出里找不到 {name} 在 {label} 的那一行:\n{out}")


class TestStatus:
    def test_identical_to_source_is_same(self, tmp_path, capsys):
        body = "print('hi')\n"
        codex_home, repo = make_tree(
            tmp_path, installed={"send_event.py": body}, source={"send_event.py": body}
        )
        code, out = run(capsys, codex_home, repo)
        assert check.STATUS_SAME in row_of(out, "send_event.py")
        assert code == 0

    def test_identical_to_adapter_is_same(self, tmp_path, capsys):
        """手工镜像目录里与适配器逐字节相等也算同源 —— 装侧可能装的是 Codex 专用那一份。"""
        body = "print('codex')\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"send_event_codex.py": body},
            source={"send_event_codex.py": "print('something else')\n"},
            adapter={"send_event_codex.py": body},
        )
        code, out = run(capsys, codex_home, repo)
        assert check.STATUS_SAME in row_of(out, "send_event_codex.py")
        assert code == 0

    def test_byte_difference_is_drift(self, tmp_path, capsys):
        codex_home, repo = make_tree(
            tmp_path,
            installed={"session_bootstrap.py": "old\n"},
            source={"session_bootstrap.py": "new\n"},
        )
        code, out = run(capsys, codex_home, repo)
        line = row_of(out, "session_bootstrap.py")
        assert check.STATUS_DRIFT in line
        assert code == 1

    def test_whitespace_only_difference_is_still_drift(self, tmp_path, capsys):
        """逐字节比对 —— 只差一个换行也是漂移，不做"看起来差不多"的宽容。"""
        codex_home, repo = make_tree(
            tmp_path,
            installed={"turn_end_guard.py": "same\n"},
            source={"turn_end_guard.py": "same"},
        )
        code, out = run(capsys, codex_home, repo)
        assert check.STATUS_DRIFT in row_of(out, "turn_end_guard.py")
        assert code == 1

    def test_installed_only(self, tmp_path, capsys):
        """装侧有、两处源都没有 = 来路不明，和漂移同样变红。"""
        codex_home, repo = make_tree(
            tmp_path,
            installed={"pipeline_gate.py": "legacy\n"},
            source={"send_event.py": "x\n"},
        )
        code, out = run(capsys, codex_home, repo)
        line = row_of(out, "pipeline_gate.py")
        assert check.STATUS_INSTALLED_ONLY in line
        assert code == 1

    def test_source_only_does_not_turn_red(self, tmp_path, capsys):
        """仓里有、装侧没有 = 只是没镜像过去，报告出来但不判红。"""
        body = "same\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"send_event.py": body},
            source={"send_event.py": body, "pre_compact_save.py": "never mirrored\n"},
            adapter={"codex_observation.py": "adapter only\n"},
        )
        code, out = run(capsys, codex_home, repo)
        assert check.STATUS_SOURCE_ONLY in row_of(out, "pre_compact_save.py")
        assert check.STATUS_SOURCE_ONLY in row_of(out, "codex_observation.py")
        assert code == 0

    def test_source_only_row_is_emitted_once_not_per_installed_dir(self, tmp_path, capsys):
        """两个安装目录都没有的文件，仅源侧只该出一行 —— 每个目录来一行就是重复计数。"""
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={},
            source={"pre_compact_save.py": "never mirrored\n"},
        )
        code, out = run(capsys, codex_home, repo)
        assert len(rows_of(out, "pre_compact_save.py")) == 1
        assert "汇总: 1 行" in out
        assert code == 0

    def test_shell_scripts_are_compared_too(self, tmp_path, capsys):
        codex_home, repo = make_tree(
            tmp_path,
            installed={"guard.sh": "echo old\n", "same.sh": "echo same\n"},
            source={"guard.sh": "echo new\n", "same.sh": "echo same\n"},
        )
        code, out = run(capsys, codex_home, repo)
        assert check.STATUS_DRIFT in row_of(out, "guard.sh")
        assert check.STATUS_SAME in row_of(out, "same.sh")
        assert code == 1

    def test_non_script_files_and_pycache_are_ignored(self, tmp_path, capsys):
        """`.bak-*` 残片与 __pycache__ 不是 hook，不该进表。"""
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path, installed={"send_event.py": body}, source={"send_event.py": body}
        )
        installed_dir = installed_dir_of(tmp_path)
        (installed_dir / "send_event.py.bak-p0-0-20260903").write_text("old\n", encoding="utf-8")
        (installed_dir / "__pycache__").mkdir()
        (installed_dir / "__pycache__" / "send_event.cpython-312.pyc").write_bytes(b"\x00")

        code, out = run(capsys, codex_home, repo)
        assert "bak-p0-0" not in out
        assert "__pycache__" not in out
        assert "汇总: 1 行" in out
        assert code == 0


class TestMultipleInstalledDirs:
    def test_observer_dir_is_scanned_by_default(self, tmp_path, capsys):
        """默认就该扫两个安装目录 —— 漏扫 observer 会把装好的适配器误报成"仅源侧"。"""
        body = "adapter\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={"send_event_codex.py": body},
            adapter={"send_event_codex.py": body},
        )
        code, out = run(capsys, codex_home, repo)
        line = row_of(out, "send_event_codex.py")
        assert check.STATUS_SAME in line
        assert OBSERVER in line
        assert check.STATUS_SOURCE_ONLY not in line
        assert code == 0

    def test_same_name_in_both_dirs_gets_one_row_each(self, tmp_path, capsys):
        """hook_core.py 两个安装目录都有且内容不同 —— 必须各成一行，不能合并。"""
        mirror_body = "shared for cc\n"
        observer_body = "shared for codex\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"hook_core.py": mirror_body},
            observer={"hook_core.py": observer_body},
            source={"hook_core.py": mirror_body},
            adapter={"hook_core.py": observer_body},
        )
        code, out = run(capsys, codex_home, repo)
        lines = rows_of(out, "hook_core.py")
        assert len(lines) == 2
        assert check.sha8(mirror_body.encode()) in row_in(out, "hook_core.py", MIRROR)
        assert check.sha8(observer_body.encode()) in row_in(out, "hook_core.py", OBSERVER)
        assert all(check.STATUS_SAME in line for line in lines)
        assert "汇总: 2 行" in out
        assert code == 0

    def test_observer_file_matching_source_only_is_not_same(self, tmp_path, capsys):
        """observer 目录只认适配器：碰巧等于 src/aiteam/hooks 的同名文件不算对上了。"""
        body = "cc flavour\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={"hook_core.py": body},
            source={"hook_core.py": body},
            adapter={"hook_core.py": "codex flavour\n"},
        )
        code, out = run(capsys, codex_home, repo)
        assert check.STATUS_DRIFT in row_of(out, "hook_core.py")
        assert code == 1

    def test_observer_file_without_adapter_counterpart_is_installed_only(self, tmp_path, capsys):
        """基准侧（适配器）没有对应文件，即便源码里有同名的，也判来路不明。"""
        body = "cc flavour\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={"send_event.py": body},
            source={"send_event.py": body},
        )
        code, out = run(capsys, codex_home, repo)
        assert check.STATUS_INSTALLED_ONLY in row_of(out, "send_event.py")
        assert code == 1

    def test_header_lists_every_dir_with_its_baseline(self, tmp_path, capsys):
        codex_home, repo = make_tree(tmp_path, installed={}, observer={})
        _, out = run(capsys, codex_home, repo)
        assert f"已安装: {installed_dir_of(tmp_path, MIRROR)}  (基准: 源码或适配器)" in out
        assert f"已安装: {installed_dir_of(tmp_path, OBSERVER)}  (基准: 仅适配器)" in out

    def test_missing_second_dir_is_reported_but_not_fatal(self, tmp_path, capsys):
        """只装了一个目录的机器不该变红，但缺的那个要印出来，别让人以为扫过了。"""
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path, installed={"send_event.py": body}, source={"send_event.py": body}
        )
        code, out = run(capsys, codex_home, repo)
        assert f"已安装: {installed_dir_of(tmp_path, OBSERVER)}  ⚠️ 不存在，已跳过" in out
        assert "汇总: 1 行" in out
        assert code == 0

    def test_explicit_installed_flag_replaces_the_defaults(self, tmp_path, capsys):
        """显式 --installed 就是全集 —— 不该还偷偷带上默认的两个目录。"""
        body = "x\n"
        custom = tmp_path / "codex" / "hooks" / "somewhere-else"
        _write_all(custom, {"send_event.py": body})
        codex_home, repo = make_tree(
            tmp_path,
            installed={"session_bootstrap.py": body},
            source={"send_event.py": body, "session_bootstrap.py": body},
        )
        code, out = run(capsys, codex_home, repo, installed=[custom])
        assert "somewhere-else" in row_of(out, "send_event.py")
        # 默认目录里的 session_bootstrap.py 这回只能以"仅源侧"出现
        assert check.STATUS_SOURCE_ONLY in row_of(out, "session_bootstrap.py")
        assert code == 0

    def test_repeated_installed_flags_are_deduped(self, tmp_path, capsys):
        """同一个目录给两遍不该变成两行。"""
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path, installed={"send_event.py": body}, source={"send_event.py": body}
        )
        mirror = installed_dir_of(tmp_path, MIRROR)
        code, out = run(capsys, codex_home, repo, installed=[mirror, mirror])
        assert len(rows_of(out, "send_event.py")) == 1
        assert code == 0

    def test_colliding_basenames_fall_back_to_full_paths(self, tmp_path, capsys):
        """两个安装目录 basename 撞名时短名会让两行长得一样，必须退回完整路径。"""
        body = "x\n"
        first = tmp_path / "a" / "hooks-dir"
        second = tmp_path / "b" / "hooks-dir"
        _write_all(first, {"send_event.py": body})
        _write_all(second, {"send_event.py": body})
        codex_home, repo = make_tree(tmp_path, installed={}, source={"send_event.py": body})
        code, out = run(capsys, codex_home, repo, installed=[first, second])
        lines = rows_of(out, "send_event.py")
        assert len(lines) == 2
        assert any(str(first) in line for line in lines)
        assert any(str(second) in line for line in lines)
        assert code == 0


class TestHashesAndTimes:
    def test_row_carries_both_hashes_and_mtimes(self, tmp_path, capsys):
        codex_home, repo = make_tree(
            tmp_path,
            installed={"send_event.py": "old\n"},
            source={"send_event.py": "new\n"},
        )
        _, out = run(capsys, codex_home, repo)
        line = row_of(out, "send_event.py")
        assert check.sha8(b"old\n") in line
        assert check.sha8(b"new\n") in line
        # 适配器那一侧没有对应文件，占位符必须在，不能悄悄留空
        assert check.MISSING in line
        assert check.mtime_text(installed_dir_of(tmp_path) / "send_event.py") in line

    def test_mtime_is_rendered_in_utc_not_host_local(self, tmp_path, capsys):
        """I11 单一时钟：mtime 必须按 UTC 印。贴上宿主本地偏移不会报错，只会让两份
        时间不可比 —— 所以拿一个已知 epoch 当锚点钉死。"""
        codex_home, repo = make_tree(tmp_path, installed={"send_event.py": "x\n"})
        anchor = 1_700_000_000  # 2023-11-14T22:13:20Z
        os.utime(installed_dir_of(tmp_path) / "send_event.py", (anchor, anchor))

        _, out = run(capsys, codex_home, repo)
        assert "2023-11-14 22:13" in row_of(out, "send_event.py")
        assert "mtime(UTC)" in out

    def test_sha8_is_eight_hex_chars(self):
        digest = check.sha8(b"payload")
        assert len(digest) == 8
        assert all(c in "0123456789abcdef" for c in digest)


class TestRegistration:
    def test_registered_single_event(self, tmp_path, capsys):
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"session_bootstrap.py": body},
            source={"session_bootstrap.py": body},
            manifest=manifest_for(
                installed_dir_of(tmp_path), {"session_bootstrap.py": ["SessionStart"]}
            ),
        )
        code, out = run(capsys, codex_home, repo)
        assert "已注册: SessionStart" in row_of(out, "session_bootstrap.py")
        assert code == 0

    def test_one_script_registered_on_several_events(self, tmp_path, capsys):
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"workflow_reminder.py": body},
            source={"workflow_reminder.py": body},
            manifest=manifest_for(
                installed_dir_of(tmp_path), {"workflow_reminder.py": ["PreToolUse", "PostToolUse"]}
            ),
        )
        code, out = run(capsys, codex_home, repo)
        line = row_of(out, "workflow_reminder.py")
        assert "已注册: " in line
        assert "PreToolUse" in line and "PostToolUse" in line
        assert code == 0

    def test_observer_registration_is_credited(self, tmp_path, capsys):
        """真机的 send_event_codex.py 是从 observer 目录注册的 —— 必须认得出来。"""
        body = "adapter\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={"send_event_codex.py": body},
            adapter={"send_event_codex.py": body},
            manifest=manifest_for(
                installed_dir_of(tmp_path, OBSERVER),
                {"send_event_codex.py": ["PreToolUse", "Stop"]},
            ),
        )
        code, out = run(capsys, codex_home, repo)
        line = row_of(out, "send_event_codex.py")
        assert "已注册: " in line
        assert "PreToolUse" in line and "Stop" in line
        assert "装了没人调 0" in out
        assert code == 0

    def test_registration_does_not_leak_across_installed_dirs(self, tmp_path, capsys):
        """同名文件装在两处，只有清单点名的那个目录算已注册，另一个仍是未注册。"""
        body = "same\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"hook_core.py": body},
            observer={"hook_core.py": body},
            source={"hook_core.py": body},
            adapter={"hook_core.py": body},
            manifest=manifest_for(
                installed_dir_of(tmp_path, OBSERVER), {"hook_core.py": ["SessionStart"]}
            ),
        )
        code, out = run(capsys, codex_home, repo)
        assert "已注册: SessionStart" in row_in(out, "hook_core.py", OBSERVER)
        assert check.REG_NONE in row_in(out, "hook_core.py", MIRROR)
        assert "装了没人调 1" in out
        assert code == 0

    def test_unregistered_script_is_marked(self, tmp_path, capsys):
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"send_event.py": body, "turn_end_guard.py": body},
            source={"send_event.py": body, "turn_end_guard.py": body},
            manifest=manifest_for(installed_dir_of(tmp_path), {"send_event.py": ["PreToolUse"]}),
        )
        code, out = run(capsys, codex_home, repo)
        assert check.REG_NONE in row_of(out, "turn_end_guard.py")
        assert "已注册: PreToolUse" in row_of(out, "send_event.py")
        assert "装了没人调 1" in out
        assert code == 0

    def test_source_only_rows_are_not_counted_as_unregistered(self, tmp_path, capsys):
        """新口径：仅源侧的行天然没有注册，算进"装了没人调"就是虚报。"""
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"send_event.py": body},
            source={
                "send_event.py": body,
                "pre_compact_save.py": body,
                "context_tracker.py": body,
            },
            adapter={"codex_observation.py": body},
            manifest=manifest_for(installed_dir_of(tmp_path), {"send_event.py": ["PreToolUse"]}),
        )
        code, out = run(capsys, codex_home, repo)
        assert "仅源侧 3" in out
        assert "装了没人调 0" in out
        assert check.REG_NONE not in row_of(out, "pre_compact_save.py")
        assert code == 0

    def test_scripts_outside_every_installed_dir_are_not_credited(self, tmp_path, capsys):
        """清单里同名但路径落在任何安装目录之外的注册不算数。"""
        body = "x\n"
        other = tmp_path / "codex" / "hooks" / "some-other-plugin"
        other.mkdir(parents=True)
        command = f'"{PYTHON}" \'{other / "send_event.py"}\' PreToolUse'
        codex_home, repo = make_tree(
            tmp_path,
            installed={"send_event.py": body},
            source={"send_event.py": body},
            manifest={"hooks": {"PreToolUse": [{"hooks": [{"command": command}]}]}},
        )
        code, out = run(capsys, codex_home, repo)
        assert check.REG_NONE in row_of(out, "send_event.py")
        assert "装了没人调 1" in out
        assert code == 0

    def test_missing_manifest_marks_registration_unreadable(self, tmp_path, capsys):
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"send_event.py": body},
            source={"send_event.py": body},
            manifest=None,
        )
        code, out = run(capsys, codex_home, repo)
        assert check.REG_UNREADABLE in row_of(out, "send_event.py")
        assert "缺失或无法解析" in out
        # 清单读不了不影响漂移判定，这里没漂移就该是绿
        assert code == 0

    def test_broken_json_marks_registration_unreadable_without_crashing(self, tmp_path, capsys):
        codex_home, repo = make_tree(
            tmp_path,
            installed={"send_event.py": "old\n"},
            source={"send_event.py": "new\n"},
            manifest="{ this is not json",
        )
        code, out = run(capsys, codex_home, repo)
        line = row_of(out, "send_event.py")
        assert check.REG_UNREADABLE in line
        # 注册不可判，但漂移照样判 —— 两件事互不绑架
        assert check.STATUS_DRIFT in line
        # 不可判 ≠ 未注册，不该混进"装了没人调"
        assert "装了没人调 0" in out
        assert code == 1

    def test_wrong_shaped_manifest_is_unreadable(self, tmp_path, capsys):
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"send_event.py": body},
            source={"send_event.py": body},
            manifest={"hooks": "should be an object"},
        )
        code, out = run(capsys, codex_home, repo)
        assert check.REG_UNREADABLE in row_of(out, "send_event.py")
        assert code == 0

    def test_unbalanced_quotes_are_reported_not_fatal(self, tmp_path, capsys):
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"send_event.py": body},
            source={"send_event.py": body},
            manifest={"hooks": {"PreToolUse": [{"hooks": [{"command": "python3 'unclosed"}]}]}},
        )
        code, out = run(capsys, codex_home, repo)
        assert "引号不配对" in out
        assert check.REG_NONE in row_of(out, "send_event.py")
        assert code == 0


class TestCompanionModules:
    """配套模块不是 hook 入口，没人在清单里调它们是常态，不该年年挂在"装了没人调"上。"""

    def test_registered_companion_is_not_counted(self, tmp_path, capsys):
        body = "adapter\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={"codex_observation.py": body},
            adapter={"codex_observation.py": body},
            manifest={"hooks": {}},
            support_modules=("codex_observation.py",),
        )
        code, out = run(capsys, codex_home, repo)
        assert check.REG_COMPANION in row_of(out, "codex_observation.py")
        assert "装了没人调 0" in out
        assert code == 0

    def test_shared_core_comes_from_the_isolation_registry(self, tmp_path, capsys):
        """hook_core.py 不在 surface.py 里 —— 它是共用核心，登记在 VERBATIM_COPIES。"""
        body = "shared\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={"hook_core.py": body},
            adapter={"hook_core.py": body},
            manifest={"hooks": {}},
            support_modules=(),
            verbatim_copies=("hook_core.py",),
        )
        code, out = run(capsys, codex_home, repo)
        assert check.REG_COMPANION in row_of(out, "hook_core.py")
        assert "装了没人调 0" in out
        assert code == 0

    def test_both_registries_are_merged_and_reported(self, tmp_path, capsys):
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={"codex_observation.py": body, "hook_core.py": body},
            adapter={"codex_observation.py": body, "hook_core.py": body},
            manifest={"hooks": {}},
            support_modules=("codex_observation.py",),
            verbatim_copies=("hook_core.py",),
        )
        code, out = run(capsys, codex_home, repo)
        assert "配套登记: 2 项" in out
        assert "plugin/harness/codex/surface.py:CODEX_SUPPORT_MODULES 1" in out
        assert "scripts/check_codex_isolation.py:VERBATIM_COPIES 1" in out
        assert "装了没人调 0" in out
        assert code == 0

    def test_missing_registries_degrade_with_a_warning(self, tmp_path, capsys):
        """登记读不到就当空 —— 计数会偏大，但必须说出来，不能静默少报也不能静默多报。"""
        body = "adapter\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={"codex_observation.py": body},
            adapter={"codex_observation.py": body},
            manifest={"hooks": {}},
            support_modules=None,
            verbatim_copies=None,
        )
        code, out = run(capsys, codex_home, repo)
        assert "配套登记: 0 项（无）" in out
        assert "读不到" in out and check.SURFACE_ATTR in out and check.ISOLATION_ATTR in out
        # 降级后这一行退回未注册，数字偏大而不是偏小
        assert check.REG_NONE in row_of(out, "codex_observation.py")
        assert "装了没人调 1" in out
        assert code == 0

    def test_unreadable_registry_does_not_crash(self, tmp_path, capsys):
        """别人的脚本炸了不该带塌一个只读报告，降级即可。"""
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={"codex_observation.py": body},
            adapter={"codex_observation.py": body},
            manifest={"hooks": {}},
            support_modules="raise RuntimeError('boom')\n",
        )
        code, out = run(capsys, codex_home, repo)
        assert "读不到" in out
        assert "装了没人调 1" in out
        assert code == 0

    def test_wrong_shaped_registry_is_treated_as_empty(self, tmp_path, capsys):
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={"codex_observation.py": body},
            adapter={"codex_observation.py": body},
            manifest={"hooks": {}},
            support_modules='CODEX_SUPPORT_MODULES = "not a tuple"\n',
        )
        code, out = run(capsys, codex_home, repo)
        assert "配套登记: 0 项" in out
        assert "装了没人调 1" in out
        assert code == 0

    def test_manifest_registration_beats_the_companion_label(self, tmp_path, capsys):
        """登记归登记，清单里真有人调就照实印 —— 实际调用面永远盖过按名分类。"""
        body = "adapter\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={"codex_observation.py": body},
            adapter={"codex_observation.py": body},
            manifest=manifest_for(
                installed_dir_of(tmp_path, OBSERVER), {"codex_observation.py": ["Stop"]}
            ),
            support_modules=("codex_observation.py",),
        )
        code, out = run(capsys, codex_home, repo)
        line = row_of(out, "codex_observation.py")
        assert "已注册: Stop" in line
        assert check.REG_COMPANION not in line
        assert code == 0

    def test_companion_label_does_not_mask_drift(self, tmp_path, capsys):
        """配套模块免的是"没人调"这一项，漂移照判照红。"""
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={"codex_observation.py": "installed\n"},
            adapter={"codex_observation.py": "repo\n"},
            manifest={"hooks": {}},
            support_modules=("codex_observation.py",),
        )
        code, out = run(capsys, codex_home, repo)
        line = row_of(out, "codex_observation.py")
        assert check.STATUS_DRIFT in line
        assert check.REG_COMPANION in line
        assert "装了没人调 0" in out
        assert code == 1

    def test_unreadable_manifest_still_wins_over_the_companion_label(self, tmp_path, capsys):
        """清单不可判时注册列该说"不知道"，不该拿登记冒充已知。"""
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={},
            observer={"codex_observation.py": body},
            adapter={"codex_observation.py": body},
            manifest="{ not json",
            support_modules=("codex_observation.py",),
        )
        code, out = run(capsys, codex_home, repo)
        assert check.REG_UNREADABLE in row_of(out, "codex_observation.py")
        assert code == 0


class TestSummaryAndExitCode:
    def test_summary_counts_every_category(self, tmp_path, capsys):
        body = "same\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={
                "same.py": body,
                "drifted.py": "installed side\n",
                "mystery.py": "nobody knows\n",
            },
            source={"same.py": body, "drifted.py": "repo side\n", "never_mirrored.py": body},
            manifest=manifest_for(installed_dir_of(tmp_path), {"same.py": ["SessionStart"]}),
        )
        code, out = run(capsys, codex_home, repo)
        assert "汇总: 4 行 / 同源 1 / 漂移 1 / 仅装侧 1 / 仅源侧 1 / 装了没人调 2" in out
        assert code == 1

    def test_summary_spans_both_installed_dirs(self, tmp_path, capsys):
        body = "same\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"send_event.py": body},
            observer={"send_event_codex.py": body, "stray.py": "nobody knows\n"},
            source={"send_event.py": body},
            adapter={"send_event_codex.py": body},
            manifest=merge_manifests(
                manifest_for(installed_dir_of(tmp_path), {"send_event.py": ["PreToolUse"]}),
                manifest_for(
                    installed_dir_of(tmp_path, OBSERVER), {"send_event_codex.py": ["Stop"]}
                ),
            ),
        )
        code, out = run(capsys, codex_home, repo)
        assert "汇总: 3 行 / 同源 2 / 漂移 0 / 仅装侧 1 / 仅源侧 0 / 装了没人调 1" in out
        assert code == 1

    def test_clean_tree_exits_zero_with_green_line(self, tmp_path, capsys):
        body = "same\n"
        codex_home, repo = make_tree(
            tmp_path, installed={"send_event.py": body}, source={"send_event.py": body}
        )
        code, out = run(capsys, codex_home, repo)
        assert "✅" in out
        assert "❌" not in out
        assert code == 0

    def test_unregistered_alone_does_not_turn_red(self, tmp_path, capsys):
        """装了没人调是待裁决的线索，不是错误 —— 不该影响退出码。"""
        body = "same\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"send_event.py": body, "turn_end_guard.py": body},
            source={"send_event.py": body, "turn_end_guard.py": body},
            manifest={"hooks": {}},
        )
        code, out = run(capsys, codex_home, repo)
        assert "装了没人调 2" in out
        assert "✅" in out
        assert code == 0

    def test_dirty_tree_exits_one_with_red_line(self, tmp_path, capsys):
        codex_home, repo = make_tree(
            tmp_path,
            installed={"a.py": "x\n", "b.py": "installed\n"},
            source={"b.py": "repo\n"},
        )
        code, out = run(capsys, codex_home, repo)
        assert "❌ 2 个已安装副本与仓内基准不一致" in out
        assert code == 1

    def test_missing_installed_dir_is_not_a_failure(self, tmp_path, capsys):
        """没接 Codex 的机器上不该变红 —— 否则机检会被全员当噪音忽略。"""
        codex_home, repo = make_tree(
            tmp_path, source={"send_event.py": "x\n"}, create_installed_dir=False
        )
        code, out = run(capsys, codex_home, repo)
        assert "不存在" in out
        assert "本机没有 Codex 侧已安装副本" in out
        assert "汇总:" not in out
        assert code == 0

    def test_empty_installed_dir_reports_source_only_rows(self, tmp_path, capsys):
        codex_home, repo = make_tree(tmp_path, installed={}, source={"send_event.py": "x\n"})
        code, out = run(capsys, codex_home, repo)
        assert check.STATUS_SOURCE_ONLY in row_of(out, "send_event.py")
        assert code == 0

    def test_all_locations_empty_says_so(self, tmp_path, capsys):
        codex_home, repo = make_tree(tmp_path, installed={})
        code, out = run(capsys, codex_home, repo)
        assert "装侧与仓内都没有" in out
        assert "汇总: 0 行" in out
        assert code == 0


class TestPureFunctions:
    @pytest.mark.parametrize(
        ("installed", "source", "adapter", "expected"),
        [
            (b"a", b"a", None, check.STATUS_SAME),
            (b"a", None, b"a", check.STATUS_SAME),
            (b"a", b"b", None, check.STATUS_DRIFT),
            (b"a", b"b", b"c", check.STATUS_DRIFT),
            (b"a", None, None, check.STATUS_INSTALLED_ONLY),
            (None, b"a", None, check.STATUS_SOURCE_ONLY),
            (None, None, b"a", check.STATUS_SOURCE_ONLY),
        ],
    )
    def test_classify_default_baselines(self, installed, source, adapter, expected):
        assert check.classify(installed, source, adapter) == expected

    @pytest.mark.parametrize(
        ("installed", "source", "adapter", "expected"),
        [
            (b"a", None, b"a", check.STATUS_SAME),
            (b"a", b"a", b"a", check.STATUS_SAME),
            # 只等于源码不算数：observer 里的副本就该是适配器那一份
            (b"a", b"a", b"b", check.STATUS_DRIFT),
            (b"a", b"a", None, check.STATUS_INSTALLED_ONLY),
            (None, b"a", b"a", check.STATUS_SOURCE_ONLY),
        ],
    )
    def test_classify_adapter_only_baseline(self, installed, source, adapter, expected):
        got = check.classify(installed, source, adapter, check.ADAPTER_ONLY_BASELINES)
        assert got == expected

    def test_baselines_for_picks_adapter_only_for_observer_dir(self):
        assert check.baselines_for(Path("/x/ai-team-os-observer")) == check.ADAPTER_ONLY_BASELINES
        assert check.baselines_for(Path("/x/ai-team-os")) == check.DEFAULT_BASELINES

    def test_default_installed_relpaths_cover_both_dirs(self):
        assert check.INSTALLED_RELPATHS == (
            ("hooks", "ai-team-os"),
            ("hooks", "ai-team-os-observer"),
        )

    def test_valid_companion_names_drops_paths_and_other_suffixes(self):
        got = check._valid_companion_names(
            ["hook_core.py", "guard.sh", "sub/dir.py", "notes.md", 7, "../escape.py"]
        )
        assert got == ["hook_core.py", "guard.sh"]

    def test_isolation_registry_only_takes_keys_under_the_hooks_dir(self, tmp_path):
        """VERBATIM_COPIES 将来可能登记 hooks/ 之外的逐字节副本，那些不是 hook 配套模块。"""
        path = tmp_path / "fake_isolation.py"
        path.write_text(
            "from pathlib import Path\n"
            "VERBATIM_COPIES = {\n"
            '    Path("hooks/hook_core.py"): Path("a"),\n'
            '    Path("templates/other.py"): Path("b"),\n'
            '    Path("hooks/deep/nested.py"): Path("c"),\n'
            "}\n",
            encoding="utf-8",
        )
        module = check._import_module(path, "_fake_isolation")
        assert check._names_from_isolation(module) == ["hook_core.py"]

    def test_load_companions_against_the_real_repo(self):
        """对着真仓跑一次 —— 假树里造的登记再漂亮，也证明不了常量名还在原处。

        登记一旦改名或搬家，本脚本会静默降级、"装了没人调"凭空变大，这条用例是唯一
        会当场喊出来的地方。
        """
        got = check.load_companions(ROOT)
        assert got.warnings == ()
        assert len(got.sources) == 2
        # 共用核心与适配器伴生模块都必须落在登记里
        assert "hook_core.py" in got.names
        assert "codex_observation.py" in got.names
        assert "codex_completion_delivery.py" in got.names

    def test_display_width_counts_cjk_as_two(self):
        assert check.display_width("仅装侧") == 6
        assert check.display_width("ab") == 2

    def test_pad_aligns_mixed_width_text(self):
        assert check.display_width(check.pad("同源", 6)) == 6
        assert check.display_width(check.pad("abc", 6)) == 6

    def test_script_never_writes_to_disk(self, tmp_path, capsys):
        """只读断言：跑完之后假树里的文件集合与 mtime 一个都没变。

        登记是 import 进来的，`__pycache__` 落一个 .pyc 就是往被比对的仓里写盘 ——
        这里把两份登记都造成非空，让那条路径真的被走到。
        """
        body = "x\n"
        codex_home, repo = make_tree(
            tmp_path,
            installed={"send_event.py": body, "drifted.py": "old\n"},
            observer={"codex_observation.py": body, "hook_core.py": body},
            source={"send_event.py": body, "drifted.py": "new\n"},
            adapter={"codex_observation.py": body, "hook_core.py": body},
            manifest=manifest_for(installed_dir_of(tmp_path), {"send_event.py": ["PreToolUse"]}),
            support_modules=("codex_observation.py",),
            verbatim_copies=("hook_core.py",),
        )

        def snapshot() -> dict[str, float]:
            return {
                str(p.relative_to(tmp_path)): p.stat().st_mtime
                for p in sorted(tmp_path.rglob("*"))
                if p.is_file()
            }

        before = snapshot()
        run(capsys, codex_home, repo)
        assert snapshot() == before
