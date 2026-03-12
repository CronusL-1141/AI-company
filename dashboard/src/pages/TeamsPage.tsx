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
        <h1 className="text-2xl font-bold">团队管理</h1>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="mr-2 h-4 w-4" />
          创建团队
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>团队列表</CardTitle>
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
              加载失败: {error.message}
            </p>
          ) : teams.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              暂无团队，点击上方按钮创建第一个团队
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>名称</TableHead>
                  <TableHead>编排模式</TableHead>
                  <TableHead>Agent 数</TableHead>
                  <TableHead>任务数</TableHead>
                  <TableHead>创建时间</TableHead>
                  <TableHead className="text-right">操作</TableHead>
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
                      {new Date(team.created_at).toLocaleString('zh-CN')}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          render={<Link to={`/teams/${team.id}`} />}
                        >
                          <Eye className="mr-1 h-3 w-3" />
                          详情
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
                          删除
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
              <DialogTitle>创建团队</DialogTitle>
              <DialogDescription>
                创建一个新的 AI Agent 团队
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="team-name">团队名称</Label>
                <Input
                  id="team-name"
                  placeholder="输入团队名称"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label>编排模式</Label>
                <Select value={newMode} onValueChange={(v) => v && setNewMode(v)}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="coordinate">coordinate（协调模式）</SelectItem>
                    <SelectItem value="broadcast">broadcast（广播模式）</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <DialogFooter>
              <Button type="submit" disabled={createTeam.isPending || !newName.trim()}>
                {createTeam.isPending ? '创建中...' : '创建'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
            <DialogDescription>
              确定要删除团队「{deleteTarget?.name}」吗？此操作不可撤销。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteOpen(false)}>
              取消
            </Button>
            <Button
              variant="destructive"
              onClick={handleDelete}
              disabled={deleteTeam.isPending}
            >
              {deleteTeam.isPending ? '删除中...' : '确认删除'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
