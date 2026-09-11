import { serverTimeMs } from './datetime';

interface AgentIdentity {
  id?: string;
  name?: string;
  role?: string;
  session_id?: string | null;
  harness?: string | null;
}

export function isFreshWorking(
  agent: { status?: string; last_active_at?: string | null }, now = Date.now(),
): boolean {
  if (agent.status !== 'busy' || !agent.last_active_at) return false;
  const timestamp = serverTimeMs(agent.last_active_at);
  const age = now - timestamp;
  // Match the backend project summary; an old busy row is not current work.
  return Number.isFinite(timestamp) && age >= 0 && age < 15 * 60_000;
}

export function readableAgentName(agent: AgentIdentity, leaderLabel: string, unknownLabel: string): string {
  const name = agent.name?.trim() ?? '';
  const suffix = (agent.session_id || agent.id || '').slice(0, 8);
  if (agent.role === 'leader' && (!name || /^cc-leader(?:-|$)/i.test(name) || name === 'leader')) {
    return [leaderLabel, suffix].filter(Boolean).join(' · ');
  }
  return name || [unknownLabel, suffix].filter(Boolean).join(' · ');
}

export function readableHarness(harness: string | null | undefined, unknownLabel: string): string {
  if (harness === 'codex') return 'Codex';
  if (harness === 'claude-code') return 'Claude Code';
  return unknownLabel;
}

export function agentKindLabel(agent: AgentIdentity, unknownLabel: string): string {
  if (agent.role !== 'leader') return readableHarness(agent.harness, unknownLabel);
  if (agent.harness === 'codex') return 'Codex Leader';
  if (agent.harness === 'claude-code') return 'Claude Leader';
  return unknownLabel;
}

export function readableMemberName(
  agent: AgentIdentity, workflowLabel: string | undefined, leaderLabel: string, unknownLabel: string,
): string {
  const name = readableAgentName(agent, leaderLabel, unknownLabel);
  return agent.harness === 'codex' ? name : workflowLabel || name;
}
