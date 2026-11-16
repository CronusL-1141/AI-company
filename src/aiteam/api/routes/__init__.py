"""AI Team OS — API route aggregation."""

from fastapi import APIRouter

from aiteam.api.routes.account_usage import router as account_usage_router
from aiteam.api.routes.activities import router as activities_router
from aiteam.api.routes.agent_templates import router as agent_templates_router
from aiteam.api.routes.agents import router as agents_router
from aiteam.api.routes.agents_config import router as agents_config_router
from aiteam.api.routes.analytics import router as analytics_router
from aiteam.api.routes.assets import router as assets_router
from aiteam.api.routes.briefings import router as briefings_router
from aiteam.api.routes.channels import router as channels_router
from aiteam.api.routes.context import router as context_router
from aiteam.api.routes.decisions import router as decisions_router
from aiteam.api.routes.ecosystem import router as ecosystem_router
from aiteam.api.routes.events import router as events_router
from aiteam.api.routes.external_exec import router as external_exec_router
from aiteam.api.routes.feedback import router as feedback_router
from aiteam.api.routes.fleet import router as fleet_router
from aiteam.api.routes.health import router as health_router
from aiteam.api.routes.hooks import router as hooks_router
from aiteam.api.routes.links import router as links_router
from aiteam.api.routes.meetings import router as meetings_router
from aiteam.api.routes.memory import router as memory_router
from aiteam.api.routes.memory import (
    router_agents_memory,
    router_memories,
    router_teams_memory,
)
from aiteam.api.routes.memory_reconcile import router as memory_reconcile_router
from aiteam.api.routes.models import router as models_router
from aiteam.api.routes.notices import router as notices_router
from aiteam.api.routes.pricing import router as pricing_router
from aiteam.api.routes.projects import router as projects_router
from aiteam.api.routes.prompt_registry import router as prompt_registry_router
from aiteam.api.routes.reports import router as reports_router
from aiteam.api.routes.scheduler import router as scheduler_router
from aiteam.api.routes.search import router as search_router
from aiteam.api.routes.settings import router as settings_router
from aiteam.api.routes.sync import router as sync_router
from aiteam.api.routes.system import router as system_router
from aiteam.api.routes.task_memo import router as task_memo_router
from aiteam.api.routes.task_wall import router as task_wall_router
from aiteam.api.routes.tasks import router as tasks_router
from aiteam.api.routes.teams import router as teams_router
from aiteam.api.routes.templates import router as templates_router
from aiteam.api.routes.tools import router as tools_router
from aiteam.api.routes.usage import router as usage_router
from aiteam.api.routes.wake import router as wake_router
from aiteam.api.routes.watchdog import router as watchdog_router
from aiteam.api.routes.workflows import router as workflows_router
from aiteam.api.routes.ws import router as ws_router

# Inclusion order is matching order: a static path must come before a parameterised
# one that would also match it.
ROUTERS: tuple[APIRouter, ...] = (
    health_router,
    notices_router,
    context_router,
    projects_router,
    teams_router,
    agents_router,
    tasks_router,
    task_memo_router,
    events_router,
    decisions_router,
    meetings_router,
    activities_router,
    memory_router,
    router_memories,
    memory_reconcile_router,
    router_teams_memory,
    router_agents_memory,
    hooks_router,
    assets_router,
    feedback_router,
    task_wall_router,
    system_router,
    analytics_router,
    agent_templates_router,
    agents_config_router,
    templates_router,
    scheduler_router,
    ws_router,
    reports_router,
    links_router,
    search_router,
    models_router,
    briefings_router,
    prompt_registry_router,
    settings_router,
    channels_router,
    wake_router,
    fleet_router,
    watchdog_router,
    ecosystem_router,
    workflows_router,
    tools_router,
    usage_router,
    pricing_router,
    account_usage_router,
    external_exec_router,
    sync_router,
)

api_router = APIRouter()
for _router in ROUTERS:
    api_router.include_router(_router)
