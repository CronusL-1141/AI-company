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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Eye, Trash2 } from 'lucide-react';
import { useTeams, useDeleteTeam, useTeamStatus } from '@/api/teams';
import { useWorkflow } from '@/api/workflows';
import type { Team } from '@/types';
import { useT } from '@/i18n';

// CC Workflow（ultracode）自动追踪的运行团队徽章。可点击跳转到 /workflows 观测详情；
// workflow-session-* 是会话级兜底团队（wf_id 迟到期的临时归组），不打徽章。
function WorkflowBadge({ team }: { team: Team }) {
  if (!team.name.startsWith('workflow-') || team.name.startsWith('workflow-session-')) {
    return null;
  }
  // 反查 wf_id：优先 config.workflow_run_id，退化到团队名 workflow-<wf_id> 去前缀。
  const wfId =
    (team.config?.workflow_run_id as string | undefined) ??
    team.name.replace(/^workflow-/, '');
  const badge = (
    <Badge
      variant="outline"
      className="border-violet-400 text-violet-600 text-[10px]"
      title="CC Workflow（ultracode）自动追踪的运行"
    >
      工作流
    </Badge>
  );
  if (!wfId) return badge;
  return (
    <Link
      to={`/workflows/${encodeURIComponent(wfId)}`}
      className="inline-flex hover:opacity-80"
      title="点击查看该 Workflow 运行的遥测详情"
    >
      {badge}
    </Link>
  );
}

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

function TeamAgentCount({ team }: { team: Team }) {
  const { data, isLoading } = useTeamStatus(team.id);
  if (isLoading) return <Skeleton className="h-4 w-8 inline-block" />;
  return <>{data?.data?.agents.length ?? 0}</>;
}

function TeamTaskCount({ team }: { team: Team }) {
  const { data, isLoading } = useTeamStatus(team.id);
  if (isLoading) return <Skeleton className="h-4 w-8 inline-block" />;
  return <>{data?.data?.total_tasks ?? 0}</>;
}

export function TeamsPage() {
  const t = useT();
  const { data, isLoading, error } = useTeams();
  const teams = data?.data ?? [];
  const deleteTeam = useDeleteTeam();

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Team | null>(null);

  function handleDelete() {
    if (!deleteTarget) return;
    deleteTeam.mutate(deleteTarget.id, {
      onSuccess: () => {
        setDeleteOpen(false);
        setDeleteTarget(null);
      },
    });
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t.teams.title}</h1>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t.teams.teamList}</CardTitle>
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
              {t.teams.loadFailed(error.message)}
            </p>
          ) : teams.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              {t.teams.noTeams}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t.teams.colName}</TableHead>
                  <TableHead>{t.teams.colMode}</TableHead>
                  <TableHead>{t.teams.colAgentCount}</TableHead>
                  <TableHead>{t.teams.colTaskCount}</TableHead>
                  <TableHead>{t.teams.colCreatedAt}</TableHead>
                  <TableHead className="text-right">{t.teams.colActions}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {teams.map((team) => (
                  <TableRow key={team.id}>
                    <TableCell className="font-medium">
                      <span className="inline-flex items-center gap-2">
                        <TeamDisplayName team={team} />
                        <WorkflowBadge team={team} />
                      </span>
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary">{team.mode}</Badge>
                    </TableCell>
                    <TableCell>
                      <TeamAgentCount team={team} />
                    </TableCell>
                    <TableCell>
                      <TeamTaskCount team={team} />
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {new Date(team.created_at).toLocaleString()}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          render={<Link to={`/projects/${team.id}`} />}
                        >
                          <Eye className="mr-1 h-3 w-3" />
                          {t.teams.viewDetail}
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => {
                            setDeleteTarget(team);
                            setDeleteOpen(true);
                          }}
                        >
                          <Trash2 className="mr-1 h-3 w-3 text-destructive" />
                          {t.teams.deleteTeam}
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
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
            <DialogTitle>{t.teams.confirmDelete}</DialogTitle>
            <DialogDescription>
              {t.teams.confirmDeleteDesc(deleteTarget?.name ?? '')}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteOpen(false)}>
              {t.common.cancel}
            </Button>
            <Button
              variant="destructive"
              onClick={handleDelete}
              disabled={deleteTeam.isPending}
            >
              {deleteTeam.isPending ? t.teams.deleting : t.teams.confirmDelete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
