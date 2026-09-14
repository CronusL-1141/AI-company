"""Read-only catalog validation, explicit supplementation and reproducible quotes.

All results go to stdout. This command does not install catalogs, change accounts,
write the OS database, fetch remote prices, or start a background process.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import typer

from aiteam.services.pricing import catalog_digest, decode_pricing_json, load_catalog, quote_requests
from aiteam.types import PricingCatalog, PricingQuoteRequest

app = typer.Typer(help="API 等值价格目录、补价与重算（非订阅扣款）", no_args_is_help=True)


def _emit(data: dict) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _fail(exc: Exception) -> NoReturn:
    # Pydantic errors can contain the entire input. Keep diagnostics useful but bounded.
    typer.echo(f"价格处理失败（{type(exc).__name__}）；请检查路径、目录和请求契约。", err=True)
    raise typer.Exit(1) from exc


@app.command("catalog")
def show_catalog(catalog: Path | None = typer.Option(None, "--catalog", help="指定价格目录；默认内置")) -> None:
    """查看完整价格、来源、核验日期和摘要。"""
    try:
        data = load_catalog(catalog)
        _emit({"catalog": data.model_dump(mode="json"), "catalog_sha256": catalog_digest(data)})
    except (OSError, ValueError) as exc:
        _fail(exc)


@app.command("validate")
def validate_catalog(catalog: Path = typer.Option(..., "--catalog", help="待验证的完整目录")) -> None:
    """验证目录，不安装、不改任何已生效配置。"""
    try:
        data = load_catalog(catalog)
        _emit({
            "valid": True, "version": data.version, "catalog_sha256": catalog_digest(data),
            "models": len({row.model for row in data.records}), "records": len(data.records),
            "unpriced_models": [row.model for row in data.unpriced_models],
        })
    except (OSError, ValueError) as exc:
        _fail(exc)


@app.command("quote")
def quote_file(
    input_file: Path = typer.Option(..., "--input", help="逐请求 JSON 文件；显式填写 service_tier"),
    catalog: Path | None = typer.Option(None, "--catalog", help="固定目录重算；不使用历史金额缓存"),
) -> None:
    """按所选目录重新计算；未完全报价时返回已知小计并以退出码 2 提醒。"""
    try:
        with input_file.open("rb") as stream:
            raw = stream.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("request file too large")
        request = PricingQuoteRequest.model_validate(decode_pricing_json(raw))
        result = quote_requests(request, load_catalog(catalog))
        _emit(result.model_dump(mode="json"))
    except (OSError, ValueError) as exc:
        _fail(exc)
    if not result.complete:
        raise typer.Exit(2)


@app.command("supplement")
def supplement_catalog(
    supplement: Path = typer.Option(..., "--supplement", help="已核价的补充目录（与完整目录同一契约）"),
    version: str = typer.Option(..., "--version", help="合并后的新版本名；不得沿用输入版本"),
    catalog: Path | None = typer.Option(None, "--catalog", help="基准目录"),
) -> None:
    """输出新目录 JSON。按模型、档位和长度区间补齐；其他记录及核验时间保持。"""
    try:
        base = load_catalog(catalog)
        patch = load_catalog(supplement)
        if version in {base.version, patch.version} or not version.strip():
            raise ValueError("a new version is required")
        if patch.verified_at < base.verified_at:
            raise ValueError("supplement is older than the base")
        patch_models = {row.model for row in patch.records}
        if {row.model for row in patch.unpriced_models} & {row.model for row in base.records}:
            raise ValueError("supplement cannot remove existing priced models")
        data = base.model_dump(mode="json")
        data.update(version=version, verified_at=patch.model_dump(mode="json")["verified_at"])

        def key(row: dict) -> tuple:
            return row["model"], row["tier"], row["min_input_tokens"], row["max_input_tokens"]

        records = {key(row): row for row in data["records"]}
        originals = {key(row.model_dump(mode="json")): row for row in base.records}
        for row in patch.records:
            wire = row.model_dump(mode="json")
            old = originals.get(key(wire))
            if old is not None and row.verified_at < old.verified_at:
                raise ValueError("record verification cannot move backwards")
            records[key(wire)] = wire
        data["records"] = list(records.values())
        unpriced = {row["model"]: row for row in data["unpriced_models"] if row["model"] not in patch_models}
        unpriced.update({row.model: row.model_dump(mode="json") for row in patch.unpriced_models})
        data["unpriced_models"] = list(unpriced.values())
        for alias, target in patch.aliases.items():
            if alias in base.aliases and base.aliases[alias] != target:
                raise ValueError("supplement cannot silently reroute an existing alias")
            data["aliases"][alias] = target
        merged = PricingCatalog.model_validate(data)
        _emit(merged.model_dump(mode="json"))
    except (OSError, ValueError) as exc:
        _fail(exc)
