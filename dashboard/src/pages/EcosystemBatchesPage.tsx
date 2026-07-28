import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CheckCircle2, XCircle, Clock, Play, RefreshCw, Plus, Loader2, ChevronRight } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import type { ShallowBatch } from '@/api/ecosystem';
import {
  useShallowBatches,
  useCreateShallowBatch,
  useApproveBatch,
  useCancelBatch,
} from '@/api/ecosystem';
import { formatDateTime } from '@/lib/datetime';

function formatDatetime(iso: string | null) {
  if (!iso) return '—';
  return formatDateTime(iso, { hour12: false });
}

function StatusBadge({ status }: { status: string }) {
  switch (status) {
    case 'pending_approval':
      return <Badge variant="destructive" className="text-[10px]">待批准</Badge>;
    case 'approved':
      return <Badge variant="outline" className="text-[10px] border-blue-400 text-blue-600">已批准</Badge>;
    case 'running':
      return <Badge variant="outline" className="text-[10px] border-orange-400 text-orange-600">运行中</Badge>;
    case 'completed':
      return <Badge variant="outline" className="text-[10px] border-green-400 text-green-600">已完成</Badge>;
    case 'cancelled':
      return <Badge variant="secondary" className="text-[10px]">已取消</Badge>;
    default:
      return <Badge variant="outline" className="text-[10px]">{status}</Badge>;
  }
}

function StatusIcon({ status }: { status: string }) {
  if (status === 'completed') return <CheckCircle2 className="w-4 h-4 text-green-500" />;
  if (status === 'cancelled') return <XCircle className="w-4 h-4 text-muted-foreground" />;
  if (status === 'running') return <Play className="w-4 h-4 text-orange-500 animate-pulse" />;
  if (status === 'pending_approval') return <Clock className="w-4 h-4 text-red-500" />;
  return <RefreshCw className="w-4 h-4 text-blue-500" />;
}

function BatchRow({ batch, onApprove, onCancel }: {
  batch: ShallowBatch;
  onApprove: (id: string) => void;
  onCancel: (id: string) => void;
}) {
  const navigate = useNavigate();
  const canApprove = batch.status === 'pending_approval';
  const canCancel = batch.status === 'pending_approval' || batch.status === 'running';

  return (
    <TableRow
      className="cursor-pointer hover:bg-muted/30"
      onClick={() => navigate(`/ecosystem/batches/${batch.id}`)}
    >
      <TableCell className="w-8">
        <StatusIcon status={batch.status} />
      </TableCell>
      <TableCell className="font-mono text-xs text-muted-foreground w-24">
        {batch.id.slice(0, 8)}
      </TableCell>
      <TableCell>
        <span className="text-xs text-muted-foreground">{batch.triggered_by}</span>
        {batch.trigger_reason && (
          <p className="text-[10px] text-muted-foreground/60 truncate max-w-[180px]">{batch.trigger_reason}</p>
        )}
      </TableCell>
      <TableCell className="text-center font-medium">{batch.candidates_count}</TableCell>
      <TableCell><StatusBadge status={batch.status} /></TableCell>
      <TableCell className="text-xs text-muted-foreground">{formatDatetime(batch.created_at)}</TableCell>
      <TableCell onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-1">
          {canApprove && (
            <Button
              size="sm"
              variant="default"
              className="h-7 text-xs"
              onClick={() => onApprove(batch.id)}
            >
              批准
            </Button>
          )}
          {canCancel && (
            <Button
              size="sm"
              variant="ghost"
              className="h-7 text-xs text-muted-foreground"
              onClick={() => onCancel(batch.id)}
            >
              取消
            </Button>
          )}
          <ChevronRight className="w-4 h-4 text-muted-foreground ml-1" />
        </div>
      </TableCell>
    </TableRow>
  );
}

