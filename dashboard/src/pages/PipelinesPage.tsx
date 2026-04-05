import { useState, useMemo } from 'react';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { useTeams } from '@/api/teams';
import { useTasks } from '@/api/tasks';
import type { Task } from '@/types';
import { GitBranch, Clock, User, CheckCircle2, Loader2, Circle, MinusCircle } from 'lucide-react';

// Pipeline stage status colors
const STAGE_STYLES: Record<string, { bg: string; border: string; text: string; label: string }> = {
  completed: {
    bg: 'bg-green-100 dark:bg-green-900/30',
    border: 'border-green-400',
    text: 'text-green-700 dark:text-green-400',
    label: '已完成',
  },
  running: {
    bg: 'bg-blue-100 dark:bg-blue-900/30',
    border: 'border-blue-400',
    text: 'text-blue-700 dark:text-blue-400',
    label: '进行中',
  },
  pending: {
    bg: 'bg-gray-100 dark:bg-gray-800/50',
    border: 'border-gray-300',
    text: 'text-gray-500 dark:text-gray-400',
    label: '待开始',
  },
  failed: {
    bg: 'bg-red-100 dark:bg-red-900/30',
    border: 'border-red-400',
    text: 'text-red-700 dark:text-red-400',
    label: '失败',
  },
  skipped: {
    bg: 'bg-gray-50 dark:bg-gray-900/20',
    border: 'border-dashed border-gray-300',
    text: 'text-gray-400',
    label: '跳过',
  },
};

function StageIcon({ status }: { status: string }) {
  if (status === 'completed') return <CheckCircle2 className="h-3.5 w-3.5" />;
  if (status === 'running') return <Loader2 className="h-3.5 w-3.5 animate-spin" />;
  if (status === 'skipped') return <MinusCircle className="h-3.5 w-3.5" />;
  return <Circle className="h-3.5 w-3.5" />;
}

interface StageDetail {
  agent?: string;
  duration?: string;
  memo?: string;
}

