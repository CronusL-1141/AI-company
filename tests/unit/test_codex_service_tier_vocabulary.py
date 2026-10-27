"""The Codex service-tier vocabulary is defined once in aiteam.types and reused everywhere."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from aiteam.services.codex_exec_launch_parser import wrapper_launch_metadata
from aiteam.services.codex_usage_export import CodexUsageExportError, export_codex_usage
from aiteam.types import (
    CODEX_SERVICE_TIERS,
    CodexExecLaunchObservation,
    CodexLaunchTierEvidence,
    CodexServiceTier,
    ExternalExecRegistration,
)

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src/aiteam", "scripts", "plugin")
# Words only the full request vocabulary carries: the priced-tier and speed-mode subsets lack aliases.
MARKERS = frozenset({"default", "priority", "flex", "batch"})
START, END = "2026-09-14T01:00:00Z", "2026-09-14T02:00:00Z"


def _owner(statement: ast.stmt) -> str:
    if isinstance(statement, ast.Assign) and isinstance(statement.targets[0], ast.Name):
        return statement.targets[0].id
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return statement.target.id
    return getattr(statement, "name", type(statement).__name__)


def _hand_written_vocabularies() -> list[tuple[str, str, int]]:
    hits = []
    for base in SCANNED:
        for path in sorted((ROOT / base).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for statement in tree.body:
                for node in ast.walk(statement):
                    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
                        words = {item.value for item in node.elts
                                 if isinstance(item, ast.Constant) and isinstance(item.value, str)}
                    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                        words = set(re.findall(r"[a-z]+", node.value))
                    else:
                        continue
                    if MARKERS <= words:
                        hits.append((path.relative_to(ROOT).as_posix(), _owner(statement), node.lineno))
    return hits


def test_tier_vocabulary_is_written_out_only_once():
    hits = _hand_written_vocabularies()
    assert [(path, owner) for path, owner, _ in hits] == [("src/aiteam/types.py", "CodexServiceTier")], hits


def test_shared_set_matches_the_literal():
    assert CODEX_SERVICE_TIERS == frozenset(get_args(CodexServiceTier))
    assert "ultrafast" in CODEX_SERVICE_TIERS


@pytest.mark.parametrize("tier", [*get_args(CodexServiceTier), "future-tier"])
def test_every_input_surface_accepts_exactly_the_shared_vocabulary(tmp_path, tier):
    known = tier in CODEX_SERVICE_TIERS
    metadata = wrapper_launch_metadata(["-c", f'service_tier="{tier}"'])
    assert metadata["requested_service_tier"] == (tier if known else None)
    for model in (ExternalExecRegistration, CodexExecLaunchObservation, CodexLaunchTierEvidence):
        adapter = TypeAdapter(model.model_fields["requested_service_tier"].annotation)
        if known:
            assert adapter.validate_python(tier) == tier
        else:
            with pytest.raises(ValidationError):
                adapter.validate_python(tier)
    if known:
        entries, _ = export_codex_usage(tmp_path, START, END, tier)
        assert entries == []
    else:
        with pytest.raises(CodexUsageExportError, match="service-tier"):
            export_codex_usage(tmp_path, START, END, tier)
