"""Production routes over real HTTP and independent CLI/spool processes."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest


@pytest.fixture
def live_api(tmp_path):
    helper = tmp_path / "api-helper.py"
    helper.write_text("""import asyncio,json,socket
from contextlib import asynccontextmanager
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete
import uvicorn
from aiteam.api.routes import external_exec as routes
from aiteam.storage.repository import StorageRepository
from aiteam.storage.external_exec import ExternalExecRepository
from aiteam.storage.connection import get_session
from aiteam.storage.models import ProjectModel
from aiteam.storage.engine_pool import engine_pool
import os
storage=StorageRepository("sqlite+aiosqlite:///"+os.environ["AITEAM_DB_PATH"])
external=ExternalExecRepository(storage._db_url)
state={"version":0}
@asynccontextmanager
async def lifespan(app):
 await storage.init_db()
 first=await storage.create_project("first",root_path="/fixture-first")
 other=await storage.create_project("other",root_path="/fixture-other")
 task=await storage.create_task(None,"fixture-task",project_id=first.id)
 state.update(project_id=first.id,other_project_id=other.id,task_id=task.id)
 yield
 await engine_pool.get_engine(storage._db_url).dispose()
app=FastAPI(lifespan=lifespan)
app.include_router(routes.router)
app.dependency_overrides[routes.get_repository]=lambda:storage
app.dependency_overrides[routes.get_external_exec_repository]=lambda:external
@app.middleware("http")
async def mismatch(request:Request,call_next):
 if state["version"] and request.url.path.startswith("/api/external-execs"):
  return JSONResponse({"detail":{"code":"version_mismatch","message":"PRIVATE-VERSION-MESSAGE"}},
                      status_code=state["version"])
 return await call_next(request)
@app.post("/fixture/version/{code}")
async def version(code:int):
 state["version"]=code
 return {"success":True}
@app.delete("/fixture/project/{identifier}")
async def remove(identifier:str):
 async with get_session(storage._db_url) as session:
  await session.execute(delete(ProjectModel).where(ProjectModel.id==identifier))
 return {"success":True}
async def main():
 listener=socket.socket();listener.bind(("127.0.0.1",0));listener.listen(16)
 state["port"]=listener.getsockname()[1]
 server=uvicorn.Server(uvicorn.Config(app,log_level="error",access_log=False))
 running=asyncio.create_task(server.serve(sockets=[listener]))
 while not server.started: await asyncio.sleep(.01)
 print(json.dumps(state),flush=True)
 await running
