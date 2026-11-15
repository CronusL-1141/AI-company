"""The batch child speaks the stored state vocabulary without importing it."""

from __future__ import annotations

from aiteam.assets import process
from aiteam.types import AssetFileState


def test_batch_outcomes_are_file_states():
    assert process.STATUSES <= {state.value for state in AssetFileState}
    assert process.MISSING not in {state.value for state in AssetFileState}  # never stored
