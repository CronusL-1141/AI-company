"""AI Team OS — API route aggregation."""

from fastapi import APIRouter

from aiteam.api.routes.activities import router as activities_router
from aiteam.api.routes.briefings import router as briefings_router
from aiteam.api.routes.agent_templates import router as agent_templates_router
from aiteam.api.routes.agents_config import router as agents_config_router
from aiteam.api.routes.agents import router as agents_router
from aiteam.api.routes.analytics import router as analytics_router
from aiteam.api.routes.cross_messages import router as cross_messages_router
from aiteam.api.routes.decisions import router as decisions_router
from aiteam.api.routes.events import router as events_router
from aiteam.api.routes.health import router as health_router
from aiteam.api.routes.hooks import router as hooks_router
from aiteam.api.routes.loop import router as loop_router
from aiteam.api.routes.meetings import router as meetings_router
from aiteam.api.routes.memory import router as memory_router
from aiteam.api.routes.memory import router_agents_memory, router_teams_memory
from aiteam.api.routes.pipeline import router as pipeline_router
from aiteam.api.routes.projects import router as projects_router
from aiteam.api.routes.reports import router as reports_router
from aiteam.api.routes.scheduler import router as scheduler_router
from aiteam.api.routes.system import router as system_router
from aiteam.api.routes.task_memo import router as task_memo_router
from aiteam.api.routes.task_wall import router as task_wall_router
from aiteam.api.routes.tasks import router as tasks_router
from aiteam.api.routes.team_config import router as team_config_router
from aiteam.api.routes.teams import router as teams_router
from aiteam.api.routes.templates import router as templates_router
from aiteam.api.routes.ws import router as ws_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(projects_router)
api_router.include_router(teams_router)
api_router.include_router(agents_router)
api_router.include_router(tasks_router)
api_router.include_router(task_memo_router)
api_router.include_router(events_router)
api_router.include_router(decisions_router)
api_router.include_router(meetings_router)
api_router.include_router(activities_router)
api_router.include_router(memory_router)
api_router.include_router(router_teams_memory)
api_router.include_router(router_agents_memory)
api_router.include_router(hooks_router)
api_router.include_router(loop_router)
api_router.include_router(task_wall_router)
api_router.include_router(system_router)
api_router.include_router(analytics_router)
api_router.include_router(team_config_router)
api_router.include_router(agent_templates_router)
api_router.include_router(agents_config_router)
api_router.include_router(templates_router)
api_router.include_router(scheduler_router)
api_router.include_router(ws_router)
api_router.include_router(cross_messages_router)
api_router.include_router(pipeline_router)
api_router.include_router(reports_router)
api_router.include_router(briefings_router)
