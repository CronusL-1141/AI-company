"""Run the real document checker against bilingual INSTALL count mutations."""

from __future__ import annotations

import re

import pytest

from .test_readme_rule_count import _run
from .test_readme_rule_count import scratch_repo as scratch_repo

ANCHOR = "<!-- install:mcp-tool-count -->"


def test_install_full_count_is_independent_of_configured_subset(scratch_repo, monkeypatch):
    monkeypatch.setenv("AITEAM_TOOLSETS", "default")
    monkeypatch.setenv("AITEAM_READONLY", "1")
    proc = _run(scratch_repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "INSTALL 数字机检通过（双语）" in proc.stdout


@pytest.mark.parametrize("name", ["INSTALL.md", "INSTALL.zh-CN.md"], ids=["en", "zh"])
@pytest.mark.parametrize(
    "mutation",
    ["wrong-count", "missing-anchor", "missing-count", "duplicate-anchor"],
)
def test_install_count_drift_fails_real_checker(scratch_repo, name, mutation):
    path = scratch_repo / name
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if ANCHOR in line]
    assert len(lines) == 1
    original = lines[0]
    if mutation == "wrong-count":
        replacement = re.sub(r"\d+", "999999", original)
    elif mutation == "missing-anchor":
        replacement = original.replace(ANCHOR, "")
    elif mutation == "missing-count":
        replacement = original.replace("MCP tools", "tools").replace("MCP 工具", "工具")
    else:
        replacement = original + " " + ANCHOR
    assert replacement != original
    path.write_text(text.replace(original, replacement, 1), encoding="utf-8")

    proc = _run(scratch_repo)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"{name}:" in proc.stdout
    assert "安装工具数声明" in proc.stdout
