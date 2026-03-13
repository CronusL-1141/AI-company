"""AI Team OS — API路由汇总."""

from fastapi import APIRouter

from aiteam.api.routes.activities import router as activities_router
from aiteam.api.routes.agents import router as agents_router
from aiteam.api.routes.events import router as events_router
from aiteam.api.routes.hooks import router as hooks_router
from aiteam.api.routes.meetings import router as meetings_router
from aiteam.api.routes.memory import router as memory_router
from aiteam.api.routes.tasks import router as tasks_router
from aiteam.api.routes.teams import router as teams_router
from aiteam.api.routes.ws import router as ws_router

api_router = APIRouter()
api_router.include_router(teams_router)
api_router.include_router(agents_router)
api_router.include_router(tasks_router)
api_router.include_router(events_router)
api_router.include_router(meetings_router)
api_router.include_router(activities_router)
api_router.include_router(memory_router)
api_router.include_router(hooks_router)
api_router.include_router(ws_router)
