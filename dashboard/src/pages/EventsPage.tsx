import { useState, useMemo } from 'react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableHeader,
  TableHead,
  TableBody,
  TableRow,
} from '@/components/ui/table';
import { EventRow } from '@/components/events/EventRow';
import { useEvents } from '@/api/events';
import { useWSStore } from '@/stores/websocket';
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents';
import { Radio, Wifi, WifiOff } from 'lucide-react';
import type { Event } from '@/types';

const EVENT_TYPE_OPTIONS = [
  { value: '__all__', label: '全部类型' },
  { value: 'team', label: 'team.*' },
  { value: 'agent', label: 'agent.*' },
  { value: 'task', label: 'task.*' },
  { value: 'system', label: 'system.*' },
];

const LIMIT_OPTIONS = [
  { value: '50', label: '50条' },
  { value: '100', label: '100条' },
  { value: '200', label: '200条' },
];

export function EventsPage() {
  useRealtimeEvents();

  const [typeFilter, setTypeFilter] = useState('__all__');
  const [sourceFilter, setSourceFilter] = useState('');
  const [limit, setLimit] = useState('50');

  const { connected, events: wsEvents } = useWSStore();

  const { data, isLoading, error } = useEvents({
    type: typeFilter === '__all__' ? undefined : typeFilter,
    source: sourceFilter || undefined,
    limit: Number(limit),
  });

  const apiEvents = data?.data ?? [];

  // Merge WS events on top of API events, deduplicated by id
  const mergedEvents = useMemo(() => {
    const seen = new Set<string>();
    const result: (Event & { _isNew?: boolean })[] = [];

    // WS events first (newer)
    for (const ws of wsEvents) {
      const ev: Event & { _isNew?: boolean } = {
        id: ws.timestamp + ws.type + ws.source,
        type: ws.type,
        source: ws.source,
        data: ws.data,
        timestamp: ws.timestamp,
        _isNew: true,
      };
      // Apply filters
      if (typeFilter !== '__all__' && !ev.type.startsWith(typeFilter)) continue;
      if (sourceFilter && !ev.source.includes(sourceFilter)) continue;
      if (!seen.has(ev.id)) {
        seen.add(ev.id);
        result.push(ev);
      }
    }

    // Then API events
    for (const ev of apiEvents) {
      if (!seen.has(ev.id)) {
        seen.add(ev.id);
        result.push(ev);
      }
    }

    return result.slice(0, Number(limit));
  }, [wsEvents, apiEvents, typeFilter, sourceFilter, limit]);

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <Radio className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">事件日志</h1>
        </div>

        <div className="flex items-center gap-2">
          {connected ? (
            <Badge className="bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400">
              <Wifi className="h-3 w-3 mr-1" />
              已连接
            </Badge>
          ) : (
            <Badge className="bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400">
              <WifiOff className="h-3 w-3 mr-1" />
              未连接
            </Badge>
          )}
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <Select value={typeFilter} onValueChange={(v) => setTypeFilter(v ?? '__all__')}>
          <SelectTrigger className="w-[140px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {EVENT_TYPE_OPTIONS.map((opt) => (
              <SelectItem key={opt.value} value={opt.value}>
                {opt.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Input
          placeholder="按来源过滤..."
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value)}
          className="w-[180px]"
        />

        <Select value={limit} onValueChange={(v) => setLimit(v ?? '50')}>
          <SelectTrigger className="w-[100px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {LIMIT_OPTIONS.map((opt) => (
              <SelectItem key={opt.value} value={opt.value}>
                {opt.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Event Table */}
      {isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-10 w-full" />
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      ) : error ? (
        <div className="rounded-lg border border-destructive/50 bg-destructive/5 p-6 text-center">
          <p className="text-sm text-destructive">加载事件失败：{(error as Error).message}</p>
        </div>
      ) : mergedEvents.length === 0 ? (
        <div className="rounded-lg border bg-muted/30 p-12 text-center">
          <p className="text-sm text-muted-foreground">暂无事件记录</p>
        </div>
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead className="w-[120px]">时间</TableHead>
                <TableHead className="w-[140px]">类型</TableHead>
                <TableHead className="w-[120px]">来源</TableHead>
                <TableHead>摘要</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {mergedEvents.map((event) => (
                <EventRow
                  key={event.id}
                  event={event}
                  isNew={'_isNew' in event && event._isNew}
                />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