/**
 * 浅扫批次列表页 — 列出所有批次，pending_approval 状态高亮，支持批准/取消操作。
 * 路由: /ecosystem/batches
 */
export function EcosystemBatchesPage() {
  const { data, isLoading, refetch } = useShallowBatches(50);
  const createBatch = useCreateShallowBatch();
  const approveBatch = useApproveBatch();
  const cancelBatch = useCancelBatch();
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const batches = data?.batches ?? [];
  const pendingCount = batches.filter((b) => b.status === 'pending_approval').length;

  const handleCreate = () => {
    createBatch.mutate(
      { triggered_by: 'user', trigger_reason: '手动触发浅扫批次' },
      {
        onSuccess: (res) => {
          setActionMsg(res.message);
          setTimeout(() => setActionMsg(null), 4000);
        },
        onError: () => setActionMsg('创建批次失败，请查看后端日志'),
      },
    );
  };

  const handleApprove = (id: string) => {
    approveBatch.mutate(
      { batchId: id, approvedBy: 'user' },
      {
        onSuccess: (res) => {
          // worker.tick() 只是把浅扫任务派进队列——真正的深度评审摘要仍需活跃 CC 会话领取
          // dispatch intent 才会执行，这里把后端 message 和这层诚实说明一起透出（用户 2026-07-14 拍板）。
          setActionMsg(`${res.message}；深度评审仍需活跃的 CC 会话领取 dispatch intent 才会真正执行。`);
          setTimeout(() => setActionMsg(null), 6000);
        },
      },
    );
  };

  const handleCancel = (id: string) => {
    cancelBatch.mutate(id, {
      onSuccess: () => {
        setActionMsg('批次已取消');
        setTimeout(() => setActionMsg(null), 3000);
      },
    });
  };

  return (
    <div className="p-6 space-y-4 max-w-5xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">浅扫批次</h1>
          <p className="text-sm text-muted-foreground mt-1">
            管理浅扫候选仓批次 — 创建批次后批准方可触发 dispatch
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isLoading}>
            <RefreshCw className={`w-4 h-4 mr-1 ${isLoading ? 'animate-spin' : ''}`} />
            刷新
          </Button>
          <Button size="sm" onClick={handleCreate} disabled={createBatch.isPending}>
            {createBatch.isPending ? (
              <Loader2 className="w-4 h-4 mr-1 animate-spin" />
            ) : (
              <Plus className="w-4 h-4 mr-1" />
            )}
            新建批次
          </Button>
        </div>
      </div>

      {pendingCount > 0 && (
        <div className="flex items-center gap-2 bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-800 rounded-md px-4 py-2 text-sm text-red-700 dark:text-red-400">
          <Clock className="w-4 h-4 flex-shrink-0" />
          <span>
            有 <strong>{pendingCount}</strong> 个批次等待批准 — 点击行进入详情页批准
          </span>
        </div>
      )}

      {actionMsg && (
        <div className="bg-green-50 dark:bg-green-950/30 border border-green-200 dark:border-green-800 rounded-md px-4 py-2 text-sm text-green-700 dark:text-green-400">
          {actionMsg}
        </div>
      )}

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">
            批次历史
            {batches.length > 0 && (
              <span className="ml-2 text-sm text-muted-foreground font-normal">
                共 {batches.length} 条
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 className="w-5 h-5 animate-spin mr-2" />
              加载中...
            </div>
          ) : batches.length === 0 ? (
            <div className="text-center py-12 text-muted-foreground text-sm">
              暂无批次记录，点击"新建批次"创建第一个批次
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8"></TableHead>
                  <TableHead>ID</TableHead>
                  <TableHead>触发源</TableHead>
                  <TableHead className="text-center">候选仓数</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>创建时间</TableHead>
                  <TableHead>操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {batches.map((b) => (
                  <BatchRow
                    key={b.id}
                    batch={b}
                    onApprove={handleApprove}
                    onCancel={handleCancel}
                  />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
