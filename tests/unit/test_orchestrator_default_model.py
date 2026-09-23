"""LangGraph 编排路径的型号字面量只允许出现在 DEFAULT_LLM_MODEL 一处."""

from __future__ import annotations

import re
from pathlib import Path

ORCHESTRATOR = Path(__file__).resolve().parents[2] / "src" / "aiteam" / "orchestrator"
MODEL_LITERAL = re.compile(r"""["']claude-[a-z0-9-]+["']""")


def test_model_id_literal_lives_in_one_constant() -> None:
    hits = [
        f"{path.relative_to(ORCHESTRATOR)}:{lineno}"
        for path in sorted(ORCHESTRATOR.rglob("*.py"))
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if MODEL_LITERAL.search(line)
    ]
    assert len(hits) == 1 and hits[0].startswith("nodes/__init__.py:"), hits
    source = (ORCHESTRATOR / "nodes" / "__init__.py").read_text(encoding="utf-8")
    assert re.search(r"^DEFAULT_LLM_MODEL = [\"']claude-", source, re.M)
