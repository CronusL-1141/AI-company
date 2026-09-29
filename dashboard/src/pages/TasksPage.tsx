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
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { KanbanColumn, type WallFilter } from '@/components/tasks/KanbanColumn';
import { CompletedSection } from '@/components/tasks/CompletedSection';
import { TaskDetailDialog } from '@/components/tasks/TaskDetailDialog';
import { WallOverview } from '@/components/tasks/WallOverview';
import { WallStatsStrip } from '@/components/tasks/WallStatsStrip';
import { useToast } from '@/components/shared/useToast';
import { useProjects } from '@/api/projects';
import { useProjectTaskWall, useRunTask, useTask } from '@/api/tasks';
import { useTeams } from '@/api/teams';
import { useT } from '@/i18n';
import { Plus, LayoutGrid } from 'lucide-react';
import type { Task } from '@/types';

export function TasksPage() {
  const t = useT();
  const { data: projectsData, isLoading: projectsLoading } = useProjects();
  const projects = projectsData?.data ?? [];

  const { data: teamsData } = useTeams();
  const teams = useMemo(() => teamsData?.data ?? [], [teamsData?.data]);

  const [selectedProjectId, setSelectedProjectId] = useState<string>('');
  const activeProjectId = selectedProjectId || projects[0]?.id || '';

  const { data: wallData, isLoading: wallLoading, error: wallError } = useProjectTaskWall(activeProjectId);

  // Rows arrive in wall order (pending by score, then running, blocked, failed).
  const grouped = useMemo(() => ({
    short: wallData?.wall?.short ?? [],
    mid: wallData?.wall?.mid ?? [],
    long: wallData?.wall?.long ?? [],
  }), [wallData]);
  const digest = wallData?.digest;
  const notLoaded = wallData?.not_shown?.pending ?? 0;

  // Stats-strip filter over the three columns.
  const [filter, setFilter] = useState<WallFilter>('all');

  // Detail dialog: the task is fetched in full when opened (wall rows are cards
  // without description or result); an open task's card fills in meanwhile.
  const [detailId, setDetailId] = useState<string | null>(null);
  const openRow = useMemo(() => {
    if (!detailId) return null;
    return [...grouped.short, ...grouped.mid, ...grouped.long].find((task) => task.id === detailId) ?? null;
  }, [detailId, grouped]);
  const { data: fetchedTask } = useTask(detailId ?? '');
  const fetched = fetchedTask?.data?.id === detailId ? fetchedTask.data : null;
  const detailTask: Task | null = detailId ? (fetched ? { ...openRow, ...fetched } : openRow) : null;

  // New task dialog
  const [newTaskOpen, setNewTaskOpen] = useState(false);
  const [newTaskTitle, setNewTaskTitle] = useState('');
  const [newTaskDesc, setNewTaskDesc] = useState('');
  const [newTaskTeamId, setNewTaskTeamId] = useState('');
  const runTask = useRunTask();
  const { showToast, toastNode } = useToast();

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
        onSuccess: (res) => {
          setNewTaskOpen(false);
          setNewTaskTitle('');
          setNewTaskDesc('');
          setNewTaskTeamId('');
          showToast(res._hint ?? res.message);
        },
      },
    );
  }

  return (
    <div className="space-y-4">
      {toastNode}

      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex shrink-0 items-center gap-2">
          <LayoutGrid className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">{t.tasks.title}</h1>
        </div>

        <div className="flex min-w-0 flex-wrap items-center gap-2">
          {projectsLoading ? (
            <Skeleton className="h-8 w-40" />
          ) : (
            <Select value={selectedProjectId || activeProjectId} onValueChange={(v) => { setSelectedProjectId(v ?? ''); setFilter('all'); }}>
              <SelectTrigger className="w-[220px] max-w-full">
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

          <Button onClick={() => { runTask.reset(); setNewTaskOpen(true); }} disabled={!activeProjectId || projectTeams.length === 0}>
            <Plus className="h-4 w-4" />
            {t.tasks.createTask}
          </Button>
        </div>
      </div>

      {/* Whole-wall stats and the three overview lists: the Leader briefing's digest */}
      {digest && !wallLoading && (
        <>
          <WallStatsStrip digest={digest} filter={filter} onFilter={setFilter} />
          <WallOverview digest={digest} onOpen={setDetailId} />
        </>
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
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            {HORIZON_COLUMNS.map((col) => (
              <KanbanColumn
                key={col.horizon}
                title={col.title}
                badgeClassName={col.badgeClassName}
                tasks={grouped[col.horizon]}
                filter={filter}
                staleDays={digest?.stale_days ?? 7}
                onTaskClick={(task) => setDetailId(task.id)}
              />
            ))}
          </div>
          {notLoaded > 0 && (
            <p className="text-xs text-muted-foreground">{t.tasks.pendingNotLoaded(notLoaded)}</p>
          )}

          {/* Completed tasks: counts from the digest, short rows loaded when opened */}
          {digest && (
            <CompletedSection
              projectId={activeProjectId}
              total={digest.completed_total}
              lastWeek={digest.closed_7d}
              onOpen={setDetailId}
            />
          )}
        </>
      )}

      {/* Task Detail Dialog */}
      <TaskDetailDialog
        task={detailTask}
        open={!!detailTask}
        onOpenChange={(open) => { if (!open) setDetailId(null); }}
      />

      {/* New Task Dialog */}
      <Dialog open={newTaskOpen} onOpenChange={setNewTaskOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t.tasks.createTask}</DialogTitle>
            <DialogDescription>{t.tasks.createTaskHint}</DialogDescription>
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
            {runTask.isError && (
              <p role="alert" className="text-sm text-destructive">{t.common.submitFailed(runTask.error.message)}</p>
            )}
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
