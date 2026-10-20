"""Exercise the real app's development CORS policy against isolated SQLite."""

import pytest


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("http://localhost:5174", id="current-vite-port"),
        pytest.param("http://localhost:5173", id="historical-vite-port"),
        pytest.param("http://localhost:3000", id="existing-local-client"),
    ],
)
def test_development_origins_allow_preflight_and_api_read(integration_client, origin):
    preflight = integration_client.options(
        "/api/projects",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin
    assert preflight.headers["access-control-allow-credentials"] == "true"

    response = integration_client.get("/api/projects", headers={"Origin": origin})
    assert response.status_code == 200
    assert response.json()["data"] == []
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://untrusted.example", id="untrusted-site"),
        pytest.param("http://localhost:5174.evil.example", id="local-origin-lookalike"),
    ],
)
def test_other_origins_do_not_receive_cors_permission(integration_client, origin):
    preflight = integration_client.options(
        "/api/projects",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in preflight.headers

    response = integration_client.get("/api/projects", headers={"Origin": origin})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
