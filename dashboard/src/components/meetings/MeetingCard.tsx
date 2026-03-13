import { useNavigate } from 'react-router-dom';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { LiveIndicator } from '@/components/shared/LiveIndicator';
import { Users, Clock } from 'lucide-react';
import type { Meeting } from '@/types';

function formatDuration(startStr: string, endStr: string | null): string {
  const start = new Date(startStr).getTime();
  const end = endStr ? new Date(endStr).getTime() : Date.now();
  const diffMs = end - start;
  const totalMinutes = Math.floor(diffMs / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;

  if (hours > 0) return `${hours}小时${minutes}分钟`;
  if (minutes > 0) return `${minutes}分钟`;
  return '刚刚开始';
}

export function MeetingCard({ meeting }: { meeting: Meeting }) {
  const navigate = useNavigate();
  const isActive = meeting.status === 'active';

  return (
    <Card
      className="cursor-pointer transition-colors hover:bg-muted/50"
      onClick={() => navigate(`/meetings/${meeting.id}`)}
    >
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-2">
          <h3 className="font-medium leading-tight">{meeting.topic}</h3>
          {isActive ? (
            <div className="flex items-center gap-1.5">
              <LiveIndicator />
              <Badge className="bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400">
                进行中
              </Badge>
            </div>
          ) : (
            <Badge variant="secondary">已结束</Badge>
          )}
        </div>

        <div className="mt-3 flex items-center gap-4 text-xs text-muted-foreground">
          <div className="flex items-center gap-1">
            <Users className="h-3 w-3" />
            <span>{meeting.participants.length} 人参与</span>
          </div>
          <div className="flex items-center gap-1">
            <Clock className="h-3 w-3" />
            <span>{formatDuration(meeting.created_at, meeting.concluded_at)}</span>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
