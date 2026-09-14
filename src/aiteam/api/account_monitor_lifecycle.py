"""Own the opt-in account monitor inside the OS API lifespan, not the AI host."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from aiteam.services.account_monitor import AccountMonitorRunner
from aiteam.storage.account_monitor import MonitorRepository


@asynccontextmanager
async def account_monitor_lifespan(app: FastAPI, db_url: str):
    repository = MonitorRepository(db_url)
    await repository.init_db()
    runner = AccountMonitorRunner(repository)
    app.state.account_monitor_runner = runner
    try:
        await runner.start()
        yield runner
    finally:
        try:
            await runner.stop()
        finally:
            app.state.account_monitor_runner = None
