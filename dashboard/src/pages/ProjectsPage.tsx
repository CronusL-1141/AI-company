import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Plus, Eye, Trash2, ChevronDown, ChevronRight } from 'lucide-react';
import {
  useProjects,
  useCreateProject,
  useDeleteProject,
  useProjectSummary,
  type ProjectSummary,
} from '@/api/projects';
import { useT } from '@/i18n';
import type { Project } from '@/types';

// Priority badge color mapping
const priorityVariant: Record<string, 'destructive' | 'default' | 'secondary' | 'outline'> = {
  critical: 'destructive',
  high: 'default',
  medium: 'secondary',
  low: 'outline',
};

function ProjectStatusBadge({ projectId }: { projectId: string }) {
  const t = useT();
  const { data, isLoading } = useProjectSummary(projectId);
  if (isLoading) return <Skeleton className="h-5 w-14 inline-block" />;
  const isActive = data?.status === 'active';
  return (
    <Badge variant={isActive ? 'default' : 'secondary'} className={isActive ? 'bg-green-600 hover:bg-green-700' : ''}>
      {isActive ? t.projects.statusActive : t.projects.statusInactive}
    </Badge>
  );
}

function ProjectExpandedRow({
  summary,
  updatedAt,
}: {
  summary: ProjectSummary | undefined;
  updatedAt: string;
}) {
  const t = useT();
  if (!summary) return null;

  return (
    <TableRow>
      <TableCell colSpan={5} className="bg-muted/30 py-3 px-6">
        <div className="flex flex-wrap gap-6 text-sm">
          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground">{t.projects.expandActiveTeams(summary.active_teams)}</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground">{t.projects.expandPendingTasks(summary.pending_tasks)}</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground">{t.projects.expandLastUpdated}:</span>
            <span>{new Date(updatedAt).toLocaleString('zh-CN')}</span>
          </div>
        </div>
        {summary.top_tasks.length > 0 && (
          <div className="mt-2">
            <p className="text-xs text-muted-foreground mb-1.5">{t.projects.expandTopTasks}:</p>
            <div className="flex flex-wrap gap-2">
              {summary.top_tasks.map((task, i) => (
                <Badge key={i} variant={priorityVariant[task.priority] ?? 'outline'} className="text-xs font-normal">
                  {task.title}
                </Badge>
              ))}
            </div>
          </div>
        )}
      </TableCell>
    </TableRow>
  );
}

