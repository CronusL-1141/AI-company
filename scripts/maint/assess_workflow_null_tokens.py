#!/usr/bin/env python3
"""只读评估 done/zero 的终态缺测候选；不恢复、不生成写库计划。

历史报告 b55ebda9 只有 85 的计数，没有行 ID。本工具列出的当前候选不能
替代原 85 行。CTX_LAST 是末条 assistant usage 四分量和，绝非累计消耗。
仅使用标准库，避免导入生产模块触发默认数据库目录创建/迁移。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

CTX_FIELDS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')


def transcript_evidence(path: Path) -> dict:
    """读完整原件并留 hash；没有 assistant usage 不推成零。"""
    digest = hashlib.sha256()
    last = None
    try:
        with path.open('rb') as stream:
            for raw in stream:
                digest.update(raw)
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(row, dict) or row.get('type') != 'assistant':
                    continue
                msg = row.get('message')
                usage = msg.get('usage') if isinstance(msg, dict) else None
                if isinstance(usage, dict):
                    def value(key):
                        try:
                            return int(float(usage.get(key) or 0))
                        except (ValueError, TypeError, OverflowError):
                            return 0
                    last = sum(value(key) for key in CTX_FIELDS)
    except OSError:
        return {'path': str(path), 'status': 'unavailable'}
    return {
        'path': str(path), 'sha256': digest.hexdigest(), 'ctx_last': last,
        'status': 'ctx_last_positive' if last is not None and last > 0 else
                  'measured_zero' if last == 0 else 'no_usage',
    }


def assess(db: Path, projects_dir: Path, project_id: str) -> dict:
    snapshots: dict[str, list[Path]] = {}
    for path in projects_dir.glob('*/*/workflows/wf_*.json'):
        snapshots.setdefault(path.stem, []).append(path)
    conn = sqlite3.connect(f'{db.resolve().as_uri()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        rows = conn.execute('''
            SELECT w.id,w.wf_id,w.cc_agent_id,w.created_at,r.session_id,r.transcript_dir,
                   a.transcript_path AS linked_transcript_path
            FROM workflow_agents w JOIN workflow_runs r ON w.wf_id=r.wf_id
            LEFT JOIN agents a ON a.id=w.os_agent_id AND a.cc_tool_use_id=w.cc_agent_id
            WHERE w.project_id=? AND w.state='done' AND w.tokens=0
            ORDER BY w.wf_id,w.cc_agent_id
        ''', (project_id,)).fetchall()
    finally:
        conn.close()
    candidates = []
    excluded = 0
    unresolved = []
    for row in rows:
        choices = snapshots.get(row['wf_id'], [])
        if row['session_id']:
            choices = [p for p in choices if p.parent.parent.name == row['session_id']]
        if len(choices) != 1:
            unresolved.append({'id': row['id'], 'status': 'snapshot_missing_or_ambiguous'})
            continue
        snapshot = choices[0]
        try:
            raw = snapshot.read_bytes()
            data = json.loads(raw)
            entries = [entry for entry in data.get('workflowProgress', [])
                       if isinstance(entry, dict) and entry.get('type') == 'workflow_agent'
                       and entry.get('agentId') == row['cc_agent_id']]
        except (OSError, ValueError, AttributeError):
            unresolved.append({'id': row['id'], 'status': 'snapshot_unreadable'})
            continue
        if data.get('runId') != row['wf_id'] or len(entries) != 1:
            unresolved.append({'id': row['id'], 'status': 'snapshot_entry_unresolved'})
            continue
        entry = entries[0]
        if entry.get('tokens') is not None:
            excluded += 1
            continue
        # 仅缺测候选；显式 0 等其他原因不查、不回采。
        paths = [snapshot.parent.parent / 'subagents' / 'workflows' /
                 row['wf_id'] / f'agent-{row["cc_agent_id"]}.jsonl']
        if row['linked_transcript_path']:
            linked = Path(row['linked_transcript_path'])
            if linked not in paths:
                paths.append(linked)
        sources = [transcript_evidence(path) for path in paths]
        values = {s['ctx_last'] for s in sources if s['status'] == 'ctx_last_positive'}
        candidates.append({
            'id': row['id'], 'wf_id': row['wf_id'], 'cc_agent_id': row['cc_agent_id'],
            'created_at': row['created_at'], 'snapshot': str(snapshot),
            'snapshot_sha256': hashlib.sha256(raw).hexdigest(),
            'tokens_field': 'null' if 'tokens' in entry else 'missing',
            'sources': sources,
            'assessment': 'source_available' if len(values) == 1 else
                          'conflicting_sources' if len(values) > 1 else 'no_positive_source',
        })
    return {
        'mode': 'read_only', 'db': str(db.resolve()), 'db_user_version': version,
        'project_id': project_id,
        'historical_85_identity': 'unavailable_report_has_count_only',
        'historical_85_restored': 0,
        'current_candidates': len(candidates),
        'current_source_available': sum(c['assessment'] == 'source_available' for c in candidates),
        'other_token_values_excluded_without_investigation': excluded,
        'unresolved': unresolved, 'candidates': candidates,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--projects-dir', required=True, type=Path)
    parser.add_argument('--project-id', required=True)
    args = parser.parse_args()
    print(json.dumps(assess(args.db, args.projects_dir, args.project_id), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
