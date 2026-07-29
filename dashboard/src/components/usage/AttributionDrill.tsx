import { useState } from 'react';
import { ChevronRight, Home } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { AttributionCard } from '@/components/usage/AttributionCard';
import { cn } from '@/lib/utils';
import { useT } from '@/i18n';
import {
  useAttribution,
  useAttributionBatch,
  useScopeCandidates,
  type DispatchPath,
  type ScopeLevel,
} from '@/api/usage';

// ─────────────────────────────────────────────────────────────────────────────
// ③ 已归因明细（可下钻）—— 设计 §5.2 ③。
//
// 两条纪律落在结构上，不是落在文案上：
//
// 1. **主会话与子 agent 分列、默认不合并。** 实测单个主会话的量级远超全部子 agent
//    之和；混进一张榜里，子 agent 的归因结果会被彻底淹没。这里做成两块并排的独立
//    面板，各自有各自的下钻状态，页面上不存在任何一处把两者相加的地方。
// 2. **每一级把子项各渲染成一张卡，按 output_tokens 降序。** 排序键刻意不是四层
//    之和：cache_read 占 95.6%，按和排序等于按缓存读取量排序。
//
// 候选清单本身（/usage/scopes）不带任何用量；用量是逐个 scope 单独查回来的，每份
// 都自带分母与未归因分类。所以这不是"一张没有分母的排行榜"。
// ─────────────────────────────────────────────────────────────────────────────

const CHILD_OF: Record<string, ScopeLevel | null> = {
  root: 'project',
  project: 'session',
  session: 'workflow_run',
  workflow_run: 'agent',
  agent: null,
};

interface Crumb {
  scope: ScopeLevel;
  scopeId: string;
  label: string;
}

const CHILD_LIMIT = 8;

