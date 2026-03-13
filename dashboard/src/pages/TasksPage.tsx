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
import { useTasks, useRunTask } from '@/api/tasks';
import { Plus, LayoutGrid } from 'lucide-react';
import type { Task } from '@/types';

const COLUMNS = [
  { status: 'pending', title: '待处理', badgeClassName: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400' },
  { status: 'running', title: '运行中', badgeClassName: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400' },
  { status: 'completed', title: '已完成', badgeClassName: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400' },
  { status: 'failed', title: '失败', badgeClassName: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400' },
];

export function TasksPage() {
  const { data: teamsData, isLoading: teamsLoading } = useTeams();
  const teams = teamsData?.data ?? [];

  const [selectedTeamId, setSelectedTeamId] = useState<string>('');
  const activeTeamId = selectedTeamId || teams[0]?.id || '';

  const { data: tasksData, isLoading: tasksLoading, error: tasksError } = useTasks(activeTeamId);
  const tasks = tasksData?.data ?? [];

  const grouped = useMemo(() => {
    const map: Record<string, Task[]> = { pending: [], running: [], completed: [], failed: [] };
    for (const t of tasks) {
      (map[t.status] ??= []).push(t);
    }
    return map;
  }, [tasks]);

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
          <h1 className="text-lg font-semibold">任务看板</h1>
        </div>

        <div className="flex items-center gap-2">
          {teamsLoading ? (
            <Skeleton className="h-8 w-32" />
          ) : (
            <Select value={selectedTeamId || activeTeamId} onValueChange={(v) => setSelectedTeamId(v ?? '')}>
              <SelectTrigger className="w-[180px]">
                <SelectValue placeholder="选择团队">
                  {teams.find((t) => t.id === (selectedTeamId || activeTeamId))?.name ?? '选择团队'}
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

      {/* Kanban Board */}
      {tasksLoading ? (
        <div className="grid grid-cols-4 gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="space-y-2">
              <Skeleton className="h-6 w-20" />
              <Skeleton className="h-24" />
              <Skeleton className="h-24" />
            </div>
          ))}
        </div>
      ) : tasksError ? (
        <div className="rounded-lg border border-destructive/50 bg-destructive/5 p-6 text-center">
          <p className="text-sm text-destructive">加载任务失败：{(tasksError as Error).message}</p>
        </div>
      ) : !activeTeamId ? (
        <div className="rounded-lg border bg-muted/30 p-12 text-center">
          <p className="text-sm text-muted-foreground">请先选择一个团队以查看任务</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {COLUMNS.map((col) => (
            <KanbanColumn
              key={col.status}
              title={col.title}
              count={grouped[col.status]?.length ?? 0}
              badgeClassName={col.badgeClassName}
              tasks={grouped[col.status] ?? []}
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
