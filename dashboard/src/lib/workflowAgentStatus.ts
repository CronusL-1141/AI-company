import type { WorkflowAgentState } from '@/api/workflows';

/**
 * workflow 队成员终态语义（任务 8306664f）：workflow-subagent 完成一个阶段后，
 * 后端 agents.status 落回 'offline'——这是数据层的真实状态（该 CC 子 agent 进程
 * 确实已退出），但对用户是误导："4个已完成+1个在跑"被呈现成"5个里4个掉线"。
 *
 * 优先信 workflow 观测层 state（design workflow-observability §2.2 的权威源）：
 * done→已完成、running→在忙、queued→等待；拿不到投影数据（未接 wf_id 或该
 * agent 未入观测表）时退化用角色兜底——workflow-subagent 角色只会出现在
 * workflow 队（hook_translator.WORKFLOW_AGENT_TYPE），session 队成员是自定义
 * 角色名，不会误触这条规则，offline 依旧如实呈现为掉线（用户 2026-07-14 拍板）。
 */
export function resolveWorkflowAgentStatus(
  agent: { status: string; role: string },
  wfState: WorkflowAgentState | undefined,
): string {
  if (wfState === 'done') return 'done';
  if (wfState === 'running') return 'busy';
  if (wfState === 'queued') return 'waiting';
  const raw = agent.status.toLowerCase();
  if (raw === 'offline' && agent.role === 'workflow-subagent') return 'done';
  return raw;
}
