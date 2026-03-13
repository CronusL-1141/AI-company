import { useAgentActivities, type AgentActivity } from '@/api/activities';
import { Badge } from '@/components/ui/badge';
import { Terminal, FileText, FileEdit, Search, Bot, Code } from 'lucide-react';

const TOOL_CONFIG: Record<string, { icon: React.ElementType; color: string }> = {
  Bash: { icon: Terminal, color: 'bg-gray-100 text-gray-800' },
  Read: { icon: FileText, color: 'bg-blue-100 text-blue-800' },
  Edit: { icon: FileEdit, color: 'bg-yellow-100 text-yellow-800' },
  Write: { icon: FileEdit, color: 'bg-green-100 text-green-800' },
  Grep: { icon: Search, color: 'bg-purple-100 text-purple-800' },
  Glob: { icon: Search, color: 'bg-purple-100 text-purple-800' },
  Agent: { icon: Bot, color: 'bg-orange-100 text-orange-800' },
};

function getToolConfig(name: string) {
  return TOOL_CONFIG[name] ?? { icon: Code, color: 'bg-gray-100 text-gray-700' };
}

function formatTime(ts: string) {
  return new Date(ts).toLocaleTimeString('zh-CN', { hour12: false });
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
      </div>
    </div>
  );
}

export function ActivityLog({ agentId }: { agentId: string }) {
  const { data, isLoading } = useAgentActivities(agentId);
  const activities = data?.data ?? [];

  if (isLoading) {
    return <div className="p-4 text-xs text-muted-foreground">加载中...</div>;
  }

  if (activities.length === 0) {
    return (
      <div className="p-4 text-center text-xs text-muted-foreground">
        暂无活动记录
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
