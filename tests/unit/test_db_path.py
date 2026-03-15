"""测试数据库路径固化 — 验证默认路径指向 ~/.claude/data/ai-team-os/aiteam.db."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from aiteam.storage.connection import _default_db_url, _migrate_old_db_if_needed


class TestDefaultDbUrl:
    """验证 _default_db_url() 返回正确的固定路径."""

    def test_url_contains_expected_path(self) -> None:
        """返回的URL应包含 .claude/data/ai-team-os/aiteam.db."""
        url = _default_db_url()
        assert "sqlite+aiosqlite:///" in url
        assert ".claude" in url
        assert "data" in url
        assert "ai-team-os" in url
        assert "aiteam.db" in url

    def test_directory_auto_created(self, tmp_path: Path) -> None:
        """使用mock的home目录验证目录自动创建."""
        fake_home = tmp_path / "fakehome"
        # 不预先创建目录，验证函数会自动创建
        with patch.object(Path, "home", return_value=fake_home):
            url = _default_db_url()

        expected_dir = fake_home / ".claude" / "data" / "ai-team-os"
        assert expected_dir.is_dir()
        assert url == f"sqlite+aiosqlite:///{expected_dir / 'aiteam.db'}"

    def test_idempotent_on_existing_directory(self, tmp_path: Path) -> None:
        """目录已存在时不应报错."""
        fake_home = tmp_path / "fakehome"
        data_dir = fake_home / ".claude" / "data" / "ai-team-os"
        data_dir.mkdir(parents=True)

        with patch.object(Path, "home", return_value=fake_home):
            url = _default_db_url()

        assert "aiteam.db" in url
        assert data_dir.is_dir()


class TestMigrateOldDb:
    """验证旧DB自动迁移逻辑."""

    def test_migrate_old_db(self, tmp_path: Path) -> None:
        """旧DB存在且新DB为空时，应自动迁移."""
        old_db = tmp_path / "old" / "aiteam.db"
        old_db.parent.mkdir(parents=True)
        old_db.write_bytes(b"x" * 20000)  # >10KB

        new_db = tmp_path / "new" / "aiteam.db"
        new_db.parent.mkdir(parents=True)

        with patch("aiteam.storage.connection.Path.cwd", return_value=old_db.parent):
            _migrate_old_db_if_needed(new_db)

        assert new_db.exists()
        assert new_db.stat().st_size == 20000
        # 旧文件应被重命名
        assert not old_db.exists()
        assert old_db.with_suffix(".db.migrated").exists()

    def test_skip_migrate_when_new_db_has_data(self, tmp_path: Path) -> None:
        """新DB已有数据时不应迁移."""
        old_db = tmp_path / "old" / "aiteam.db"
        old_db.parent.mkdir(parents=True)
        old_db.write_bytes(b"old" * 10000)

        new_db = tmp_path / "new" / "aiteam.db"
        new_db.parent.mkdir(parents=True)
        new_db.write_bytes(b"new" * 10000)  # >10KB

        with patch("aiteam.storage.connection.Path.cwd", return_value=old_db.parent):
            _migrate_old_db_if_needed(new_db)

        # 新DB内容不变
        assert new_db.read_bytes() == b"new" * 10000
        # 旧DB未被重命名
        assert old_db.exists()

    def test_migrate_silent_on_error(self, tmp_path: Path) -> None:
        """错误时应静默处理，不抛异常."""
        new_db = tmp_path / "nonexistent" / "deep" / "aiteam.db"
        # new_db的父目录不存在，stat会失败，但不应抛异常
        with patch("aiteam.storage.connection.Path.cwd", return_value=tmp_path):
            _migrate_old_db_if_needed(new_db)  # 不应抛异常
