import { useAgentActivities } from '@/api/activities';
import type { AgentActivity } from '@/types';
import { Badge } from '@/components/ui/badge';
import { useT } from '@/i18n';
import { Terminal, FileText, FileEdit, Search, Bot, Code, Loader2 } from 'lucide-react';
import { formatTime } from '@/lib/datetime';

const TOOL_CONFIG: Record<string, { icon: React.ElementType; color: string }> = {
  Bash: { icon: Terminal, color: 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200' },
  Read: { icon: FileText, color: 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200' },
  Edit: { icon: FileEdit, color: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200' },
  Write: { icon: FileEdit, color: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200' },
  Grep: { icon: Search, color: 'bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200' },
  Glob: { icon: Search, color: 'bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200' },
  Agent: { icon: Bot, color: 'bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200' },
};

function getToolConfig(name: string) {
  return TOOL_CONFIG[name] ?? { icon: Code, color: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300' };
}

export function formatDuration(ms: number | null): string {
  if (ms === null) return '-';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function StatusIcon({ status }: { status: AgentActivity['status'] }) {
  const t = useT();
  if (status === 'running') {
    return <Loader2 className="h-3 w-3 animate-spin text-blue-500" aria-label={t.activityLog.ariaRunning} />;
  }
  if (status === 'error') {
    return <span aria-label={t.activityLog.ariaError} role="img">❌</span>;
  }
  return <span aria-label={t.activityLog.ariaCompleted} role="img">✅</span>;
}

function ActivityItem({ activity }: { activity: AgentActivity }) {
  const config = getToolConfig(activity.tool_name);
  const Icon = config.icon;

  return (
    <div className="flex items-start gap-2 py-1.5 px-2 hover:bg-muted/50 rounded text-xs">
      <span className="text-muted-foreground shrink-0 w-16 pt-0.5">
        {formatTime(activity.timestamp)}
      </span>
      <Badge variant="outline" className={`shrink-0 gap-1 ${config.color}`}>
        <Icon className="h-3 w-3" />
        {activity.tool_name}
      </Badge>
      <div className="flex-1 min-w-0">
        {activity.input_summary && (
          <p className="text-foreground truncate">{activity.input_summary}</p>
        )}
        {activity.output_summary && (
          <p className="text-muted-foreground truncate mt-0.5">&rarr; {activity.output_summary}</p>
        )}
        {activity.error && (
          <p className="text-destructive truncate mt-0.5">{activity.error}</p>
        )}
      </div>
    </div>
  );
}

export function ActivityLog({ agentId, limit = 50 }: { agentId: string; limit?: number }) {
  const t = useT();
  const { data, isLoading } = useAgentActivities(agentId, limit);
  const activities = data?.data ?? [];

  if (isLoading) {
    return <div className="p-4 text-xs text-muted-foreground">{t.activityLog.loading}</div>;
  }

  if (activities.length === 0) {
    return (
      <div className="p-4 text-center text-xs text-muted-foreground">
        {t.activityLog.noActivities}
      </div>
    );
  }

  return (
    <div className="max-h-[300px] overflow-y-auto">
      <div className="space-y-0.5">
        {activities.map((a) => (
          <ActivityItem key={a.id} activity={a} />
        ))}
      </div>
    </div>
  );
}

export { ActivityItem };
