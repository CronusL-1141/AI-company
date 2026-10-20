"""只读候选评估不能写库，且必须区分真实零、没有usage与正数。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'workflow_null_assessment',
    Path(__file__).resolve().parents[2] / 'scripts/maint/assess_workflow_null_tokens.py',
)
assessment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assessment)


def test_transcript_evidence_preserves_no_data_and_zero(tmp_path):
    path = tmp_path / 'agent-a.jsonl'
    path.write_text('{"type":"user"}\n')
    assert assessment.transcript_evidence(path)['ctx_last'] is None
    path.write_text('{"type":"assistant","message":{"usage":{"input_tokens":0}}}\n')
    assert assessment.transcript_evidence(path)['status'] == 'measured_zero'
    with path.open('a') as stream:
        stream.write(json.dumps({'type': 'assistant', 'message': {'usage': {
            'input_tokens': 2, 'cache_creation_input_tokens': 3,
            'cache_read_input_tokens': 4, 'output_tokens': 5,
        }}}) + '\n')
        stream.write('{"type":"attachment"}\n')
    assert assessment.transcript_evidence(path)['ctx_last'] == 14
    assert assessment.transcript_evidence(tmp_path / 'gone')['status'] == 'unavailable'


def test_assessment_is_readonly_and_excludes_explicit_zero(tmp_path):
    db = tmp_path / 'fixture.db'
    conn = sqlite3.connect(db)
    conn.executescript('''
        CREATE TABLE workflow_agents(id TEXT,wf_id TEXT,cc_agent_id TEXT,created_at TEXT,
            project_id TEXT,state TEXT,tokens INT,os_agent_id TEXT);
        CREATE TABLE workflow_runs(wf_id TEXT,session_id TEXT,transcript_dir TEXT);
        CREATE TABLE agents(id TEXT,cc_tool_use_id TEXT,transcript_path TEXT);
        INSERT INTO workflow_runs VALUES('wf_test','session','');
        INSERT INTO workflow_agents VALUES('row1','wf_test','null','2026-09-29','project','done',0,NULL);
        INSERT INTO workflow_agents VALUES('row2','wf_test','missing','2026-09-29','project','done',0,NULL);
        INSERT INTO workflow_agents VALUES('row3','wf_test','zero','2026-09-29','project','done',0,NULL);
        INSERT INTO workflow_agents VALUES('row4','wf_test','error','2026-09-29','project','error',0,NULL);
    ''')
    conn.close()
    original = hashlib.sha256(db.read_bytes()).hexdigest()
    session = tmp_path / 'projects' / 'slug' / 'session'
    wf_dir = session / 'subagents' / 'workflows' / 'wf_test'
    wf_dir.mkdir(parents=True)
    (wf_dir / 'agent-null.jsonl').write_text(
        '{"type":"assistant","message":{"usage":{"output_tokens":42}}}\n',
    )
    snapshots = session / 'workflows'
    snapshots.mkdir()
    (snapshots / 'wf_test.json').write_text(json.dumps({
        'runId': 'wf_test', 'workflowProgress': [
            {'type': 'workflow_agent', 'agentId': 'null', 'tokens': None},
            {'type': 'workflow_agent', 'agentId': 'missing'},
            {'type': 'workflow_agent', 'agentId': 'zero', 'tokens': 0},
        ],
    }))
    report = assessment.assess(db, tmp_path / 'projects', 'project')
    assert report['current_candidates'] == 2
    assert report['current_source_available'] == 1
    assert report['historical_85_restored'] == 0
    assert report['other_token_values_excluded_without_investigation'] == 1
    assert hashlib.sha256(db.read_bytes()).hexdigest() == original
    assert {row['id'] for row in report['candidates']} == {'row1', 'row2'}
    assert report['unresolved'] == []
