"""AI Team OS — Context recovery management.

Provides checkpoint creation, restoration, and cleanup for recovering agent state
when context is exhausted.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4


class ContextRecovery:
    """Recovery mechanism for context exhaustion.

    Saves agent state snapshots as JSON files, supporting checkpoint-based recovery.
    """

    def __init__(self, checkpoint_dir: Path | None = None) -> None:
        self._checkpoint_dir = checkpoint_dir or Path(".aiteam/checkpoints")

    async def create_checkpoint(self, agent_id: str, state: dict) -> str:
        """Create a checkpoint, saving a state snapshot as a JSON file.

        Args:
            agent_id: The agent's ID.
            state: State dictionary to save.

        Returns:
            checkpoint_id.
        """
        checkpoint_id = str(uuid4())
        timestamp = datetime.now().isoformat()

        checkpoint_data = {
            "checkpoint_id": checkpoint_id,
            "agent_id": agent_id,
            "state": state,
            "timestamp": timestamp,
        }

        # Ensure directory exists
        agent_dir = self._checkpoint_dir / agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)

        file_path = agent_dir / f"{checkpoint_id}.json"
        file_path.write_text(
            json.dumps(checkpoint_data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        return checkpoint_id

    async def restore_checkpoint(self, checkpoint_id: str) -> dict:
        """Restore checkpoint state from a JSON file.

        Args:
            checkpoint_id: ID of the checkpoint to restore.

        Returns:
            The restored state dictionary.

        Raises:
            FileNotFoundError: Checkpoint file does not exist.
        """
        # Iterate all agent directories to find the checkpoint file
        if not self._checkpoint_dir.exists():
            msg = f"检查点 {checkpoint_id} 不存在"
            raise FileNotFoundError(msg)

        for agent_dir in self._checkpoint_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            file_path = agent_dir / f"{checkpoint_id}.json"
            if file_path.exists():
                data = json.loads(file_path.read_text(encoding="utf-8"))
                return data

        msg = f"检查点 {checkpoint_id} 不存在"
        raise FileNotFoundError(msg)

    async def list_checkpoints(self, agent_id: str) -> list[dict]:
        """List all checkpoints for an agent (sorted by time).

        Args:
            agent_id: The agent's ID.

        Returns:
            List of checkpoint info, sorted ascending by time.
        """
        agent_dir = self._checkpoint_dir / agent_id
        if not agent_dir.exists():
            return []

        checkpoints: list[dict] = []
        for file_path in agent_dir.glob("*.json"):
            data = json.loads(file_path.read_text(encoding="utf-8"))
            checkpoints.append({
                "checkpoint_id": data["checkpoint_id"],
                "agent_id": data["agent_id"],
                "timestamp": data["timestamp"],
            })

        # Sort ascending by time
        checkpoints.sort(key=lambda x: x["timestamp"])
        return checkpoints

    async def cleanup_old_checkpoints(
        self, agent_id: str, keep_latest: int = 5
    ) -> int:
        """Keep only the latest N checkpoints and delete older ones.

        Args:
            agent_id: The agent's ID.
            keep_latest: Number of latest checkpoints to keep.

        Returns:
            Number of deleted checkpoints.
        """
        agent_dir = self._checkpoint_dir / agent_id
        if not agent_dir.exists():
            return 0

        # Read all checkpoints and sort by time
        files_with_time: list[tuple[str, Path]] = []
        for file_path in agent_dir.glob("*.json"):
            data = json.loads(file_path.read_text(encoding="utf-8"))
            files_with_time.append((data.get("timestamp", ""), file_path))

        # Sort descending by time (newest first)
        files_with_time.sort(key=lambda x: x[0], reverse=True)

        # Delete checkpoints exceeding the retention count
        deleted = 0
        for _, file_path in files_with_time[keep_latest:]:
            file_path.unlink()
            deleted += 1

        return deleted
