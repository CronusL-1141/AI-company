"""Independent API-equivalent pricing; never reads usage databases or credentials."""

from fastapi import APIRouter, Depends, HTTPException, Request

from aiteam.services.pricing import catalog_digest, decode_pricing_json, load_catalog, quote_requests
from aiteam.types import PricingCatalog, PricingQuoteRequest

router = APIRouter(prefix="/api/pricing", tags=["pricing"])


def _catalog() -> PricingCatalog:
    try:
        return load_catalog()
    except (OSError, ValueError) as exc:
        # Do not leak server paths or a caller-supplied malformed catalog body.
        raise HTTPException(503, detail="价格目录不可用；请校验已配置目录后重试") from exc


@router.get("/catalog")
def get_catalog() -> dict:
    """Return the active rate card and its content identity, without refreshing remotely."""
    catalog = _catalog()
    return {
        "success": True,
        "data": {"catalog": catalog.model_dump(mode="json"), "catalog_sha256": catalog_digest(catalog)},
    }


async def _validate_json_keys(request: Request) -> None:
    try:
        decode_pricing_json(await request.body())
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, detail="报价 JSON 无效或包含重复字段；请逐项保留所有请求") from exc


@router.post("/quote", dependencies=[Depends(_validate_json_keys)])
def quote(request: PricingQuoteRequest) -> dict:
    """Recompute submitted requests against the active catalog; no ledger writes."""
    result = quote_requests(request, _catalog())
    return {"success": True, "data": result.model_dump(mode="json")}
