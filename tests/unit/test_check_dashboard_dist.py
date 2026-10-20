"""I3: the local and shipped Dashboard builds must be one build (scripts/check_dashboard_dist.py).

Each case writes two tiny dist trees under tmp_path and runs the real checker on
them; the repository's own dist directories are never read.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_dashboard_dist.py"
_spec = importlib.util.spec_from_file_location("check_dashboard_dist_under_test", SCRIPT)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

INDEX = """<!doctype html>
<html lang="en">
  <head>
    <link rel="icon" type="image/svg+xml" href="/vite.svg" />
    <script type="module" crossorigin src="/assets/index-AAAA.js"></script>
    <link rel="stylesheet" crossorigin href="/assets/index-BBBB.css">
  </head>
  <body><div id="root"></div></body>
</html>
"""


def write_dist(root: Path, *parts: str, index: str = INDEX) -> Path:
    dist = root.joinpath(*parts)
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(index, encoding="utf-8")
    for name, body in {"index-AAAA.js": "js", "index-BBBB.css": "css", "font-CCCC.woff2": "font"}.items():
        (dist / "assets" / name).write_text(body, encoding="utf-8")
    return dist


def write_both(root: Path) -> tuple[Path, Path]:
    return write_dist(root, "dashboard", "dist"), write_dist(root, "plugin", "dashboard-dist")


def run(root: Path, capsys) -> tuple[int, str]:
    code = check.main(["--root", str(root)])
    return code, capsys.readouterr().out


def test_one_build_passes(tmp_path, capsys):
    write_both(tmp_path)
    code, out = run(tmp_path, capsys)
    assert code == 0, out
    assert "JS 1 / CSS 1 / index.html 引用 2 项" in out


def test_only_the_stylesheet_changed_is_caught(tmp_path, capsys):
    """The JS name is unchanged; the old JS-only comparison passed this."""
    local, shipped = write_both(tmp_path)
    css = INDEX.replace("index-BBBB.css", "index-DDDD.css")
    (local / "index.html").write_text(css, encoding="utf-8")
    (local / "assets" / "index-BBBB.css").rename(local / "assets" / "index-DDDD.css")
    code, out = run(tmp_path, capsys)
    assert code == 1
    assert "assets/*.css 不一致" in out
    assert "assets/*.js" not in out


def test_index_html_difference_is_caught(tmp_path, capsys):
    local, _shipped = write_both(tmp_path)
    (local / "index.html").write_text(INDEX + "<!-- rebuilt -->\n", encoding="utf-8")
    code, out = run(tmp_path, capsys)
    assert code == 1
    assert "两份 index.html 不是逐字节相同" in out


def test_referenced_asset_with_different_bytes_is_caught(tmp_path, capsys):
    _local, shipped = write_both(tmp_path)
    (shipped / "assets" / "index-AAAA.js").write_text("stale js", encoding="utf-8")
    code, out = run(tmp_path, capsys)
    assert code == 1
    assert "assets/index-AAAA.js 两份内容不同" in out


@pytest.mark.parametrize("label,parts", [
    ("dashboard/dist", ("dashboard", "dist")),
    ("plugin/dashboard-dist", ("plugin", "dashboard-dist")),
], ids=["local", "shipped"])
def test_index_pointing_at_a_missing_bundle_is_caught(tmp_path, capsys, label, parts):
    write_both(tmp_path)
    (tmp_path.joinpath(*parts) / "assets" / "index-AAAA.js").unlink()
    code, out = run(tmp_path, capsys)
    assert code == 1
    assert f"{label}/index.html 引用的 assets/index-AAAA.js 不存在" in out


def test_shipped_copy_is_checked_even_without_a_local_build(tmp_path, capsys):
    """CI has no dashboard/dist: a shipped index.html that white-screens still fails."""
    shipped = write_dist(tmp_path, "plugin", "dashboard-dist")
    code, out = run(tmp_path, capsys)
    assert code == 2, out
    assert "缺 assets/ 目录: dashboard/dist" in out
    (shipped / "assets" / "index-BBBB.css").unlink()
    code, out = run(tmp_path, capsys)
    assert code == 1
    assert "plugin/dashboard-dist/index.html 引用的 assets/index-BBBB.css 不存在" in out


def test_reference_escaping_assets_is_rejected(tmp_path, capsys):
    escaping = INDEX.replace("/assets/index-AAAA.js", "/assets/../index.html")
    write_dist(tmp_path, "dashboard", "dist", index=escaping)
    write_dist(tmp_path, "plugin", "dashboard-dist", index=escaping)
    code, out = run(tmp_path, capsys)
    assert code == 1
    assert "引用越出 assets/" in out


def test_index_without_any_asset_reference_fails(tmp_path, capsys):
    bare = "<!doctype html><html><body></body></html>\n"
    write_dist(tmp_path, "dashboard", "dist", index=bare)
    write_dist(tmp_path, "plugin", "dashboard-dist", index=bare)
    code, out = run(tmp_path, capsys)
    assert code == 1
    assert "没有引用 assets/ 下的任何资源" in out


def test_cli_exit_codes(tmp_path):
    write_both(tmp_path)
    ok = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path)],
                        capture_output=True, text=True, timeout=30)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    (tmp_path / "plugin" / "dashboard-dist" / "index.html").unlink()
    broken = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path)],
                            capture_output=True, text=True, timeout=30)
    assert broken.returncode == 1
    assert "plugin/dashboard-dist/index.html 不可读" in broken.stdout
