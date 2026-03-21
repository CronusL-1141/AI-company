import { useNavigate } from 'react-router-dom';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { LiveIndicator } from '@/components/shared/LiveIndicator';
import { Users, Clock } from 'lucide-react';
import type { Meeting } from '@/types';
import { useT } from '@/i18n';

export function MeetingCard({ meeting }: { meeting: Meeting }) {
  const navigate = useNavigate();
  const t = useT();
  const isActive = meeting.status === 'active';

  function formatDuration(startStr: string, endStr: string | null): string {
    const start = new Date(startStr).getTime();
    const end = endStr ? new Date(endStr).getTime() : Date.now();
    const diffMs = end - start;
    const totalMinutes = Math.floor(diffMs / 60000);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;

    if (hours > 0) return t.meetings.durationHoursMinutes(hours, minutes);
    if (minutes > 0) return t.meetings.durationMinutes(minutes);
    return t.meetings.durationJustStarted;
  }

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
                {t.meetings.statusActive}
              </Badge>
            </div>
          ) : (
            <Badge variant="secondary">{t.meetings.statusConcluded}</Badge>
          )}
        </div>

        <div className="mt-3 flex items-center gap-4 text-xs text-muted-foreground">
          <div className="flex items-center gap-1">
            <Users className="h-3 w-3" />
            <span>{t.meetings.participantsCount(meeting.participants.length)}</span>
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
