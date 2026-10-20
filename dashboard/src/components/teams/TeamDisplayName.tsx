import { useWorkflow } from '@/api/workflows';
import type { Team } from '@/types';

// workflow 团队主标题 = 观测层 run 名称（如 d5-ecosystem-axis-convergence）；
// wf 编号降级为小号淡色追踪标——编号是追踪用的，不该当主描述（用户 2026-07-06 需求）。
export function TeamDisplayName({ team }: { team: Team }) {
  const wfId =
    (team.config?.workflow_run_id as string | undefined) ??
    (team.name.startsWith('workflow-') && !team.name.startsWith('workflow-session-')
      ? team.name.replace(/^workflow-/, '')
      : undefined);
  const { data: run } = useWorkflow(wfId ?? '');
  if (!wfId) return <>{team.name}</>;
  return (
    <span className="inline-flex flex-col leading-tight">
      <span>{run?.name || team.name}</span>
      <span className="font-mono text-[10px] font-normal text-muted-foreground/60">
        {wfId}
      </span>
    </span>
  );
}