function PipelineStageCell({
  name,
  status,
  detail,
}: {
  name: string;
  status: string;
  detail?: StageDetail;
}) {
  const style = STAGE_STYLES[status] ?? STAGE_STYLES.pending;

  return (
    <Tooltip>
      <TooltipTrigger
        className={`flex flex-col items-center gap-1 px-3 py-2 rounded border ${style.bg} ${style.border} cursor-default min-w-[80px]`}
      >
        <span className={`${style.text}`}>
          <StageIcon status={status} />
        </span>
        <span className={`text-xs font-medium ${style.text} text-center leading-tight`}>
          {name}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <div className="space-y-1 text-xs">
          <p className="font-semibold">{name}</p>
          <p>状态: {style.label}</p>
          {detail?.agent && <p className="flex items-center gap-1"><User className="h-3 w-3" />{detail.agent}</p>}
          {detail?.duration && <p className="flex items-center gap-1"><Clock className="h-3 w-3" />{detail.duration}</p>}
          {detail?.memo && <p className="text-muted-foreground">{detail.memo}</p>}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}

function PipelineRow({ task }: { task: Task }) {
  const pipeline = task.pipeline_progress;
  if (!pipeline) return null;

  const completed = pipeline.stages.filter((s) => s.status === 'completed').length;
  const total = pipeline.total_stages;
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

  return (
    <Card className="overflow-hidden">
      <CardHeader className="pb-2 pt-4 px-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <p className="font-medium text-sm truncate">{task.title}</p>
            <div className="flex items-center gap-2 mt-1">
              <Badge variant="outline" className="text-xs">{task.status}</Badge>
              <span className="text-xs text-muted-foreground">
                当前阶段: <strong>{pipeline.current_stage}</strong>
              </span>
              <span className="text-xs text-muted-foreground">
                {completed}/{total} ({pct}%)
              </span>
            </div>
          </div>
          {task.assigned_to && (
            <div className="flex items-center gap-1 text-xs text-muted-foreground shrink-0">
              <User className="h-3 w-3" />
              <span>{task.assigned_to}</span>
            </div>
          )}
        </div>
        {/* Progress bar */}
        <div className="w-full h-1 bg-muted rounded-full overflow-hidden mt-2">
          <div
            className="h-full bg-primary transition-all duration-300"
            style={{ width: `${pct}%` }}
          />
        </div>
      </CardHeader>
      <CardContent className="px-4 pb-4">
        <div className="flex items-center gap-2 flex-wrap">
          {pipeline.stages.map((stage, idx) => (
            <div key={stage.name} className="flex items-center gap-1">
              <PipelineStageCell
                name={stage.name}
                status={stage.status}
                detail={{ agent: stage.agent_template }}
              />
              {idx < pipeline.stages.length - 1 && (
                <div className="h-px w-3 bg-border shrink-0" />
              )}
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

export function PipelinesPage() {
  const { data: teamsData, isLoading: teamsLoading } = useTeams();
  const teams = teamsData?.data ?? [];

  const [selectedTeamId, setSelectedTeamId] = useState<string>('');
  const activeTeamId = selectedTeamId || teams[0]?.id || '';

  const { data: tasksData, isLoading: tasksLoading } = useTasks(activeTeamId);
  const tasks = tasksData?.data ?? [];

  // Only tasks that have pipeline_progress
  const pipelineTasks = useMemo(
    () => tasks.filter((t) => t.pipeline_progress != null),
    [tasks],
  );

  const running = pipelineTasks.filter((t) => t.status === 'running' || t.status === 'in_progress');
  const others = pipelineTasks.filter((t) => t.status !== 'running' && t.status !== 'in_progress');

  const isLoading = teamsLoading || tasksLoading;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <GitBranch className="h-6 w-6" />
            Pipeline 可视化
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            查看各 Pipeline 工作流的阶段进度
          </p>
        </div>
        <Select
          value={activeTeamId}
          onValueChange={(v) => setSelectedTeamId(v ?? '')}
          disabled={teamsLoading}
        >
          <SelectTrigger className="w-48">
            <SelectValue placeholder="选择团队" />
          </SelectTrigger>
          <SelectContent>
            {teams.map((team) => (
              <SelectItem key={team.id} value={team.id}>
                {team.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-3 gap-4">
        {isLoading ? (
          Array.from({ length: 3 }).map((_, i) => (
            <Card key={i}>
              <CardContent className="pt-6">
                <Skeleton className="h-8 w-16 mb-1" />
                <Skeleton className="h-4 w-24" />
              </CardContent>
            </Card>
          ))
        ) : (
          <>
            <Card>
              <CardContent className="pt-6">
                <p className="text-2xl font-bold">{pipelineTasks.length}</p>
                <p className="text-sm text-muted-foreground">Pipeline 任务总数</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <p className="text-2xl font-bold text-blue-600">{running.length}</p>
                <p className="text-sm text-muted-foreground">进行中</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <p className="text-2xl font-bold text-green-600">
                  {pipelineTasks.filter((t) => t.status === 'completed').length}
                </p>
                <p className="text-sm text-muted-foreground">已完成</p>
              </CardContent>
            </Card>
          </>
        )}
      </div>

      {/* Legend */}
      <div className="flex items-center gap-4 text-xs text-muted-foreground flex-wrap">
        <span className="font-medium">阶段状态:</span>
        {Object.entries(STAGE_STYLES).map(([status, style]) => (
          <div key={status} className="flex items-center gap-1">
            <div className={`w-3 h-3 rounded border ${style.bg} ${style.border}`} />
            <span>{style.label}</span>
          </div>
        ))}
      </div>

      {isLoading ? (
        <div className="space-y-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <Card key={i}>
              <CardContent className="pt-6">
                <Skeleton className="h-20 w-full" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : pipelineTasks.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-muted-foreground">
            <GitBranch className="h-8 w-8 mx-auto mb-3 opacity-40" />
            <p>当前团队暂无 Pipeline 任务</p>
            <p className="text-xs mt-1">为任务创建 Pipeline 后将在此显示</p>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          {running.length > 0 && (
            <div className="space-y-3">
              <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wide">
                进行中 ({running.length})
              </h2>
              {running.map((task) => (
                <PipelineRow key={task.id} task={task} />
              ))}
            </div>
          )}
          {others.length > 0 && (
            <div className="space-y-3">
              <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wide">
                其他 ({others.length})
              </h2>
              {others.map((task) => (
                <PipelineRow key={task.id} task={task} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
