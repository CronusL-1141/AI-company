"""Report presentation follows the host zone without changing UTC storage.

Each zone runs in its own Python process: TZ never changes in the pytest host.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("zone", "instant", "local_date", "local_time", "zone_label"),
    [
        ("UTC", "2026-01-15T00:30:00+00:00", "2026-01-15", "00:30", "UTC (+0000)"),
        ("America/Los_Angeles", "2026-01-15T00:30:00+00:00", "2026-01-14", "16:30", "PST (-0800)"),
        ("America/Los_Angeles", "2026-07-15T00:30:00+00:00", "2026-07-14", "17:30", "PDT (-0700)"),
        ("America/Los_Angeles", "2026-07-15T06:59:00+00:00", "2026-07-14", "23:59", "PDT (-0700)"),
        ("America/Los_Angeles", "2026-07-15T07:00:00+00:00", "2026-07-15", "00:00", "PDT (-0700)"),
        ("America/Los_Angeles", "2026-03-08T09:59:00+00:00", "2026-03-08", "01:59", "PST (-0800)"),
        ("America/Los_Angeles", "2026-03-08T10:00:00+00:00", "2026-03-08", "03:00", "PDT (-0700)"),
    ],
    ids=["utc", "pacific-winter", "pacific-summer", "before-midnight", "midnight", "before-dst", "dst"],
)
def test_report_dates_use_host_timezone(
    zone: str, instant: str, local_date: str, local_time: str, zone_label: str,
) -> None:
    source = Path(__file__).resolve().parents[3] / "src"
    env = {**os.environ, "TZ": zone, "PYTHONPATH": str(source)}
    script = """
import json
import sys
from datetime import datetime
from unittest.mock import patch
import aiteam.mcp.tools.ecosystem as eco
from aiteam.services.ecosystem_summarizer import (
    WeeklyStats, HealthStats, _iso_date, _iso_dt, _render_weekly, _render_health,
)
from aiteam.types import EcosystemScanRun

now = datetime.fromisoformat(sys.argv[1])
class Capture:
    def __init__(self):
        self.tools = {}
    def tool(self, *args, **kwargs):
        def capture(fn):
            self.tools[fn.__name__] = fn
            return fn
        return capture
capture = Capture()
eco.register(capture)
saved = []
def api(method, path, payload=None):
    if path.startswith('/api/ecosystem/summary/'):
        return {'markdown': '# report'}
    if path == '/api/reports':
        saved.append(payload)
        return {'id': 'report-local-time'}
with patch.object(eco, 'utc_now', return_value=now), patch.object(eco, '_api_call', side_effect=api):
    capture.tools['ecosystem_summary_weekly']()
    capture.tools['ecosystem_summary_health']()
stats = WeeklyStats(window_days=7, window_start=now, window_end=now,
                    scan_runs=[EcosystemScanRun(started_at=now)])
print(json.dumps({
    'topics': [r['topic'] for r in saved],
    'date': _iso_date(now),
    'dt': _iso_dt(now),
    'naive_dt': _iso_dt(now.replace(tzinfo=None)),
    'missing': _iso_dt(None),
    'weekly': _render_weekly(stats),
    'health': _render_health(HealthStats(last_scan_run_at=now)),
    'instant': now.isoformat(),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, instant],
        env=env, capture_output=True, text=True, timeout=30, check=True,
    )
    data = json.loads(result.stdout)
    expected_date = f"{local_date} {zone_label}"
    expected_dt = f"{local_date} {local_time} {zone_label}"
    assert data["topics"] == [f"ecosystem-weekly-{local_date}", f"ecosystem-health-{local_date}"]
    assert data["date"] == expected_date
    assert data["dt"] == data["naive_dt"] == expected_dt
    assert expected_date in data["weekly"]
    assert expected_dt in data["weekly"]
    assert expected_dt in data["health"]
    assert data["missing"] == "-"
    assert data["instant"] == instant
