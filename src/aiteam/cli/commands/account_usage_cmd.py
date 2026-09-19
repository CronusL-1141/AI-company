"""Export a local, explicitly scoped request sample as importable JSON."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from aiteam.services.codex_usage_export import CodexUsageExportError, export_codex_usage

app = typer.Typer(help="导出本地 Codex 逐请求用量样本（不绑定账号）", no_args_is_help=True)

_GAPS = {
    "missing_request_id": "缺少真实 response_id",
    "missing_model": "模型未知",
    "invalid_tokens": "逐请求 token 字段不完整或无效",
    "invalid_timestamp": "账本时间无效",
    "unidentified_token_count": "仅有 token_count、无法确定请求 ID 的事件",
    "unidentified_event_time": "token_count 时间无效",
    "invalid_json": "无法解析的记录",
    "oversized_records": "超过读取上限的记录",
    "symlinks_skipped": "未读取的符号链接",
}


@app.callback()
def account_usage() -> None:
    """只读导出本地账本，不保存账号或导入批次。"""


@app.command("export")
def export_usage(
    since: str = typer.Option(..., "--since", help="起点（不含），ISO 时间必须含时区"),
    until: str = typer.Option(..., "--until", help="终点（含），ISO 时间必须含时区"),
    service_tier: str = typer.Option(
        ..., "--service-tier", help="显式计价假设：standard/default/fast/priority/flex/batch",
    ),
    session_root: Path = typer.Option(
        Path("~/.codex/sessions"), "--session-root", help="只读扫描此目录下的 JSONL 会话",
    ),
) -> None:
    """首选 response_id + usage 账本；不导出正文，不从累计值推算请求。"""
    try:
        entries, counts = export_codex_usage(session_root, since, until, service_tier)
    except CodexUsageExportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except OSError as exc:
        typer.echo("读取本地会话失败；请检查目录和读取权限。", err=True)
        raise typer.Exit(1) from exc

    typer.echo(
        f"计价档位假设：{service_tier}。仅导出本地所选目录的 (since, until] 样本；"
        "不证明账号绑定、全部客户端覆盖或实际扣款。", err=True,
    )
    typer.echo(
        f"已导出 {len(entries)} 条可验证逐请求账本；"
        f"去重 {counts.get('duplicate_records', 0)} 条；"
        f"忽略 {counts.get('token_count_events_ignored', 0)} 条无请求 ID 的通知事件。", err=True,
    )
    has_gaps = False
    for key, label in _GAPS.items():
        if counts.get(key):
            has_gaps = True
            typer.echo(f"未完整识别：{label} = {counts[key]}。", err=True)
    if not entries:
        typer.echo("没有可导入的请求；不能由此断言区间用量为零。", err=True)
    typer.echo(json.dumps([entry.model_dump(mode="json") for entry in entries], ensure_ascii=False, indent=2))
    if has_gaps:
        raise typer.Exit(2)
