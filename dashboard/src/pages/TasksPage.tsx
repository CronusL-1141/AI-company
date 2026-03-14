import { useState, useMemo } from 'react';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { KanbanColumn } from '@/components/tasks/KanbanColumn';
import { TaskDetailDialog } from '@/components/tasks/TaskDetailDialog';
import { useTeams } from '@/api/teams';
import { useTaskWall, useRunTask } from '@/api/tasks';
import { Plus, LayoutGrid } from 'lucide-react';
import type { Task } from '@/types';

const HORIZON_COLUMNS = [
  { horizon: 'short' as const, title: '短期', badgeClassName: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400' },
  { horizon: 'mid' as const, title: '中期', badgeClassName: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400' },
  { horizon: 'long' as const, title: '长期', badgeClassName: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400' },
];

function sortByScore(tasks: Task[]): Task[] {
  return [...tasks].sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
}

export function TasksPage() {
  const { data: teamsData, isLoading: teamsLoading } = useTeams();
  const teams = teamsData?.data ?? [];

  const [selectedTeamId, setSelectedTeamId] = useState<string>('');
  const activeTeamId = selectedTeamId || teams[0]?.id || '';

  const { data: wallData, isLoading: wallLoading, error: wallError } = useTaskWall(activeTeamId);

  const grouped = useMemo(() => {
    if (!wallData?.wall) return { short: [], mid: [], long: [] };
    return {
      short: sortByScore(wallData.wall.short ?? []),
      mid: sortByScore(wallData.wall.mid ?? []),
      long: sortByScore(wallData.wall.long ?? []),
    };
  }, [wallData]);

  const stats = wallData?.stats;

  // Detail dialog
  const [detailTask, setDetailTask] = useState<Task | null>(null);

  // New task dialog
  const [newTaskOpen, setNewTaskOpen] = useState(false);
  const [newTaskTitle, setNewTaskTitle] = useState('');
  const [newTaskDesc, setNewTaskDesc] = useState('');
  const runTask = useRunTask();

  function handleSubmitTask() {
    if (!activeTeamId || !newTaskTitle.trim()) return;
    runTask.mutate(
      { team_id: activeTeamId, title: newTaskTitle.trim(), description: newTaskDesc.trim() },
      {
        onSuccess: () => {
          setNewTaskOpen(false);
          setNewTaskTitle('');
          setNewTaskDesc('');
        },
      },
    );
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <LayoutGrid className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">任务墙</h1>
        </div>

        <div className="flex items-center gap-2">
          {teamsLoading ? (
            <Skeleton className="h-8 w-32" />
          ) : (
            <Select value={selectedTeamId || activeTeamId} onValueChange={(v) => setSelectedTeamId(v ?? '')}>
              <SelectTrigger className="w-[180px]">
                <SelectValue placeholder="选择项目">
                  {teams.find((t) => t.id === (selectedTeamId || activeTeamId))?.name ?? '选择项目'}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {teams.map((t) => (
                  <SelectItem key={t.id} value={t.id}>
                    {t.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}

          <Button onClick={() => setNewTaskOpen(true)} disabled={!activeTeamId}>
            <Plus className="h-4 w-4" />
            执行新任务
          </Button>
        </div>
      </div>

      {/* Stats Bar */}
      {stats && (
        <div className="flex items-center gap-4 text-sm text-muted-foreground">
          <span>共 {stats.total} 个任务</span>
          <span>平均分 {stats.avg_score?.toFixed(1) ?? '-'}</span>
          {stats.by_priority && Object.entries(stats.by_priority).map(([k, v]) => (
            <span key={k}>{k}: {v}</span>
          ))}
        </div>
      )}

      {/* Kanban Board - Horizon based */}
      {wallLoading ? (
        <div className="grid grid-cols-3 gap-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="space-y-2">
              <Skeleton className="h-6 w-20" />
              <Skeleton className="h-24" />
              <Skeleton className="h-24" />
            </div>
          ))}
        </div>
      ) : wallError ? (
        <div className="rounded-lg border border-destructive/50 bg-destructive/5 p-6 text-center">
          <p className="text-sm text-destructive">加载任务失败：{(wallError as Error).message}</p>
        </div>
      ) : !activeTeamId ? (
        <div className="rounded-lg border bg-muted/30 p-12 text-center">
          <p className="text-sm text-muted-foreground">请先选择一个项目以查看任务</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {HORIZON_COLUMNS.map((col) => (
            <KanbanColumn
              key={col.horizon}
              title={col.title}
              count={grouped[col.horizon]?.length ?? 0}
              badgeClassName={col.badgeClassName}
              tasks={grouped[col.horizon] ?? []}
              onTaskClick={setDetailTask}
            />
          ))}
        </div>
      )}

      {/* Task Detail Dialog */}
      <TaskDetailDialog
        task={detailTask}
        open={!!detailTask}
        onOpenChange={(open) => { if (!open) setDetailTask(null); }}
      />

      {/* New Task Dialog */}
      <Dialog open={newTaskOpen} onOpenChange={setNewTaskOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>执行新任务</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="task-title">任务标题</Label>
              <Input
                id="task-title"
                placeholder="输入任务标题"
                value={newTaskTitle}
                onChange={(e) => setNewTaskTitle(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="task-desc">任务描述</Label>
              <Textarea
                id="task-desc"
                placeholder="输入任务描述（可选）"
                value={newTaskDesc}
                onChange={(e) => setNewTaskDesc(e.target.value)}
                rows={4}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setNewTaskOpen(false)}>
              取消
            </Button>
            <Button
              onClick={handleSubmitTask}
              disabled={!newTaskTitle.trim() || runTask.isPending}
            >
              {runTask.isPending ? '提交中...' : '提交'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
