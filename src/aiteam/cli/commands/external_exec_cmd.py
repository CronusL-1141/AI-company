"""Opt-in Codex exec collection; no global configuration or hook installation."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from uuid import uuid4

import click
import httpx
import typer
from sqlalchemy.engine import make_url

from aiteam.services.codex_usage_journal import source_namespace
from aiteam.services.external_exec import ExternalExecAssociationError, run_external_exec, upload_invocation
from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.external_exec import ExternalExecRepository
from aiteam.types import ExternalExecFinish, ExternalExecRegistration

app = typer.Typer(help="显式采集外部 Codex exec 最小用量；stdout 为原始 JSONL", no_args_is_help=True)


def _warning(message: str) -> None:
    typer.echo(message, err=True)


def _reason(reason: str | None) -> str:
    return {
        "project_not_found": "项目不存在",
        "task_not_found": "任务不存在",
        "task_project_mismatch": "任务不属于指定项目",
    }.get(reason, "关联校验失败")


def _mkdir_private(path: Path) -> None:
    if path.is_dir():
        return
    _mkdir_private(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise


def _spool_path(path: Path | None) -> Path:
    if path is None:
        path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "data/ai-team-os/external-exec.db"
    path = path.expanduser().absolute()
    # EnginePool accepts URL strings. A literal ? would be reparsed as URL query
    # and redirect SQLite to a different file from the one vetted below. Reject
    # non-roundtripping paths before creating directories/files or running Codex.
    url = make_url(f"sqlite+aiosqlite:///{path}")
    if url.database != str(path) or url.query:
        raise ValueError("spool_path_not_losslessly_representable")
    _mkdir_private(path.parent)
    # An existing symlink/device must never redirect the minimal private ledger.
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("spool_not_regular_file")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return path


def _api_url(value: str | None) -> str:
    value = value or os.environ.get("AITEAM_API_URL") or "http://127.0.0.1:8000"
    url = httpx.URL(value)
    if url.scheme not in ("http", "https") or not url.host or url.username or url.password or url.query:
        raise ValueError("api_url_must_not_include_credentials")
    return value.rstrip("/")


@app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    context: typer.Context,
    project_id: str | None = typer.Option(None, "--project-id", help="明确项目 UUID；不按 cwd 猜测"),
    task_id: str | None = typer.Option(None, "--task-id", help="同项目任务 UUID"),
    caller_harness: str = typer.Option("unknown", "--caller-harness", help="claude-code/codex/other/unknown"),
    caller_session_id: str | None = typer.Option(None, "--caller-session-id"),
    spool_db: Path | None = typer.Option(None, "--spool-db", help="独立本地 SQLite 最小审计账"),
    api_url: str | None = typer.Option(None, "--api-url", help="OS API；不可用时执行仍可完成并留待重放"),
    executable: str = typer.Option("codex", "--executable", help="实际 Codex CLI 可执行文件"),
) -> None:
    """将原 exec 参数放在 -- 后；强制 --json，stdout 原样 JSONL。

    例如 aiteam codex-exec run --caller-harness claude-code -- -s read-only "任务" < /dev/null。
    带 --ephemeral 时 Codex 不留会话日志，这次用量只在回执里。
    prompt、stdin、工具输出和完整 stdout 不进入审计数据库。
    """
    arguments = list(context.args)
    # Click consumes the wrapper delimiter; a remaining -- belongs to Codex.
    option_end = arguments.index("--") if "--" in arguments else len(arguments)
    try:
        registration = ExternalExecRegistration(
            invocation_id=str(uuid4()),
            project_id=project_id,
            task_id=task_id,
            caller_harness=caller_harness,
            caller_session_id=caller_session_id,
            source_namespace=source_namespace(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")),
            ephemeral="--ephemeral" in arguments[:option_end],
        )
        path, base_url = _spool_path(spool_db), _api_url(api_url)
    except (ValueError, OSError):
        _warning("登记元数据或本地审计账无效；尚未启动 Codex。")
        raise typer.Exit(64) from None

    async def execute() -> int:
        repository = ExternalExecRepository(f"sqlite+aiosqlite:///{path}")
        try:
            await repository.init_db()
            await repository.register(registration)
            _warning(f"外部执行 {registration.invocation_id}；stdout=原始 JSONL，用量账不含正文。")
            async with httpx.AsyncClient(base_url=base_url, timeout=5, trust_env=False) as client:
                try:
                    await upload_invocation(repository, registration.invocation_id, client, allow_quarantine=False)
                except ExternalExecAssociationError as error:
                    await repository.finish(
                        registration.invocation_id, ExternalExecFinish(status="failed", exit_code=64)
                    )
                    await repository.association(registration.invocation_id, "rejected", error.reason)
                    _warning(f"OS 拒绝登记：{_reason(error.reason)}；尚未启动 Codex，已隔离本地且不再重试。")
                    return 64
                except httpx.HTTPError:
                    await repository.diagnostic(registration.invocation_id, "api_unavailable")
                    _warning("OS API 暂不可用；执行继续，关联待校验，最小账据将保留待重放。")
                if registration.ephemeral:
                    _warning(
                        "带了 --ephemeral：Codex 不留会话日志，这次用量只在回执里，进不了用量页和 /usage；"
                        "不需要时去掉它，后台运行加 < /dev/null 防止等待输入。"
                    )
                code = await run_external_exec(
                    repository,
                    registration,
                    arguments,
                    executable=executable,
                    stdout=click.get_binary_stream("stdout"),
                    warn=_warning,
                )
                try:
                    await upload_invocation(repository, registration.invocation_id, client)
                    current = await repository.get(registration.invocation_id)
                    if current.association_status == "quarantined":
                        _warning(
                            f"OS 已隔离未关联用量：{_reason(current.association_reason)}；真实执行退出码保持不变。"
                        )
                except httpx.HTTPError:
                    _warning("执行结果已留本地；OS 尚未确认最终账据，请运行 codex-exec replay。")
                return code
        finally:
            await engine_pool.get_engine(repository._db_url).dispose()

    raise typer.Exit(asyncio.run(execute()))


@app.command("replay")
def replay(
    spool_db: Path | None = typer.Option(None, "--spool-db"),
    api_url: str | None = typer.Option(None, "--api-url"),
    limit: int = typer.Option(100, "--limit", min=1, max=10000),
) -> None:
    """重放已持久化的最小账据；未确认的账据不删除，不恢复执行 prompt。"""
    try:
        path, base_url = _spool_path(spool_db), _api_url(api_url)
    except (ValueError, OSError):
        _warning("审计账路径或 API 地址无效。")
        raise typer.Exit(64) from None

    async def upload() -> tuple[int, int, int, int]:
        repository = ExternalExecRepository(f"sqlite+aiosqlite:///{path}")
        try:
            await repository.init_db()
            items, total = await repository.list(limit=limit, pending_only=True)
            succeeded = failed = quarantined = 0
            async with httpx.AsyncClient(base_url=base_url, timeout=5, trust_env=False) as client:
                for item in items:
                    try:
                        succeeded += int(await upload_invocation(repository, item.invocation_id, client))
                        current = await repository.get(item.invocation_id)
                        if current.association_status == "quarantined":
                            quarantined += 1
                            _warning(
                                f"账据 {item.invocation_id} 已确认隔离：{_reason(current.association_reason)}；"
                                "不再重试关联。"
                            )
                    except httpx.HTTPError:
                        failed += 1
                        _warning(f"账据 {item.invocation_id} 未获确认，已保留。")
            return succeeded, failed, total - len(items), quarantined
        finally:
            await engine_pool.get_engine(repository._db_url).dispose()

    succeeded, failed, remaining, quarantined = asyncio.run(upload())
    typer.echo(
        f"已确认 {succeeded}，隔离 {quarantined}，未确认 {failed}，本批上限外 {remaining}；"
        "永久拒绝已排除重试，审计原账保留。"
    )
    if failed or remaining:
        raise typer.Exit(2)
    if quarantined:
        raise typer.Exit(64)
