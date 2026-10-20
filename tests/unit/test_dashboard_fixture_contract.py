"""Dashboard node-test fixtures must stay inside the production API contract.

The node tests under dashboard/tests render these JSON files without Python;
this side holds every row to the model the API serializes, so the frontend
cannot pass on rows the backend would never return.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest
from pydantic import BaseModel

from aiteam.types import CodexExecLaunchObservation, PricingPlanCapacityEstimate

FIXTURES = Path(__file__).resolve().parents[2] / "dashboard/tests/fixtures"
LAUNCHES = FIXTURES / "codex-launch-observations.json"
PLANS = FIXTURES / "pricing-plan-estimates.json"


def _load(path: Path) -> list[tuple[str, dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = list(data.items()) if isinstance(data, dict) else [(f"row{index}", row) for index, row in enumerate(data)]
    assert rows, f"{path.name} must not be empty"
    return rows


CASES = [(LAUNCHES.name, name, row, CodexExecLaunchObservation) for name, row in _load(LAUNCHES)] + [
    (PLANS.name, name, row, PricingPlanCapacityEstimate) for name, row in _load(PLANS)
]


@pytest.mark.parametrize(("file", "name", "row", "model"), CASES, ids=[f"{case[0]}:{case[1]}" for case in CASES])
def test_fixture_row_matches_api_serialization(file: str, name: str, row: dict, model: type[BaseModel]):
    # Both routes return model_dump(mode="json"); an exact round trip pins the
    # accepted values and the full field set the frontend receives.
    assert model.model_validate(row).model_dump(mode="json") == row


def test_launch_fixture_covers_every_state_mode_and_binding_reason():
    rows = [row for _, row in _load(LAUNCHES)]
    fields = CodexExecLaunchObservation.model_fields
    assert {row["state"] for row in rows} == set(get_args(fields["state"].annotation))
    assert {row["exec_mode"] for row in rows} == set(get_args(fields["exec_mode"].annotation))
    assert {row["binding_reason"] for row in rows} == set(get_args(fields["binding_reason"].annotation))
    assert len({row["launch_id"] for row in rows}) == len(rows)


def test_plan_fixture_covers_every_price_verification_status():
    signals = [signal for _, row in _load(PLANS) for signal in row["price_verification"]]
    signal_model = get_args(PricingPlanCapacityEstimate.model_fields["price_verification"].annotation)[0]
    assert {signal["status"] for signal in signals} == set(get_args(signal_model.model_fields["status"].annotation))
    assert any(signal["missing_price"] and signal["status"] != "unpriced" for signal in signals)
    assert any(signal["canonical_model"] not in (None, signal["model"]) for signal in signals)
