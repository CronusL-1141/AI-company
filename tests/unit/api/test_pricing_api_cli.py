"""Pricing boundary tests use no OS database, account, or network."""

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from aiteam.api.routes import pricing
from aiteam.cli.commands.pricing_cmd import app as cli
from aiteam.services.pricing import catalog_digest, load_catalog


def request(model="gpt-6-astra"):
    return {"requests": [{
        "request_id": "one", "model": model, "service_tier": "standard",
        "input_tokens": 100, "cached_input_tokens": 60,
        "cache_write_input_tokens": 10, "output_tokens": 20,
    }]}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("AITEAM_PRICING_CATALOG", raising=False)
    app = FastAPI()
    app.include_router(pricing.router)
    with TestClient(app) as connection:
        yield connection


def test_catalog_has_provenance_and_digest(client):
    data = client.get("/api/pricing/catalog").json()["data"]
    assert data["catalog_sha256"] == catalog_digest(load_catalog())
    assert data["catalog"]["currency"] == "USD"
    assert all(row["source_url"].startswith("https://") for row in data["catalog"]["records"])


def test_quote_exact_decimal_and_missing_total(client):
    result = client.post("/api/pricing/quote", json=request()).json()["data"]
    assert result["complete"]
    assert Decimal(result["total_usd"]) == Decimal("0.001485")
    missing = client.post("/api/pricing/quote", json=request("unknown-model")).json()["data"]
    assert not missing["complete"]
    assert missing["total_usd"] is None
    assert missing["unpriced_request_count"] == 1


@pytest.mark.parametrize("change", [
    {"input_tokens": True}, {"input_tokens": -1}, {"input_tokens": 1.5},
    {"cached_input_tokens": 101}, {"service_tier": ""}, {"service_tier": "  "},
    pytest.param({"service_tier": "x" * 257}, id="tier-over-256-chars"),
])
def test_invalid_entire_request_rejected(client, change):
    payload = request()
    payload["requests"][0].update(change)
    assert client.post("/api/pricing/quote", json=payload).status_code == 422


def test_tier_cannot_be_silently_assumed(client):
    payload = request()
    del payload["requests"][0]["service_tier"]
    assert client.post("/api/pricing/quote", json=payload).status_code == 422


def test_catalog_errors_are_redacted(client, monkeypatch):
    monkeypatch.setenv("AITEAM_PRICING_CATALOG", "/not-present/private-account-secret/catalog.json")
    response = client.get("/api/pricing/catalog")
    assert response.status_code == 503
    assert "private-account-secret" not in response.text


def test_cli_matches_api_and_does_not_write(client, tmp_path):
    path = tmp_path / "requests.json"
    path.write_text(json.dumps(request()))
    before = path.read_bytes()
    result = CliRunner().invoke(cli, ["quote", "--input", str(path)])
    assert result.exit_code == 0, result.output
    quoted = json.loads(result.stdout)
    assert quoted == client.post("/api/pricing/quote", json=request()).json()["data"]
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_cli_unpriced_exit_two(tmp_path, monkeypatch):
    monkeypatch.delenv("AITEAM_PRICING_CATALOG", raising=False)
    path = tmp_path / "requests.json"
    path.write_text(json.dumps(request("new-model")))
    result = CliRunner().invoke(cli, ["quote", "--input", str(path)])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["total_usd"] is None


