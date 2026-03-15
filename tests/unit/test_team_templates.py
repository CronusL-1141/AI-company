"""AI Team OS — 团队模板API测试."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiteam.api.routes import templates


@pytest.fixture()
def templates_client(tmp_path):
    """创建测试客户端，模板文件指向临时目录."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    templates_file = config_dir / "team-templates.json"

    data = {
        "templates": [
            {
                "id": "fullstack",
                "name": "全栈开发团队",
                "description": "前后端+测试的标准开发团队",
                "members": [
                    {"name": "frontend-engineer", "role": "前端工程师"},
                    {"name": "backend-engineer", "role": "后端工程师"},
                ],
            },
            {
                "id": "minimal",
                "name": "最小团队",
                "description": "只有常驻成员的最小配置",
                "members": [
                    {"name": "qa-observer", "role": "QA观察员"},
                ],
            },
        ]
    }
    templates_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    with patch.object(templates, "CONFIG_DIR", config_dir), \
         patch.object(templates, "TEMPLATES_FILE", templates_file):
        app = FastAPI()
        app.include_router(templates.router)
        client = TestClient(app)
        yield client


class TestListTemplates:
    """测试GET列出所有模板."""

    def test_list_templates(self, templates_client):
        resp = templates_client.get("/api/config/team-templates")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert len(data["data"]) == 2
        ids = [t["id"] for t in data["data"]]
        assert "fullstack" in ids
        assert "minimal" in ids


class TestGetTemplate:
    """测试GET获取单个模板."""

    def test_get_template(self, templates_client):
        resp = templates_client.get("/api/config/team-templates/fullstack")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["id"] == "fullstack"
        assert data["data"]["name"] == "全栈开发团队"
        assert len(data["data"]["members"]) == 2

    def test_template_not_found(self, templates_client):
        resp = templates_client.get("/api/config/team-templates/nonexistent")
        assert resp.status_code == 404