export function ProjectsPage() {
  const t = useT();
  const { data, isLoading, error } = useProjects();
  const rawProjects: Project[] = data?.data ?? [];
  const createProject = useCreateProject();
  const deleteProject = useDeleteProject();

  const [createOpen, setCreateOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [newRootPath, setNewRootPath] = useState('');
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  // Track loaded summaries per project for sorting
  const [summaryCache, setSummaryCache] = useState<Record<string, ProjectSummary>>({});

  function toggleExpand(id: string) {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function handleSummaryLoaded(id: string, summary: ProjectSummary) {
    setSummaryCache((prev) => (prev[id]?.status === summary.status ? prev : { ...prev, [id]: summary }));
  }

  // Sort: active projects first, then by updated_at desc
  const projects = [...rawProjects].sort((a, b) => {
    const aActive = summaryCache[a.id]?.status === 'active' ? 0 : 1;
    const bActive = summaryCache[b.id]?.status === 'active' ? 0 : 1;
    if (aActive !== bActive) return aActive - bActive;
    return new Date(b.updated_at ?? b.created_at).getTime() - new Date(a.updated_at ?? a.created_at).getTime();
  });

  function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    createProject.mutate(
      {
        name: newName.trim(),
        description: newDesc.trim() || undefined,
        root_path: newRootPath.trim() || undefined,
      },
      {
        onSuccess: () => {
          setCreateOpen(false);
          setNewName('');
          setNewDesc('');
          setNewRootPath('');
        },
      }
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t.projects.title}</h1>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="mr-2 h-4 w-4" />
          {t.projects.createProject}
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t.projects.projectList}</CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : error ? (
            <p className="text-sm text-destructive">
              {t.projects.loadFailed(error.message)}
            </p>
          ) : projects.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              {t.projects.noProjects}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-6" />
                  <TableHead>{t.projects.colName}</TableHead>
                  <TableHead>{t.projects.colDesc}</TableHead>
                  <TableHead>{t.projects.colStatus}</TableHead>
                  <TableHead>{t.projects.colCreatedAt}</TableHead>
                  <TableHead className="text-right">{t.projects.colActions}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {projects.map((project: Project) => (
                  <ProjectRowGroup
                    key={project.id}
                    project={project}
                    expanded={expandedIds.has(project.id)}
                    onToggleExpand={() => toggleExpand(project.id)}
                    onSummaryLoaded={(s) => handleSummaryLoaded(project.id, s)}
                    cachedSummary={summaryCache[project.id]}
                    onDeleteClick={() => {
                      setDeleteTarget(project);
                      setDeleteOpen(true);
                    }}
                  />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t.projects.confirmDelete}</DialogTitle>
            <DialogDescription>
              {t.projects.confirmDeleteDesc(deleteTarget?.name ?? '')}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteOpen(false)}>
              {t.common.cancel}
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (!deleteTarget) return;
                deleteProject.mutate(deleteTarget.id, {
                  onSuccess: () => { setDeleteOpen(false); setDeleteTarget(null); },
                });
              }}
              disabled={deleteProject.isPending}
            >
              {deleteProject.isPending ? t.common.deleting : t.projects.confirmDelete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Create Project Dialog */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="sm:max-w-md">
          <form onSubmit={handleCreate}>
            <DialogHeader>
              <DialogTitle>{t.projects.createTitle}</DialogTitle>
              <DialogDescription>
                {t.projects.createDesc}
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="project-name">{t.projects.projectName}</Label>
                <Input
                  id="project-name"
                  placeholder={t.projects.projectNamePlaceholder}
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="project-desc">{t.projects.projectDesc}</Label>
                <Textarea
                  id="project-desc"
                  placeholder={t.projects.projectDescPlaceholder}
                  value={newDesc}
                  onChange={(e) => setNewDesc(e.target.value)}
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="project-root">{t.projects.rootPath}</Label>
                <Input
                  id="project-root"
                  placeholder={t.projects.rootPathPlaceholder}
                  value={newRootPath}
                  onChange={(e) => setNewRootPath(e.target.value)}
                />
              </div>
            </div>
            <DialogFooter>
              <Button type="submit" disabled={createProject.isPending || !newName.trim()}>
                {createProject.isPending ? t.common.creating : t.common.create}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// Separate component for each project row + optional expanded row
function ProjectRowGroup({
  project,
  expanded,
  onToggleExpand,
  onSummaryLoaded,
  cachedSummary,
  onDeleteClick,
}: {
  project: Project;
  expanded: boolean;
  onToggleExpand: () => void;
  onSummaryLoaded: (s: ProjectSummary) => void;
  cachedSummary: ProjectSummary | undefined;
  onDeleteClick: () => void;
}) {
  const t = useT();
  const { data: summary } = useProjectSummary(project.id);

  // Notify parent when summary loads (for sorting)
  if (summary && summary !== cachedSummary) {
    onSummaryLoaded(summary);
  }

  return (
    <>
      <TableRow
        className="cursor-pointer hover:bg-muted/50"
        onClick={onToggleExpand}
      >
        <TableCell className="pr-0">
          {expanded
            ? <ChevronDown className="h-4 w-4 text-muted-foreground" />
            : <ChevronRight className="h-4 w-4 text-muted-foreground" />
          }
        </TableCell>
        <TableCell className="font-medium">{project.name}</TableCell>
        <TableCell className="text-muted-foreground max-w-[240px] truncate">
          {project.description || '--'}
        </TableCell>
        <TableCell>
          <ProjectStatusBadge projectId={project.id} />
        </TableCell>
        <TableCell className="text-muted-foreground">
          {new Date(project.created_at).toLocaleString('zh-CN')}
        </TableCell>
        <TableCell className="text-right" onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center justify-end gap-1">
            <Button
              variant="ghost"
              size="sm"
              render={<Link to={`/projects/${project.id}`} />}
            >
              <Eye className="mr-1 h-3 w-3" />
              {t.projects.viewDetail}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={onDeleteClick}
            >
              <Trash2 className="mr-1 h-3 w-3 text-destructive" />
              {t.projects.deleteProject}
            </Button>
          </div>
        </TableCell>
      </TableRow>
      {expanded && (
        <ProjectExpandedRow
          summary={summary}
          updatedAt={project.updated_at}
        />
      )}
    </>
  );
}