function PopulationPanel({
  path,
  days,
  onProbe,
}: {
  path: DispatchPath;
  days: number;
  onProbe: (agentId: string) => void;
}) {
  const t = useT();
  const [trail, setTrail] = useState<Crumb[]>([]);

  const levelName: Record<ScopeLevel, string> = {
    project: t.usage.levelProject,
    session: t.usage.levelSession,
    workflow_run: t.usage.levelWorkflowRun,
    agent: t.usage.levelAgent,
    task: t.usage.levelTask,
  };
  const pathName: Record<DispatchPath, string> = {
    subagent: t.usage.pathSubagent,
    leader_session: t.usage.pathLeader,
    workflow_self_report: t.usage.pathWorkflowSelfReport,
    tool_call: t.usage.pathToolCall,
  };

  const current = trail.at(-1) ?? null;
  const childLevel = CHILD_OF[current?.scope ?? 'root'] ?? null;

  // 当前这一级的汇总卡。scope_id 留空 = 不按该维度过滤（全库），后端按此约定。
  const summary = useAttribution(current?.scope ?? 'project', current?.scopeId ?? '', path, days);
  const candidates = useScopeCandidates(
    childLevel ?? 'project',
    current ? current.scope : '',
    current?.scopeId ?? '',
    path,
    days,
    CHILD_LIMIT,
  );
  const childIds = childLevel ? (candidates.data ?? []).map((c) => c.scope_id) : [];
  const childResults = useAttributionBatch(childLevel ?? 'project', childIds, path, days);

  const children = childIds
    .map((id, i) => ({
      id,
      label: candidates.data?.[i]?.label ?? '',
      data: childResults[i]?.data,
    }))
    .filter((c) => c.data)
    // 默认排序键 = output_tokens（唯一与"干了多少活"强相关的一层），不是四层之和
    .sort((a, b) => (b.data?.output_tokens ?? 0) - (a.data?.output_tokens ?? 0));

  return (
    <div className="min-w-0 space-y-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-1 text-xs">
        <span className="font-medium">{pathName[path]}</span>
        <span className="text-muted-foreground">·</span>
        <button
          type="button"
          onClick={() => setTrail([])}
          className="inline-flex items-center gap-0.5 rounded px-1 text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          <Home className="h-3 w-3" aria-hidden />
          {t.usage.drillRoot}
        </button>
        {trail.map((crumb, i) => (
          <span key={crumb.scope + crumb.scopeId} className="inline-flex items-center gap-0.5">
            <ChevronRight className="h-3 w-3 text-muted-foreground" aria-hidden />
            <button
              type="button"
              onClick={() => setTrail(trail.slice(0, i + 1))}
              className="max-w-[12rem] truncate rounded px-1 text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
              title={`${levelName[crumb.scope]} · ${crumb.scopeId}`}
            >
              {crumb.label || crumb.scopeId}
            </button>
          </span>
        ))}
      </div>

      {summary.data ? (
        <AttributionCard
          data={summary.data}
          title={current ? current.label || current.scopeId : `${pathName[path]} · ${t.usage.drillRoot}`}
          subtitle={current?.scopeId}
          compact
          onProbe={current?.scope === 'agent' ? () => onProbe(current.scopeId) : undefined}
        />
      ) : (
        <div className="rounded border border-dashed p-3 text-xs text-muted-foreground">
          {summary.isLoading ? t.common.loading : t.common.noData}
        </div>
      )}

      {childLevel && (
        <div className="space-y-1.5">
          <p className="text-[11px] text-muted-foreground">{t.usage.sortNote}</p>
          {candidates.isLoading && <p className="text-xs text-muted-foreground">{t.common.loading}</p>}
          {!candidates.isLoading && children.length === 0 && (
            <p className="rounded border border-dashed px-2 py-1.5 text-xs text-muted-foreground">
              {t.usage.noChildren}
            </p>
          )}
          {children.map((child) => (
            <AttributionCard
              key={child.id}
              data={child.data!}
              title={child.label || child.id}
              subtitle={child.id}
              compact
              drillLabel={
                CHILD_OF[childLevel] ? t.usage.drillInto(levelName[CHILD_OF[childLevel]!]) : undefined
              }
              onDrill={
                CHILD_OF[childLevel]
                  ? () =>
                      setTrail([
                        ...trail,
                        { scope: childLevel, scopeId: child.id, label: child.label },
                      ])
                  : undefined
              }
              onProbe={childLevel === 'agent' ? () => onProbe(child.id) : undefined}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** task 级旁支 —— 独立卡片，标题里必须印出它自己的真实覆盖率。 */
function TaskAside({ days, coveragePct }: { days: number; coveragePct: string }) {
  const t = useT();
  const candidates = useScopeCandidates('task', '', '', 'subagent', days, CHILD_LIMIT);
  const ids = (candidates.data ?? []).map((c) => c.scope_id);
  const results = useAttributionBatch('task', ids, 'subagent', days);
  const cards = ids
    .map((id, i) => ({ id, label: candidates.data?.[i]?.label ?? '', data: results[i]?.data }))
    .filter((c) => c.data)
    .sort((a, b) => (b.data?.output_tokens ?? 0) - (a.data?.output_tokens ?? 0));

  return (
    <div className="space-y-2 rounded-lg border border-dashed p-3">
      <div>
        <h3 className="text-sm font-medium">{t.usage.taskAsideTitle(coveragePct)}</h3>
        <p className="text-xs text-muted-foreground">{t.usage.taskAsideDesc}</p>
      </div>
      {cards.length === 0 ? (
        <p className="rounded border border-dashed px-2 py-1.5 text-xs text-muted-foreground">
          {t.usage.taskAsideEmpty}
        </p>
      ) : (
        <div className="grid gap-2 md:grid-cols-2">
          {cards.map((c) => (
            <AttributionCard key={c.id} data={c.data!} title={c.label || c.id} subtitle={c.id} compact />
          ))}
        </div>
      )}
    </div>
  );
}

export interface AttributionDrillProps {
  days: number;
  /** agent→task 跳的真实可解析率，原样印在旁支卡标题里 */
  taskCoveragePct: string;
  onProbe: (agentId: string) => void;
  className?: string;
}

export function AttributionDrill({ days, taskCoveragePct, onProbe, className }: AttributionDrillProps) {
  const t = useT();
  return (
    <Card className={cn(className)}>
      <CardHeader>
        <CardTitle>{t.usage.detailTitle}</CardTitle>
        <CardDescription>{t.usage.detailDesc}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* 分列呈现：两块并排，各自独立下钻，页面上不存在把两者相加的地方 */}
        <div className="grid gap-3 xl:grid-cols-2">
          <PopulationPanel path="subagent" days={days} onProbe={onProbe} />
          <PopulationPanel path="leader_session" days={days} onProbe={onProbe} />
        </div>
        <TaskAside days={days} coveragePct={taskCoveragePct} />
      </CardContent>
    </Card>
  );
}
