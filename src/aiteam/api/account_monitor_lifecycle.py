"""Own independent usage recording and account monitoring in the OS API."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from aiteam.services.account_monitor import AccountMonitorRunner
from aiteam.services.codex_usage_recorder import CodexUsageRecorder
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.codex_usage_journal import CodexUsageJournalRepository


@asynccontextmanager
async def account_monitor_lifespan(app: FastAPI, db_url: str):
    repository = MonitorRepository(db_url)
    await repository.init_db()
    runner = AccountMonitorRunner(repository)
    recorder = CodexUsageRecorder(CodexUsageJournalRepository(db_url))
    app.state.account_monitor_runner = runner
    app.state.codex_usage_recorder = recorder
    try:
        try:
            await recorder.start()
        except Exception as error:
            recorder.last_error = type(error).__name__
            logging.getLogger(__name__).warning("Codex usage recorder startup failed (%s)", recorder.last_error)
        await runner.start()
        yield runner
    finally:
        try:
            await recorder.stop()
        finally:
            try:
                await runner.stop()
            finally:
                app.state.account_monitor_runner = None
                app.state.codex_usage_recorder = None
