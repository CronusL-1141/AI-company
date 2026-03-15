"""AI Team OS — 常驻成员配置API测试."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from aiteam.api.routes import team_config


@pytest.fixture()
def config_client(tmp_path):
    """创建测试客户端，配置文件指向临时目录."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "team-defaults.json"

    # 写入默认配置
    default = {
        "auto_create_team": True,
        "team_name_prefix": "auto",
        "permanent_members": [
            {
                "name": "qa-observer",
                "role": "常驻QA观察员",
                "model": "claude-sonnet-4-6",
                "enabled": True,
            },
            {
                "name": "bug-fixer",
                "role": "常驻Bug工程师",
                "model": "claude-sonnet-4-6",
                "enabled": True,
            },
        ],
    }
    config_file.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")

    # Patch配置路径
    with patch.object(team_config, "CONFIG_DIR", config_dir), \
         patch.object(team_config, "CONFIG_FILE", config_file):
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(team_config.router)
        client = TestClient(app)
        yield client


class TestGetDefaults:
    """测试GET默认配置."""

    def test_get_defaults(self, config_client):
        resp = config_client.get("/api/config/team-defaults")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["auto_create_team"] is True
        assert len(data["data"]["permanent_members"]) == 2

    def test_get_defaults_member_names(self, config_client):
        resp = config_client.get("/api/config/team-defaults")
        members = resp.json()["data"]["permanent_members"]
        names = [m["name"] for m in members]
        assert "qa-observer" in names
        assert "bug-fixer" in names


class TestPutDefaults:
    """测试PUT更新配置."""

    def test_put_update_config(self, config_client):
        new_config = {
            "auto_create_team": False,
            "team_name_prefix": "test",
            "permanent_members": [
                {
                    "name": "researcher",
                    "role": "研究员",
                    "model": "claude-opus-4-6",
                    "enabled": True,
                }
            ],
        }
        resp = config_client.put("/api/config/team-defaults", json=new_config)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["auto_create_team"] is False
        assert data["data"]["team_name_prefix"] == "test"
        assert len(data["data"]["permanent_members"]) == 1

        # 验证持久化
        resp2 = config_client.get("/api/config/team-defaults")
        assert resp2.json()["data"]["auto_create_team"] is False


class TestPostMember:
    """测试POST添加成员."""

    def test_add_member(self, config_client):
        new_member = {
            "name": "researcher",
            "role": "研究员",
            "model": "claude-opus-4-6",
            "enabled": True,
        }
        resp = config_client.post("/api/config/team-defaults/members", json=new_member)
        assert resp.status_code == 201
        assert resp.json()["success"] is True

        # 验证已添加
        resp2 = config_client.get("/api/config/team-defaults")
        members = resp2.json()["data"]["permanent_members"]
        assert len(members) == 3
        names = [m["name"] for m in members]
        assert "researcher" in names

    def test_add_duplicate_member(self, config_client):
        dup_member = {
            "name": "qa-observer",
            "role": "重复的QA",
        }
        resp = config_client.post("/api/config/team-defaults/members", json=dup_member)
        assert resp.status_code == 409


class TestDeleteMember:
    """测试DELETE删除成员."""

    def test_delete_member(self, config_client):
        resp = config_client.delete("/api/config/team-defaults/members/qa-observer")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # 验证已删除
        resp2 = config_client.get("/api/config/team-defaults")
        members = resp2.json()["data"]["permanent_members"]
        assert len(members) == 1
        assert members[0]["name"] == "bug-fixer"

    def test_delete_nonexistent_member(self, config_client):
        resp = config_client.delete("/api/config/team-defaults/members/nonexistent")
        assert resp.status_code == 404


class TestPatchMember:
    """测试PATCH更新成员."""

    def test_disable_member(self, config_client):
        resp = config_client.patch(
            "/api/config/team-defaults/members/qa-observer",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["enabled"] is False
        assert data["data"]["name"] == "qa-observer"

    def test_update_model(self, config_client):
        resp = config_client.patch(
            "/api/config/team-defaults/members/bug-fixer",
            json={"model": "claude-opus-4-6"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["model"] == "claude-opus-4-6"

    def test_update_role(self, config_client):
        resp = config_client.patch(
            "/api/config/team-defaults/members/qa-observer",
            json={"role": "高级QA观察员"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["role"] == "高级QA观察员"

    def test_patch_nonexistent_member(self, config_client):
        resp = config_client.patch(
            "/api/config/team-defaults/members/nonexistent",
            json={"enabled": False},
        )
        assert resp.status_code == 404
