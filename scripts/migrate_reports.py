"""Migrate old file-system reports to the database via POST /api/reports."""

import re
import sys
from pathlib import Path

import requests

REPORTS_DIR = Path.home() / ".claude" / "data" / "ai-team-os" / "reports"
API_BASE = "http://localhost:8000"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML frontmatter and return (fields, full_content)."""
    fields: dict = {}
    if not text.startswith("---"):
        return fields, text

    end = text.find("\n---", 3)
    if end == -1:
        return fields, text

    fm_block = text[3:end].strip()
    for line in fm_block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()

    return fields, text  # content includes frontmatter as-is


def migrate() -> None:
    if not REPORTS_DIR.exists():
        print(f"Reports directory not found: {REPORTS_DIR}")
        sys.exit(1)

    files = sorted(REPORTS_DIR.glob("*.md"))
    total = len(files)
    success = 0
    skipped = 0
    failed = 0

    print(f"Found {total} report files in {REPORTS_DIR}\n")

    for f in files:
        raw = f.read_text(encoding="utf-8")
        fm, content = parse_frontmatter(raw)

        # Derive fields — fall back to filename parsing
        name = f.stem  # e.g. "algo-researcher_量化策略_2026-04-04"
        parts = name.split("_")

        author = fm.get("author") or (parts[0] if parts else "unknown")
        topic = fm.get("topic") or ("_".join(parts[1:-1]) if len(parts) >= 3 else name)
        report_type = fm.get("type") or "research"
        project_id = fm.get("project_id") or ""

        payload = {
            "author": author,
            "topic": topic,
            "content": content,
            "report_type": report_type,
            "task_id": "",
            "team_id": "",
        }
        headers = {}
        if project_id:
            headers["X-Project-Id"] = project_id

        try:
            resp = requests.post(f"{API_BASE}/api/reports", json=payload, headers=headers, timeout=10)
            if resp.status_code in (200, 201):
                success += 1
                print(f"  [OK] {f.name}")
            elif resp.status_code == 409:
                skipped += 1
                print(f"  [SKIP] {f.name} (already exists)")
            else:
                failed += 1
                print(f"  [FAIL] {f.name} — HTTP {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            failed += 1
            print(f"  [ERROR] {f.name} — {e}")

    print(f"\n=== Migration complete ===")
    print(f"  Total:   {total}")
    print(f"  Success: {success}")
    print(f"  Skipped: {skipped}")
    print(f"  Failed:  {failed}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    migrate()
