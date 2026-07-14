import { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  CheckCircle2, XCircle, ArrowLeft, Loader2, RefreshCw, Play, Star,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import {
  useShallowBatch,
  useShallowBatchItems,
  useApproveBatch,
  useCancelBatch,
} from '@/api/ecosystem';

function formatDatetime(iso: string | null) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false });
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    pending_approval: '待批准',
    approved: '已批准',
    running: '运行中',
    completed: '已完成',
    cancelled: '已取消',
  };
  const label = map[status] ?? status;
  if (status === 'pending_approval') return <Badge variant="destructive">{label}</Badge>;
  if (status === 'completed') return <Badge variant="outline" className="border-green-400 text-green-600">{label}</Badge>;
  if (status === 'cancelled') return <Badge variant="secondary">{label}</Badge>;
  if (status === 'running') return <Badge variant="outline" className="border-orange-400 text-orange-600">{label}</Badge>;
  return <Badge variant="outline">{label}</Badge>;
}

/**
 * 浅扫批次详情页 — 展示批次元信息 + 候选仓清单，支持批准/取消操作。
 * 路由: /ecosystem/batches/:batchId
 */
export function EcosystemBatchDetailPage() {
  const { batchId } = useParams<{ batchId: string }>();
  const navigate = useNavigate();
  const { data: batchData, isLoading: batchLoading, refetch } = useShallowBatch(batchId ?? null);
  const { data: itemsData, isLoading: itemsLoading } = useShallowBatchItems(batchId ?? null);
  const approveBatch = useApproveBatch();
  const cancelBatch = useCancelBatch();
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const batch = batchData?.batch;
  const items = itemsData?.items ?? [];
  const canApprove = batch?.status === 'pending_approval';
  const canCancel = batch?.status === 'pending_approval' || batch?.status === 'running';

  const handleApprove = () => {
    if (!batchId) return;
    approveBatch.mutate(
      { batchId, approvedBy: 'user' },
      {
        onSuccess: (res) => {
          // worker.tick() 只是把浅扫任务派进队列——真正的深度评审摘要仍需活跃 CC 会话领取
          // dispatch intent 才会执行，这里把后端 message 和这层诚实说明一起透出（用户 2026-07-14 拍板）。
          setActionMsg(`${res.message}；深度评审仍需活跃的 CC 会话领取 dispatch intent 才会真正执行。`);
          refetch();
          setTimeout(() => setActionMsg(null), 7000);
        },
      },
    );
  };

  const handleCancel = () => {
    if (!batchId) return;
    cancelBatch.mutate(batchId, {
      onSuccess: () => {
        setActionMsg('批次已取消');
        refetch();
        setTimeout(() => setActionMsg(null), 3000);
      },
    });
  };

  if (batchLoading) {
    return (
      <div className="flex items-center justify-center py-24 text-muted-foreground">
        <Loader2 className="w-5 h-5 animate-spin mr-2" />
        加载中...
      </div>
    );
  }

  if (!batch) {
    return (
      <div className="p-6 text-center text-muted-foreground">
        <p>批次不存在或已被删除</p>
        <Button variant="link" onClick={() => navigate('/ecosystem/batches')}>
          返回批次列表
        </Button>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-4 max-w-5xl mx-auto">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate('/ecosystem/batches')}>
          <ArrowLeft className="w-4 h-4 mr-1" />
          返回列表
        </Button>
        <h1 className="text-xl font-bold">批次详情</h1>
        <StatusBadge status={batch.status} />
        {batch.status === 'pending_approval' && (
          <Badge variant="destructive" className="animate-pulse ml-1">需要批准</Badge>
        )}
      </div>

      {actionMsg && (
        <div className="bg-green-50 dark:bg-green-950/30 border border-green-200 dark:border-green-800 rounded-md px-4 py-2 text-sm text-green-700 dark:text-green-400">
          {actionMsg}
        </div>
      )}

      {/* 批次元信息 */}
      <Card>
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">批次信息</CardTitle>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => refetch()}>
                <RefreshCw className="w-4 h-4 mr-1" />
                刷新
              </Button>
              {canApprove && (
                <Button size="sm" onClick={handleApprove} disabled={approveBatch.isPending}>
                  {approveBatch.isPending ? (
                    <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                  ) : (
                    <CheckCircle2 className="w-4 h-4 mr-1" />
                  )}
                  批准
                </Button>
              )}
              {batch.status === 'running' && (
                <Button size="sm" variant="outline" onClick={() => navigate('/ecosystem/shallow-queue')}>
                  <Play className="w-4 h-4 mr-1" />
                  查看进度
                </Button>
              )}
              {canCancel && (
                <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={handleCancel} disabled={cancelBatch.isPending}>
                  <XCircle className="w-4 h-4 mr-1" />
                  取消批次
                </Button>
              )}
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 md:grid-cols-3 gap-4 text-sm">
            <div>
              <dt className="text-muted-foreground text-xs">批次 ID</dt>
              <dd className="font-mono text-xs mt-0.5">{batch.id}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">触发源</dt>
              <dd className="font-medium mt-0.5">{batch.triggered_by}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">候选仓数</dt>
              <dd className="font-medium mt-0.5">{batch.candidates_count}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">创建时间</dt>
              <dd className="mt-0.5">{formatDatetime(batch.created_at)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">批准时间</dt>
              <dd className="mt-0.5">{formatDatetime(batch.approved_at)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">完成时间</dt>
              <dd className="mt-0.5">{formatDatetime(batch.completed_at)}</dd>
            </div>
            {batch.status !== 'pending_approval' && (
              <>
                <div>
                  <dt className="text-muted-foreground text-xs">新增仓</dt>
                  <dd className="font-medium mt-0.5 text-green-600">{batch.new_repos_count}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground text-xs">更新仓</dt>
                  <dd className="font-medium mt-0.5">{batch.updated_repos_count}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground text-xs">Metadata 变化</dt>
                  <dd className="font-medium mt-0.5 text-amber-600">{batch.metadata_changed_count}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground text-xs">失败</dt>
                  <dd className="font-medium mt-0.5 text-red-500">{batch.failed_count}</dd>
                </div>
              </>
            )}
            {batch.trigger_reason && (
              <div className="col-span-2 md:col-span-3">
                <dt className="text-muted-foreground text-xs">触发原因</dt>
                <dd className="mt-0.5 text-sm">{batch.trigger_reason}</dd>
              </div>
            )}
          </dl>
        </CardContent>
      </Card>

      {/* 候选仓清单 */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">
            候选仓清单
            <span className="ml-2 text-sm text-muted-foreground font-normal">
              共 {itemsData?.total ?? batch.candidates_count} 仓
            </span>
            {batch.status === 'pending_approval' && (
              <Badge variant="outline" className="ml-2 text-[10px]">预览 — 批准后正式入队</Badge>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {itemsLoading ? (
            <div className="flex items-center justify-center py-8 text-muted-foreground">
              <Loader2 className="w-4 h-4 animate-spin mr-2" />
              加载候选仓...
            </div>
          ) : items.length === 0 ? (
            <div className="text-center py-8 text-sm text-muted-foreground">
              {batch.status === 'pending_approval'
                ? '候选仓列表为空（所有仓均为最新状态）'
                : '暂无数据'}
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>仓库</TableHead>
                  <TableHead className="text-center">Stars</TableHead>
                  <TableHead>阶段状态</TableHead>
                  <TableHead>浅扫摘要预览</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((item, i) => (
                  <TableRow key={item.repo_id ?? i}>
                    <TableCell>
                      <button
                        className="font-mono text-xs text-primary hover:underline text-left"
                        onClick={() => window.open(`https://github.com/${item.repo_full_name}`, '_blank')}
                      >
                        {item.repo_full_name}
                      </button>
                    </TableCell>
                    <TableCell className="text-center">
                      <span className="flex items-center justify-center gap-1 text-xs">
                        <Star className="w-3 h-3 text-amber-400" />
                        {item.stars.toLocaleString()}
                      </span>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline" className="text-[10px]">
                        {item.stage_status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground max-w-[300px] truncate">
                      {item.shallow_summary_excerpt || '—'}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
