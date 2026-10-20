"""真实文件 SQLite、独立 HTTP 请求验证终态缺测不会抹掉 live CTX_LAST。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiteam.api import workflow_ingest
from aiteam.api.deps import get_event_bus, get_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.routes.workflows import router
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import WorkflowRun


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('reported', 'expected'),
    [(None, 104814), ('missing', 104814), (0, 0), ('17', 17)],
    ids=['null-preserves-live', 'missing-preserves-live', 'explicit-zero-overrides', 'numeric-overrides'],
)
async def test_terminal_progress_tokens_file_sqlite_requests(tmp_path, monkeypatch, reported, expected):
    repo = StorageRepository(db_url=f'sqlite+aiosqlite:///{tmp_path / "workflow.db"}')
    await repo.init_db()
    try:
        base = tmp_path / 'projects'
        session_dir = base / 'project' / 'session'
        wf_id = 'wf_null0001-abc'
        wf_dir = session_dir / 'subagents' / 'workflows' / wf_id
        wf_dir.mkdir(parents=True)
        (wf_dir / 'agent-agent1.jsonl').write_text(json.dumps({
            'type': 'assistant', 'timestamp': '2026-10-15T08:00:00Z',
            'message': {'usage': {'input_tokens': 100000, 'output_tokens': 4814}},
        }) + '\n')
        monkeypatch.setattr(workflow_ingest, '_claude_projects_dir', lambda: base)
        await repo.upsert_workflow_run(WorkflowRun(
            wf_id=wf_id, status='running', source='hook', session_id='session',
            transcript_dir=str(wf_dir),
        ))
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_repository] = lambda: repo
        app.dependency_overrides[get_event_bus] = lambda: EventBus(repo=repo)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/workflows/reconcile', json={})
            assert response.status_code == 200
            before = (await client.get(f'/api/workflows/{wf_id}/agents')).json()['data']
            assert before[0]['tokens'] == 104814  # 真正经 live tail 与一次独立读请求
            entry = {'type': 'workflow_agent', 'agentId': 'agent1', 'state': 'done'}
            if reported != 'missing':
                entry['tokens'] = reported
            terminal_dir = session_dir / 'workflows'
            terminal_dir.mkdir()
            terminal = terminal_dir / f'{wf_id}.json'
            terminal.write_text(json.dumps({
                'runId': wf_id, 'status': 'completed', 'workflowProgress': [entry],
            }))
            for attempt in range(2):
                # 改 fingerprint 强制重复摄取，验证幂等合并而非仅命中跳过。
                terminal.write_text(terminal.read_text() + '\n')
                response = await client.post('/api/workflows/reconcile', json={})
                assert response.status_code == 200
                after = (await client.get(f'/api/workflows/{wf_id}/agents')).json()['data']
                assert len(after) == 1
                assert after[0]['id'] == before[0]['id']
                assert after[0]['tokens'] == expected
                assert after[0]['state'] == 'done'
                assert (await client.get(f'/api/workflows/{wf_id}')).json()['status'] == 'completed'
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_first_null_progress_uses_existing_schema_default(tmp_path: Path):
    repo = StorageRepository(db_url=f'sqlite+aiosqlite:///{tmp_path / "new.db"}')
    await repo.init_db()
    try:
        await workflow_ingest._upsert_agents_from_progress(repo, 'wf_new', '', [
            {'type': 'workflow_agent', 'agentId': 'agent1', 'tokens': None},
        ])
        rows = await repo.list_workflow_agents('wf_new')
        assert len(rows) == 1 and rows[0].tokens == 0
    finally:
        await close_db()
