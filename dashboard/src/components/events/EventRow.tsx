import { useState } from 'react';
import { TableRow, TableCell } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { RelativeTime } from '@/components/shared/RelativeTime';
import { ChevronRight, ChevronDown } from 'lucide-react';
import type { Event } from '@/types';

const TYPE_COLORS: Record<string, string> = {
  'team': 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400',
  'agent': 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400',
  'task': 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
  'system': 'bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-400',
};

function getTypeColor(type: string): string {
  const prefix = type.split('.')[0];
  return TYPE_COLORS[prefix] ?? '';
}

function getSummary(data: Record<string, unknown>): string {
  if (typeof data.message === 'string') return data.message;
  if (typeof data.status === 'string') return data.status;
  const keys = Object.keys(data);
  if (keys.length === 0) return '-';
  return keys.slice(0, 3).join(', ');
}

export function EventRow({ event, isNew }: { event: Event; isNew?: boolean }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <>
      <TableRow
        className={`cursor-pointer ${isNew ? 'animate-pulse bg-accent/50' : ''}`}
        onClick={() => setExpanded(!expanded)}
      >
        <TableCell className="w-8">
          {expanded ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )}
        </TableCell>
        <TableCell className="text-xs text-muted-foreground">
          <RelativeTime date={event.timestamp} />
        </TableCell>
        <TableCell>
          <Badge className={getTypeColor(event.type)}>{event.type}</Badge>
        </TableCell>
        <TableCell className="text-xs">{event.source}</TableCell>
        <TableCell className="text-xs text-muted-foreground max-w-[300px] truncate">
          {getSummary(event.data)}
        </TableCell>
      </TableRow>
      {expanded && (
        <TableRow>
          <TableCell colSpan={5} className="bg-muted/30">
            <pre className="text-xs p-2 overflow-auto max-h-48 whitespace-pre-wrap">
              {JSON.stringify(event.data, null, 2)}
            </pre>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}
