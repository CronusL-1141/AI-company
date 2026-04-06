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
import { Badge } from '@/components/ui/badge';
import { KanbanColumn } from '@/components/tasks/KanbanColumn';
import { TaskCard } from '@/components/tasks/TaskCard';
import { TaskDetailDialog } from '@/components/tasks/TaskDetailDialog';
import { useProjects } from '@/api/projects';
import { useProjectTaskWall, useRunTask } from '@/api/tasks';
import { useTeams } from '@/api/teams';
import { useT } from '@/i18n';
import { Plus, LayoutGrid, ChevronDown, CheckCircle2 } from 'lucide-react';
import type { Task } from '@/types';

function sortByScore(tasks: Task[]): Task[] {
  return [...tasks].sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
}

export function TasksPage() {
  const t = useT();
  const { data: projectsData, isLoading: projectsLoading } = useProjects();
  const projects = projectsData?.data ?? [];

  const { data: teamsData } = useTeams();
  const teams = useMemo(() => teamsData?.data ?? [], [teamsData?.data]);

  const [selectedProjectId, setSelectedProjectId] = useState<string>('');
  const activeProjectId = selectedProjectId || projects[0]?.id || '';

  const { data: wallData, isLoading: wallLoading, error: wallError } = useProjectTaskWall(activeProjectId);

  const grouped = useMemo(() => {
    if (!wallData?.wall) return { short: [], mid: [], long: [] };
    return {
      short: sortByScore(wallData.wall.short ?? []),
      mid: sortByScore(wallData.wall.mid ?? []),
      long: sortByScore(wallData.wall.long ?? []),
    };
  }, [wallData]);

  const completedTasks = useMemo(() => {
    return wallData?.completed ?? [];
  }, [wallData]);

  const stats = wallData?.stats;

  // Detail dialog
  const [detailTask, setDetailTask] = useState<Task | null>(null);

  // Completed section collapse
  const [completedOpen, setCompletedOpen] = useState(false);

  // New task dialog
  const [newTaskOpen, setNewTaskOpen] = useState(false);
  const [newTaskTitle, setNewTaskTitle] = useState('');
  const [newTaskDesc, setNewTaskDesc] = useState('');
  const [newTaskTeamId, setNewTaskTeamId] = useState('');
  const runTask = useRunTask();

  // 当前项目下的团队
  const projectTeams = useMemo(() => {
    if (!activeProjectId) return [];
    return teams.filter((tm) => tm.project_id === activeProjectId);
  }, [teams, activeProjectId]);

  const HORIZON_COLUMNS = [
    { horizon: 'short' as const, title: t.tasks.horizonShort, badgeClassName: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400' },
    { horizon: 'mid' as const, title: t.tasks.horizonMid, badgeClassName: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400' },
    { horizon: 'long' as const, title: t.tasks.horizonLong, badgeClassName: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400' },
  ];

  function handleSubmitTask() {
    const teamId = newTaskTeamId || projectTeams[0]?.id;
    if (!teamId || !newTaskTitle.trim()) return;
    runTask.mutate(
      { team_id: teamId, title: newTaskTitle.trim(), description: newTaskDesc.trim() },
      {
        onSuccess: () => {
          setNewTaskOpen(false);
          setNewTaskTitle('');
          setNewTaskDesc('');
          setNewTaskTeamId('');
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
          <h1 className="text-lg font-semibold">{t.tasks.title}</h1>
        </div>

        <div className="flex items-center gap-2">
          {projectsLoading ? (
            <Skeleton className="h-8 w-40" />
          ) : (
            <Select value={selectedProjectId || activeProjectId} onValueChange={(v) => setSelectedProjectId(v ?? '')}>
              <SelectTrigger className="w-[220px]">
                <SelectValue placeholder={t.tasks.selectProject}>
                  {projects.find((p) => p.id === (selectedProjectId || activeProjectId))?.name ?? t.tasks.selectProject}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {projects.map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    {p.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}

          <Button onClick={() => setNewTaskOpen(true)} disabled={!activeProjectId || projectTeams.length === 0}>
            <Plus className="h-4 w-4" />
            {t.tasks.executeTask}
          </Button>
        </div>
      </div>

      {/* Stats Bar */}
      {stats && (
        <div className="flex items-center gap-4 text-sm text-muted-foreground flex-wrap">
          <span>{t.tasks.totalTasks(stats.total)}</span>
          <span>{t.tasks.avgScore(stats.avg_score?.toFixed(1) ?? '-')}</span>
          {stats.by_priority && Object.entries(stats.by_priority).map(([k, v]) => (
            <span key={k}>{k}: {v as number}</span>
          ))}
          {stats.completed_count != null && (
            <span>{t.tasks.completed(stats.completed_count)}</span>
          )}
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
          <p className="text-sm text-destructive">{t.tasks.loadFailed((wallError as Error).message)}</p>
        </div>
      ) : !activeProjectId ? (
        <div className="rounded-lg border bg-muted/30 p-12 text-center">
          <p className="text-sm text-muted-foreground">{t.tasks.selectProjectHint}</p>
        </div>
      ) : (
        <>
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

          {/* Completed Tasks - Collapsible */}
          {completedTasks.length > 0 && (
            <div>
              <Button
                variant="ghost"
                className="w-full justify-between px-3 py-2 h-auto"
                onClick={() => setCompletedOpen(!completedOpen)}
              >
                <div className="flex items-center gap-2">
                  <CheckCircle2 className="h-4 w-4 text-green-600" />
                  <span className="text-sm font-medium">{t.tasks.completedSection}</span>
                  <Badge variant="secondary">{completedTasks.length}</Badge>
                </div>
                <ChevronDown className={`h-4 w-4 transition-transform ${completedOpen ? 'rotate-180' : ''}`} />
              </Button>
              {completedOpen && (
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 mt-2">
                  {completedTasks.slice(0, 20).map((task) => (
                    <TaskCard
                      key={task.id}
                      task={task}
                      onClick={() => setDetailTask(task)}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
        </>
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
            <DialogTitle>{t.tasks.executeTask}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            {/* 选择目标团队 */}
            {projectTeams.length > 1 && (
              <div className="space-y-2">
                <Label htmlFor="task-team">{t.tasks.targetTeam}</Label>
                <Select value={newTaskTeamId || projectTeams[0]?.id || ''} onValueChange={(v) => setNewTaskTeamId(v ?? '')}>
                  <SelectTrigger>
                    <SelectValue placeholder={t.tasks.selectTeam} />
                  </SelectTrigger>
                  <SelectContent>
                    {projectTeams.map((tm) => (
                      <SelectItem key={tm.id} value={tm.id}>
                        {tm.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            <div className="space-y-2">
              <Label htmlFor="task-title">{t.tasks.taskTitle}</Label>
              <Input
                id="task-title"
                placeholder={t.tasks.taskTitlePlaceholder}
                value={newTaskTitle}
                onChange={(e) => setNewTaskTitle(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="task-desc">{t.tasks.taskDesc}</Label>
              <Textarea
                id="task-desc"
                placeholder={t.tasks.taskDescPlaceholder}
                value={newTaskDesc}
                onChange={(e) => setNewTaskDesc(e.target.value)}
                rows={4}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setNewTaskOpen(false)}>
              {t.common.cancel}
            </Button>
            <Button
              onClick={handleSubmitTask}
              disabled={!newTaskTitle.trim() || runTask.isPending}
            >
              {runTask.isPending ? t.common.submitting : t.tasks.submit}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