asyncio.run(main())
""")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).parents[3] / "src")
    environment["AITEAM_DB_PATH"] = str(tmp_path / "api.sqlite")
    process = subprocess.Popen(
        [sys.executable, str(helper)], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        metadata = json.loads(process.stdout.readline())
        with httpx.Client(base_url=f"http://127.0.0.1:{metadata['port']}", trust_env=False) as client:
            yield metadata, client, environment
    finally:
        process.terminate()
        try:
            process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)


def cli(environment, path, base, *arguments):
    return subprocess.run(
        [sys.executable, "-m", "aiteam.cli.app", "codex-exec", *arguments, "--spool-db", str(path), "--api-url", base],
        env=environment,
        capture_output=True,
        check=False,
        timeout=30,
    )


def run(environment, path, base, executable, project, task=None):
    arguments = [
        sys.executable,
        "-m",
        "aiteam.cli.app",
        "codex-exec",
        "run",
        "--spool-db",
        str(path),
        "--api-url",
        base,
        "--executable",
        str(executable),
        "--project-id",
        project,
    ]
    if task is not None:
        arguments.extend(["--task-id", task])
    return subprocess.run(
        [*arguments, "--", "PRIVATE-PROMPT"],
        env=environment,
        capture_output=True,
        check=False,
        timeout=30,
    )


def fake(tmp_path, *, before="", tokens=100):
    path, marker = tmp_path / "fake-codex", tmp_path / "launched"
    raw = (
        "\n".join(
            json.dumps(event)
            for event in [
                {"type": "thread.started", "thread_id": "delivery-thread"},
                {"type": "turn.started"},
                {"type": "turn.completed", "usage": {"input_tokens": tokens, "output_tokens": 5}},
            ]
        )
        + "\n"
    )
    path.write_text(
        f"#!{sys.executable}\nimport sys,urllib.request\n"
        f"open({str(marker)!r},'w').write('yes')\n{before}\n"
        f"sys.stdout.write({raw!r})\n"
    )
    path.chmod(0o700)
    return path, marker


def local(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        payload, revision, uploaded = db.execute(
            "SELECT payload,revision,uploaded_revision FROM external_exec_invocations"
        ).fetchone()
    return json.loads(payload), revision, uploaded


def test_pre_execution_business_rejection_is_permanent_and_replay_converges(live_api, tmp_path):
    _, http, environment = live_api
    executable, marker = fake(tmp_path)
    spool = tmp_path / "spool.sqlite"
    outcome = run(environment, spool, str(http.base_url), executable, str(uuid4()))
    assert outcome.returncode == 64 and not marker.exists()
    assert "项目不存在" in outcome.stderr.decode()
    assert local(spool)[0]["association_status"] == "rejected"
    for _ in range(2):
        replay = cli(environment, spool, str(http.base_url), "replay")
        assert replay.returncode == 0 and "未确认 0" in replay.stdout.decode()
    assert http.get("/api/external-execs").json()["data"]["total"] == 0


@pytest.mark.parametrize("fault", ["project_not_found", "task_not_found", "task_project_mismatch"])
def test_offline_execution_invalid_association_preserves_actual_usage_in_quarantine(live_api, tmp_path, fault):
    metadata, http, environment = live_api
    project, task = metadata["project_id"], metadata["task_id"]
    if fault == "project_not_found":
        project = str(uuid4())
    elif fault == "task_not_found":
        task = str(uuid4())
    else:
        project = metadata["other_project_id"]
    executable, marker = fake(tmp_path)
    spool = tmp_path / "spool.sqlite"
    outcome = run(environment, spool, "http://127.0.0.1:9", executable, project, task)
    assert outcome.returncode == 0 and marker.exists()
    first = cli(environment, spool, str(http.base_url), "replay")
    assert first.returncode == 64, first.stderr.decode()
    saved, revision, uploaded = local(spool)
    assert revision == uploaded and saved["association_status"] == "quarantined"
    remote = http.get(f"/api/external-execs/{saved['invocation_id']}").json()["data"]
    assert remote["project_id"] is None and remote["task_id"] is None
    assert remote["requested_project_id"] == project and remote["requested_task_id"] == task
    assert remote["association_reason"] == fault and remote["status"] == "completed"
    assert remote["receipts"]["turn:1"]["usage"]["input_tokens"] == 100
    again = cli(environment, spool, str(http.base_url), "replay")
    assert again.returncode == 0 and "未确认 0" in again.stdout.decode()
    assert http.get(f"/api/external-execs/{saved['invocation_id']}").json()["data"]["revision"] == remote["revision"]


def test_existing_exact_registration_can_finish_after_project_deletion(live_api, tmp_path):
    metadata, http, environment = live_api
    url = str(http.base_url).rstrip("/") + "/fixture/project/" + metadata["project_id"]
    request = (
        "opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
        f"opener.open(urllib.request.Request({url!r},method='DELETE')).read()"
    )
    executable, _ = fake(tmp_path, before=request, tokens=777)
    spool = tmp_path / "spool.sqlite"
    result = run(environment, spool, str(http.base_url), executable, metadata["project_id"], metadata["task_id"])
    assert result.returncode == 0, result.stderr.decode()
    saved, revision, uploaded = local(spool)
    assert uploaded == revision
    remote = http.get(f"/api/external-execs/{saved['invocation_id']}").json()["data"]
    assert remote["association_status"] == "accepted" and remote["project_id"] == metadata["project_id"]
    assert remote["status"] == "completed" and remote["receipts"]["turn:1"]["usage"]["input_tokens"] == 777
    assert cli(environment, spool, str(http.base_url), "replay").returncode == 0
    different = {
        key: saved[key]
        for key in (
            "invocation_id",
            "project_id",
            "task_id",
            "caller_harness",
            "caller_session_id",
            "callee_harness",
            "source_namespace",
            "ephemeral",
            "started_at",
        )
    }
    different["invocation_id"] = str(uuid4())
    assert http.post("/api/external-execs", json=different).status_code == 404


@pytest.mark.parametrize("status", [405, 422])
def test_version_mismatch_does_not_block_exec_and_later_real_replay_works(live_api, tmp_path, status):
    metadata, http, environment = live_api
    http.post(f"/fixture/version/{status}")
    executable, marker = fake(tmp_path)
    spool = tmp_path / "spool.sqlite"
    result = run(environment, spool, str(http.base_url), executable, metadata["project_id"])
    assert result.returncode == 0 and marker.exists()
    assert "PRIVATE-VERSION-MESSAGE" not in result.stderr.decode()
    assert "api_unavailable" in local(spool)[0]["diagnostics"]
    http.post("/fixture/version/0")
    assert cli(environment, spool, str(http.base_url), "replay").returncode == 0
    saved = http.get("/api/external-execs").json()["data"]["items"][0]
    assert saved["status"] == "completed" and saved["receipts_count"] == 1
