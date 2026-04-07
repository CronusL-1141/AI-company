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
import { useProjects } from '@/api/projects';
import { useWSStore } from '@/stores/websocket';
import { useT } from '@/i18n';
import { FolderOpen, Radio, Wifi, WifiOff } from 'lucide-react';
import type { Event } from '@/types';

const EVENT_TYPE_OPTIONS = [
  { value: '__all__', labelKey: 'allTypes' as const },
  { value: 'team', label: 'team.*' },
  { value: 'agent', label: 'agent.*' },
  { value: 'task', label: 'task.*' },
  { value: 'cc', label: 'cc.*' },
  { value: 'system', label: 'system.*' },
];

export function EventsPage() {
  const t = useT();
  const [typeFilter, setTypeFilter] = useState('__all__');
  const [sourceFilter, setSourceFilter] = useState('');
  const [limit, setLimit] = useState('50');
  const [projectFilter, setProjectFilter] = useState('__all__');

  const { data: projectsData } = useProjects();
  const projects = projectsData?.data ?? [];

  const { connected, events: wsEvents } = useWSStore();

  const { data, isLoading, error } = useEvents({
    type: typeFilter === '__all__' ? undefined : typeFilter,
    source: sourceFilter || undefined,
    limit: Number(limit),
    project_id: projectFilter === '__all__' ? undefined : projectFilter,
  });

  const apiEvents = useMemo(() => data?.data ?? [], [data?.data]);

  const LIMIT_OPTIONS = [
    { value: '50', label: t.events.limit50 },
    { value: '100', label: t.events.limit100 },
    { value: '200', label: t.events.limit200 },
  ];

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
          <h1 className="text-lg font-semibold">{t.events.title}</h1>
        </div>

        <div className="flex items-center gap-2">
          <Select value={projectFilter} onValueChange={(v) => setProjectFilter(v ?? '__all__')}>
            <SelectTrigger className="h-8 w-[200px] text-sm">
              <FolderOpen className="mr-1.5 h-3.5 w-3.5" />
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all__">{t.common.allProjects}</SelectItem>
              {projects.map((p) => (
                <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>
              ))}
            </SelectContent>
          </Select>

          {connected ? (
            <Badge className="bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400">
              <Wifi className="h-3 w-3 mr-1" />
              {t.status.connected}
            </Badge>
          ) : (
            <Badge className="bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400">
              <WifiOff className="h-3 w-3 mr-1" />
              {t.status.disconnected}
            </Badge>
          )}
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <Select value={typeFilter} onValueChange={(v) => setTypeFilter(v ?? '__all__')}>
          <SelectTrigger className="w-[140px]">
            <SelectValue placeholder={t.events.allTypes} />
          </SelectTrigger>
          <SelectContent>
            {EVENT_TYPE_OPTIONS.map((opt) => (
              <SelectItem key={opt.value} value={opt.value}>
                {'labelKey' in opt ? (t.events as unknown as Record<string, string>)[opt.labelKey as string] : opt.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Input
          placeholder={t.events.filterBySource}
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
          <p className="text-sm text-destructive">{t.events.loadFailed((error as Error).message)}</p>
        </div>
      ) : mergedEvents.length === 0 ? (
        <div className="rounded-lg border bg-muted/30 p-12 text-center">
          <p className="text-sm text-muted-foreground">{t.events.noEvents}</p>
        </div>
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead className="w-[120px]">{t.events.colTime}</TableHead>
                <TableHead className="w-[140px]">{t.events.colType}</TableHead>
                <TableHead className="w-[120px]">{t.events.colSource}</TableHead>
                <TableHead>{t.events.colSummary}</TableHead>
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
