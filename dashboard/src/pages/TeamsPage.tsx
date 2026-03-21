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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Plus, Eye, Trash2 } from 'lucide-react';
import { useTeams, useCreateTeam, useDeleteTeam, useTeamStatus } from '@/api/teams';
import type { Team } from '@/types';
import { useT } from '@/i18n';

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
  const createTeam = useCreateTeam();
  const deleteTeam = useDeleteTeam();

  const [createOpen, setCreateOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Team | null>(null);
  const [newName, setNewName] = useState('');
  const [newMode, setNewMode] = useState('coordinate');

  function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    createTeam.mutate(
      { name: newName.trim(), mode: newMode },
      {
        onSuccess: () => {
          setCreateOpen(false);
          setNewName('');
          setNewMode('coordinate');
        },
      }
    );
  }

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
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="mr-2 h-4 w-4" />
          {t.teams.createTeam}
        </Button>
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
                    <TableCell className="font-medium">{team.name}</TableCell>
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

      {/* Create Team Dialog */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="sm:max-w-md">
          <form onSubmit={handleCreate}>
            <DialogHeader>
              <DialogTitle>{t.teams.createTitle}</DialogTitle>
              <DialogDescription>
                {t.teams.createDesc}
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="team-name">{t.teams.teamName}</Label>
                <Input
                  id="team-name"
                  placeholder={t.teams.teamNamePlaceholder}
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label>{t.teams.modeLabel}</Label>
                <Select value={newMode} onValueChange={(v) => v && setNewMode(v)}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="coordinate">{t.teams.modeCoordinate}</SelectItem>
                    <SelectItem value="broadcast">{t.teams.modeBroadcast}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <DialogFooter>
              <Button type="submit" disabled={createTeam.isPending || !newName.trim()}>
                {createTeam.isPending ? t.teams.creating : t.common.create}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

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
