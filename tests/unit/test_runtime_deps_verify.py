"""安装/更新后的依赖自愈 —— install.verify_runtime_dependencies。

为什么需要这一层：本仓无 venv，四类进程共享系统 Python，而 Homebrew / Debian 系的
解释器带 PEP 668 外部管理标记，`pip install -e .` 会被**直接拒绝**。更新脚本对这个
失败是容忍的（不容忍则后面的 hook/skill/settings 刷新全做不成，那是更大的漂移），
代价是新增的依赖悄悄没装上——而更新脚本自己并不知道这一版有没有加依赖。

失败形态因此不是报错，而是"工具在列表里、一调就炸"。这里的用例盯住三件事：
声明面只有 pyproject 一处（不许再立第四份清单）、缺失能被 import 判出来、
PEP 668 被拒后会自动改用 --break-system-packages 重试。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("install_module", PROJECT_ROOT / "install.py")
assert _spec is not None and _spec.loader is not None
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


class TestDeclaredDependencies:
    """声明面唯一：依赖清单从 pyproject.toml 现读，不在代码里另抄一份。"""

    def test_reads_pyproject_and_maps_import_names(self):
        deps = dict(install.declared_dependencies(PROJECT_ROOT))
        # extras 要剥掉：uvicorn[standard] 的 import 名是 uvicorn
        assert "uvicorn" in deps
        assert deps["uvicorn"].startswith("uvicorn[standard]")
        # 连字符转下划线
        assert "pydantic_settings" in deps
        # 唯一的例外表项
        assert deps["yaml"].startswith("pyyaml")
        # 版本规格原样保留，补装时才装得到对的下界
        assert deps["httpx"] == "httpx>=0.28.1"

    def test_covers_every_declared_dependency(self):
        """漏读一条就等于漏检一条，所以数量必须与 pyproject 声明数相等。"""
        text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        # 收尾要认独占一行的 "]"：裸 "]" 会被 uvicorn[standard] 里的那个截断
        block = text.split("dependencies = [", 1)[1].split("\n]", 1)[0]
        declared = [
            ln.strip().strip(",").strip('"')
            for ln in block.splitlines()
            if ln.strip().startswith('"')
        ]
        assert len(install.declared_dependencies(PROJECT_ROOT)) == len(declared)

    def test_missing_pyproject_degrades_to_empty(self, tmp_path: Path):
        """读不到就返回空——校验缺席胜过安装器自己崩掉。"""
        assert install.declared_dependencies(tmp_path) == []


@pytest.fixture()
def package_ok(monkeypatch: pytest.MonkeyPatch):
    """The aiteam package check passes, so these cases see only the dependency check.

    Without it the real probe runs, and in a worktree it finds the main checkout's
    package and asks pip to reinstall from the worktree.
    """
    monkeypatch.setattr(install, "package_problem", lambda root: None)


@pytest.mark.usefixtures("package_ok")
class TestVerifyRuntimeDependencies:
    def test_all_present_installs_nothing(self, monkeypatch: pytest.MonkeyPatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(install, "pip_install", lambda specs, root: calls.append(specs))
        missing = install.verify_runtime_dependencies(PROJECT_ROOT)
        # 本进程正在跑测试，说明依赖俱在
        assert missing == []
        assert calls == []

    def test_missing_dependency_triggers_targeted_install(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            install, "declared_dependencies", lambda root: [("no_such_module_xyz", "ghost>=1.0")]
        )
        attempted: list[list[str]] = []

        def fake_install(specs, root):
            attempted.append(list(specs))
            return False  # 装不上，模拟离线/无权限

        monkeypatch.setattr(install, "pip_install", fake_install)
        missing = install.verify_runtime_dependencies(PROJECT_ROOT)
        assert attempted == [["ghost>=1.0"]]
        # 装不上要如实报回缺哪个，不能吞掉
        assert missing == ["ghost>=1.0"]

    def test_manual_command_quotes_specs(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        """兜底命令里的规格必须带引号。

        裸写 `pip install pkg>=1.0` 时 shell 会把 '>' 当重定向：照抄的人得到一个名为
        "=1.0" 的空文件和一个仍然缺依赖的环境，而且看不出哪里错了。
        """
        monkeypatch.setattr(
            install, "declared_dependencies", lambda root: [("no_such_module_xyz", "ghost>=1.0")]
        )
        monkeypatch.setattr(install, "pip_install", lambda specs, root: False)
        install.verify_runtime_dependencies(PROJECT_ROOT)
        printed = capsys.readouterr().out
        assert '"ghost>=1.0"' in printed
        assert "packages ghost>=1.0" not in printed

    def test_recheck_after_successful_install(self, monkeypatch: pytest.MonkeyPatch):
        """补装成功后要复检，不能拿 pip 的返回码当"能 import 了"。"""
        monkeypatch.setattr(
            install, "declared_dependencies", lambda root: [("json", "json-stub>=1.0")]
        )
        seen: list[bool] = []

        def fake_importable(module: str) -> bool:
            # 第一次判缺、补装后第二次判有 —— 复检必须真的再问一次
            seen.append(True)
            return len(seen) > 1

        monkeypatch.setattr(install, "_importable", fake_importable)
        monkeypatch.setattr(install, "pip_install", lambda specs, root: True)
        assert install.verify_runtime_dependencies(PROJECT_ROOT) == []
        assert len(seen) == 2


class TestPipInstallPep668:
    """PEP 668 被拒后自动改用 --break-system-packages 重试。

    不重试就等于：Homebrew / 系统 Python 的用户永远装不上新依赖，而这正是本仓
    默认的运行环境（venv 禁令）。
    """

    def _fake_runner(self, monkeypatch: pytest.MonkeyPatch, results: list[tuple[int, str]]):
        calls: list[list[str]] = []

        def fake_run(args, cwd=None):
            calls.append(list(args))
            code, output = results[len(calls) - 1]
            return code, output, ""

        monkeypatch.setattr(install, "_run_capture", fake_run)
        return calls

    def test_retries_with_break_system_packages(self, monkeypatch: pytest.MonkeyPatch):
        calls = self._fake_runner(
            monkeypatch,
            [(1, "error: externally-managed-environment"), (0, "Successfully installed ghost")],
        )
        assert install.pip_install(["ghost>=1.0"], PROJECT_ROOT) is True
        assert len(calls) == 2
        assert "--break-system-packages" not in calls[0]
        assert "--break-system-packages" in calls[1]

    def test_no_retry_on_unrelated_failure(self, monkeypatch: pytest.MonkeyPatch):
        """非 PEP 668 的失败不重试——重试治不了网络不通，只会多等一轮。"""
        calls = self._fake_runner(monkeypatch, [(1, "Could not find a version that satisfies")])
        assert install.pip_install(["ghost>=1.0"], PROJECT_ROOT) is False
        assert len(calls) == 1

    def test_success_first_try_does_not_retry(self, monkeypatch: pytest.MonkeyPatch):
        calls = self._fake_runner(monkeypatch, [(0, "Successfully installed ghost")])
        assert install.pip_install(["ghost>=1.0"], PROJECT_ROOT) is True
        assert len(calls) == 1
        assert calls[0][:4] == [sys.executable, "-m", "pip", "install"]

    def test_editable_install_is_retried_too(self, monkeypatch: pytest.MonkeyPatch):
        """pip install -e . goes through the same retry: a fresh install on a PEP 668 Python needs it."""
        calls = self._fake_runner(
            monkeypatch,
            [(1, "error: externally-managed-environment"), (0, "Successfully installed ai-team-os")],
        )
        assert install.pip_install(["-e", "."], PROJECT_ROOT) is True
        assert calls[1][-3:] == ["--break-system-packages", "-e", "."]

    def test_failure_prints_the_last_line(self, monkeypatch: pytest.MonkeyPatch, capsys):
        self._fake_runner(monkeypatch, [(1, "Collecting ghost\nERROR: No matching distribution found for ghost")])
        assert install.pip_install(["ghost"], PROJECT_ROOT) is False
        assert "No matching distribution found for ghost" in capsys.readouterr().out


class TestPackageCheck:
    """The aiteam package itself: importable outside the checkout, from this checkout, at its version."""

    def _probe(self, monkeypatch: pytest.MonkeyPatch, result: dict | None):
        seen: dict = {}

        def fake_run(args, **kwargs):
            seen.update(kwargs)
            if result is None:
                return subprocess.CompletedProcess(args, 1, "", "ModuleNotFoundError: No module named 'aiteam'")
            return subprocess.CompletedProcess(args, 0, json.dumps(result) + "\n", "")

        monkeypatch.setattr(install.subprocess, "run", fake_run)
        return seen

    def test_probe_runs_outside_the_checkout_without_pythonpath(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PYTHONPATH", str(PROJECT_ROOT / "src"))
        seen = self._probe(monkeypatch, None)
        assert install.package_problem(PROJECT_ROOT) == "not importable"
        assert "PYTHONPATH" not in seen["env"]
        assert not Path(seen["cwd"]).resolve().is_relative_to(PROJECT_ROOT)

    def test_this_checkout_at_its_version_is_fine(self, monkeypatch: pytest.MonkeyPatch):
        self._probe(monkeypatch, {"file": str(PROJECT_ROOT / "src" / "aiteam" / "__init__.py"),
                                  "dist": install.source_version(PROJECT_ROOT)})
        assert install.package_problem(PROJECT_ROOT) is None

    def test_a_copy_elsewhere_is_a_problem(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        self._probe(monkeypatch, {"file": str(tmp_path / "site-packages" / "aiteam" / "__init__.py"),
                                  "dist": install.source_version(PROJECT_ROOT)})
        assert "not this checkout" in install.package_problem(PROJECT_ROOT)

    def test_a_stale_pip_record_is_a_problem(self, monkeypatch: pytest.MonkeyPatch):
        """An editable install recorded at 1.15.2 still loads the new code, but its entry points lag."""
        self._probe(monkeypatch, {"file": str(PROJECT_ROOT / "src" / "aiteam" / "__init__.py"), "dist": "1.15.2"})
        assert "1.15.2" in install.package_problem(PROJECT_ROOT)

    def test_a_problem_reinstalls_from_the_checkout(self, monkeypatch: pytest.MonkeyPatch):
        problems = iter(["not importable", None])
        monkeypatch.setattr(install, "package_problem", lambda root: next(problems))
        monkeypatch.setattr(install, "declared_dependencies", lambda root: [])
        calls: list[list[str]] = []
        monkeypatch.setattr(install, "pip_install", lambda args, root: calls.append(args) or True)
        assert install.verify_runtime_dependencies(PROJECT_ROOT) == []
        assert calls == [["-e", "."]]

    def test_a_package_that_stays_broken_is_reported(self, monkeypatch: pytest.MonkeyPatch, capsys):
        monkeypatch.setattr(install, "package_problem", lambda root: "not importable")
        monkeypatch.setattr(install, "declared_dependencies", lambda root: [])
        monkeypatch.setattr(install, "pip_install", lambda args, root: False)
        assert install.verify_runtime_dependencies(PROJECT_ROOT) == ["aiteam (this checkout)"]
        assert "--break-system-packages -e ." in capsys.readouterr().out