def test_duplicate_json_keys_are_not_silent_data_loss(client, tmp_path):
    raw = '{"requests":[],"requests":' + json.dumps(request()["requests"]) + '}'
    response = client.post("/api/pricing/quote", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 422
    source = tmp_path / "duplicate.json"
    source.write_text(raw)
    result = CliRunner().invoke(cli, ["quote", "--input", str(source)])
    assert result.exit_code == 1


def test_supplement_cannot_reroute_an_existing_alias(tmp_path, monkeypatch):
    monkeypatch.delenv("AITEAM_PRICING_CATALOG", raising=False)
    patch = load_catalog().model_dump(mode="json")
    patch["version"] = "test-supplement"
    patch["aliases"]["gpt-5.6"] = "gpt-6-astra"
    source = tmp_path / "supplement.json"
    source.write_text(json.dumps(patch))
    result = CliRunner().invoke(cli, ["supplement", "--supplement", str(source), "--version", "test-merged"])
    assert result.exit_code == 1


def test_supplement_preserves_records_and_requotes(client, tmp_path, monkeypatch):
    base = load_catalog()
    patch = base.model_dump(mode="json")
    patch["version"] = "test-supplement"
    patch["records"] = [next(row for row in patch["records"] if row["model"] == "gpt-6-astra")]
    patch["records"][0]["model"] = "new-model"
    patch["aliases"] = {}
    patch["unpriced_models"] = []
    source = tmp_path / "supplement.json"
    source.write_text(json.dumps(patch))
    result = CliRunner().invoke(cli, ["supplement", "--supplement", str(source), "--version", "test-merged"])
    assert result.exit_code == 0, result.output
    merged = json.loads(result.stdout)
    assert len(merged["records"]) == len(base.records) + 1
    destination = tmp_path / "merged.json"
    destination.write_text(result.stdout)
    monkeypatch.setenv("AITEAM_PRICING_CATALOG", str(destination))
    quote = client.post("/api/pricing/quote", json=request("new-model")).json()["data"]
    assert quote["complete"]
    assert quote["catalog_version"] == "test-merged"
    assert quote["catalog_sha256"] != catalog_digest(base)
    # A second request re-reads the active file: a quote is never a cached zero.
    monkeypatch.delenv("AITEAM_PRICING_CATALOG")
    assert client.post("/api/pricing/quote", json=request("new-model")).json()["data"]["total_usd"] is None


def speed_patch(shift: timedelta, multiplier: str = "9") -> tuple[dict, str]:
    """A supplement that only re-verifies the ultrafast rule, moved by ``shift`` from the base."""
    patch = load_catalog().model_dump(mode="json")
    patch.update(version="test-supplement", records=[], aliases={}, unpriced_models=[])
    mode = next(row for row in patch["speed_modes"] if row["service_tier"] == "ultrafast")
    verified_at = (datetime.fromisoformat(mode["verified_at"]) + shift).isoformat()
    mode.update(included_usage_multiplier=multiplier, verified_at=verified_at)
    patch["speed_modes"] = [mode]
    return patch, verified_at


def run_supplement(tmp_path, patch):
    source = tmp_path / "supplement.json"
    source.write_text(json.dumps(patch))
    return CliRunner().invoke(cli, ["supplement", "--supplement", str(source), "--version", "test-merged"])


def test_supplement_replaces_speed_mode_by_tier_and_keeps_the_rest(client, tmp_path, monkeypatch):
    monkeypatch.delenv("AITEAM_PRICING_CATALOG", raising=False)
    base = load_catalog()
    base_modes = {row["service_tier"]: row for row in base.model_dump(mode="json")["speed_modes"]}
    patch, verified_at = speed_patch(timedelta(days=1))
    result = run_supplement(tmp_path, patch)
    assert result.exit_code == 0, result.output
    merged = json.loads(result.stdout)
    modes = {row["service_tier"]: row for row in merged["speed_modes"]}
    assert modes.keys() == base_modes.keys()
    assert modes["fast"] == base_modes["fast"]
    assert modes["ultrafast"] == {**base_modes["ultrafast"], "included_usage_multiplier": "9",
                                  "verified_at": verified_at.replace("+00:00", "Z")}
    # Read the merged catalog back through the API that serves it.
    destination = tmp_path / "merged.json"
    destination.write_text(result.stdout)
    monkeypatch.setenv("AITEAM_PRICING_CATALOG", str(destination))
    served = client.get("/api/pricing/catalog").json()["data"]
    assert served["catalog"]["speed_modes"] == merged["speed_modes"]
    assert served["catalog_sha256"] != catalog_digest(base)


@pytest.mark.parametrize(("shift", "exit_code"), [
    pytest.param(timedelta(microseconds=-1), 1, id="older-rejected"),
    pytest.param(timedelta(0), 0, id="same-time-accepted"),
])
def test_supplement_speed_mode_verification_cannot_move_backwards(tmp_path, monkeypatch, shift, exit_code):
    monkeypatch.delenv("AITEAM_PRICING_CATALOG", raising=False)
    patch, _ = speed_patch(shift)
    result = run_supplement(tmp_path, patch)
    assert result.exit_code == exit_code, result.output
    if exit_code:
        assert not result.stdout
    else:
        merged = {row["service_tier"]: row for row in json.loads(result.stdout)["speed_modes"]}
        assert merged["ultrafast"]["included_usage_multiplier"] == "9"


def test_ultrafast_api_and_unknown_tier_keep_request_identity(client):
    payload = request("gpt-6.1-sol")
    payload["requests"][0]["service_tier"] = "ultrafast"
    result = client.post("/api/pricing/quote", json=payload)
    assert result.status_code == 200
    priced = result.json()["data"]
    assert priced["items"][0]["service_tier"] == "ultrafast"
    assert Decimal(priced["total_usd"]) == Decimal("0.001746")
    payload["requests"][0]["service_tier"] = "future-tier"
    result = client.post("/api/pricing/quote", json=payload)
    assert result.status_code == 200
    item = result.json()["data"]["items"][0]
    assert item["service_tier"] == "future-tier"
    assert item["reason"] == "service_tier_not_in_catalog"
    assert item["amount_usd"] is None
