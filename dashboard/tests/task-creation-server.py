"""Real API/SQLite fixture for task-creation browser acceptance.

Run only with an explicit disposable AITEAM_DB_PATH. No host hooks, usage
collection or background workers are started; route validation is unchanged.
"""

import os
from contextlib import asynccontextmanager

if not os.environ.get("AITEAM_DB_PATH"):
    raise RuntimeError("AITEAM_DB_PATH must point to a disposable test database")

import uvicorn

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.loop.task_wall_engine import TaskWallEngine
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@asynccontextmanager
async def lifespan(app):
    repo = StorageRepository()
    await repo.init_db()
    deps._repository = repo
    deps._memory_store = MemoryStore(repository=repo)
    deps._event_bus = EventBus(repo=repo)
    deps._manager = TeamManager(repository=repo, memory=deps._memory_store, event_bus=deps._event_bus)
    deps._hook_translator = HookTranslator(repo=repo, event_bus=deps._event_bus)
    deps._task_wall_engine = TaskWallEngine(repo=repo)
    await repo.create_project("No teams", root_path="/task-acceptance/empty")
    staffed = await repo.create_project("With teams", root_path="/task-acceptance/staffed")
    single = await repo.create_project("One team", root_path="/task-acceptance/single")
    await repo.create_team("Team A", "coordinate", project_id=staffed.id)
    await repo.create_team("Team B", "coordinate", project_id=staffed.id)
    await repo.create_team("Only team", "coordinate", project_id=single.id)
    try:
        yield
    finally:
        await close_db()


app = create_app()
app.router.lifespan_context = lifespan


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("TASK_ACCEPTANCE_PORT", "18082")))
