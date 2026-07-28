import { Activity, CheckCircle2, XCircle, Loader2, Clock, ChevronDown, ChevronUp } from 'lucide-react';
import { useState } from 'react';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useEcosystemRecentScanRuns } from '@/api/ecosystem';
import type { EcosystemScanRun } from '@/api/ecosystem';
import { formatDateTimeCompact, serverTimeMs } from '@/lib/datetime';

function StatusIcon({ status }: { status: string }) {
  switch (status) {
    case 'completed':
      return <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" aria-label="完成" />;
    case 'running':
      return <Loader2 className="h-3.5 w-3.5 text-blue-500 animate-spin" aria-label="进行中" />;
    case 'failed':
      return <XCircle className="h-3.5 w-3.5 text-rose-500" aria-label="失败" />;
    default:
      return <Clock className="h-3.5 w-3.5 text-muted-foreground" aria-label={status} />;
  }
}

function formatDateTime(iso: string | null): string {
  return formatDateTimeCompact(iso);
}

function formatDuration(start: string, end: string | null): string {
  if (!end) {
    const ms = Date.now() - serverTimeMs(start);
    if (Number.isNaN(ms) || ms < 0) return '进行中';
    const sec = Math.floor(ms / 1000);
    if (sec < 60) return `进行中 · ${sec}s`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `进行中 · ${min}m ${sec % 60}s`;
    return `进行中 · ${Math.floor(min / 60)}h ${min % 60}m`;
  }
  const ms = serverTimeMs(end) - serverTimeMs(start);
  if (Number.isNaN(ms) || ms < 0) return '—';
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ${sec % 60}s`;
  return `${Math.floor(min / 60)}h ${min % 60}m`;
}

function ScanRunRow({ run, isLatest }: { run: EcosystemScanRun; isLatest?: boolean }) {
  return (
    <div className="flex items-center gap-3 text-xs py-1.5 border-b border-border/30 last:border-0">
      <StatusIcon status={run.status} />
      <span className="font-medium min-w-[120px]">{formatDateTime(run.started_at)}</span>
      <Badge variant="outline" className="text-[10px]">
        {run.scan_type}
      </Badge>
      <span className="text-muted-foreground" title="耗时（基于 started_at→completed_at 实时计算）">
        {formatDuration(run.started_at, run.completed_at)}
      </span>
      <span className="text-muted-foreground">
        <span title="首次入档的仓数（DB 之前没有）">
          新增 <span className="text-foreground font-medium">{run.repos_added}</span>
        </span>
        {' · '}
        <span
          title={
            run.metadata_changed_count != null && run.metadata_changed_count > 0
              ? `已在 DB 的仓重新拉取元数据。${run.metadata_changed_count} 个真实有 topics/stars/description 变化，剩余仅刷新 last_scanned_at`
              : '已在 DB 的仓重新拉取元数据（无真实变化）'
          }
        >
          更新 <span className="text-foreground font-medium">{run.repos_updated}</span>
          {run.metadata_changed_count != null && run.metadata_changed_count > 0 && (
            <span className="text-amber-600 dark:text-amber-400">
              {' '}(含 {run.metadata_changed_count} 真实变化)
            </span>
          )}
        </span>
      </span>
      {isLatest && (
        <Badge variant="outline" className="text-[10px] bg-primary/10 text-primary border-primary/30 ml-auto">
          最新
        </Badge>
      )}
    </div>
  );
}

/**
 * 生态档案首页顶部 — 最近批次扫描概览（默认显示最新 1 次，可展开看 N 次）。
 * 数据：GET /api/ecosystem/scan-runs?limit=5
 *
 * v1.5.2 重构：批次扫描记录从单仓详情页移到生态档案页（生态级数据 ≠ 单仓级）。
 */
export function RecentScanRunsBar() {
  const { data, isLoading } = useEcosystemRecentScanRuns(5);
  const [expanded, setExpanded] = useState(false);

  if (isLoading) {
    return (
      <Card className="mx-4 mt-2 mb-1">
        <CardContent className="py-2 text-xs text-muted-foreground">加载批次扫描记录…</CardContent>
      </Card>
    );
  }

  const runs = data?.data ?? [];
  if (runs.length === 0) {
    return (
      <Card className="mx-4 mt-2 mb-1">
        <CardContent className="py-2 text-xs text-muted-foreground flex items-center gap-2">
          <Activity className="h-3.5 w-3.5" aria-hidden="true" />
          暂无批次扫描记录
        </CardContent>
      </Card>
    );
  }

  const latest = runs[0];
  const rest = runs.slice(1);

  return (
    <Card className="mx-4 mt-2 mb-1">
      <CardContent className="py-2 px-3">
        <div className="flex items-center gap-2 mb-1">
          <Activity className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
          <span className="text-xs font-medium text-muted-foreground">批次扫描记录</span>
          {rest.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 px-2 text-xs ml-auto"
              onClick={() => setExpanded((v) => !v)}
              aria-label={expanded ? '收起历史' : '展开历史'}
            >
              {expanded ? (
                <>
                  <ChevronUp className="h-3 w-3 mr-1" aria-hidden="true" />
                  收起 ({rest.length} 历史)
                </>
              ) : (
                <>
                  <ChevronDown className="h-3 w-3 mr-1" aria-hidden="true" />
                  展开 ({rest.length} 历史)
                </>
              )}
            </Button>
          )}
        </div>
        <ScanRunRow run={latest} isLatest />
        {expanded && rest.map((run) => <ScanRunRow key={run.id} run={run} />)}
      </CardContent>
    </Card>
  );
}
