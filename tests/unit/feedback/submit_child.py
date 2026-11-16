"""Publish one confirmed draft, as the API would, in a process the test kills mid-send.

Usage: submit_child.py <database> <data dir> <draft id> <content hash> <repository>
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from aiteam.feedback.github import Timeouts
from aiteam.feedback.service import Caller, FeedbackService
from aiteam.storage.feedback import FeedbackRepository
from aiteam.types import FeedbackConfirmMethod, FeedbackSubmitRequest


async def main(database: str, data_dir: str, draft_id: str, content_hash: str, repo: str) -> None:
    async def projects() -> list[tuple[str, str]]:
        return []

    async def emit(*args, **kwargs) -> None:
        return None

    service = FeedbackService(FeedbackRepository(f"sqlite+aiosqlite:///{database}"), projects=projects, emit=emit,
                              data_dir=Path(data_dir), target_repo=repo,
                              timeouts=Timeouts(login=120, lookup=120, search=120, send=120))
    request = FeedbackSubmitRequest(confirm_method=FeedbackConfirmMethod.ELICITATION, action="accept",
                                    content_hash=content_hash)
    await service.submit(draft_id, request, Caller(session_id="session-killed"))


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
