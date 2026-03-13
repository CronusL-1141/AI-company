import { useMemo } from 'react';
import { useQueries } from '@tanstack/react-query';
import { Skeleton } from '@/components/ui/skeleton';
import { MeetingCard } from '@/components/meetings/MeetingCard';
import { useTeams } from '@/api/teams';
import { apiFetch } from '@/api/client';
import { MessageSquare } from 'lucide-react';
import type { Meeting, APIListResponse } from '@/types';

export function MeetingsPage() {
  const { data: teamsData, isLoading: teamsLoading } = useTeams();
  const teams = teamsData?.data ?? [];

  const meetingQueries = useQueries({
    queries: teams.map((team) => ({
      queryKey: ['meetings', team.id],
      queryFn: () =>
        apiFetch<APIListResponse<Meeting>>(`/api/teams/${team.id}/meetings`),
      enabled: !!team.id,
    })),
  });

  const isLoading = teamsLoading || meetingQueries.some((q) => q.isLoading);

  const allMeetings = useMemo(() => {
    const result: Meeting[] = [];
    for (const q of meetingQueries) {
      if (q.data?.data) {
        result.push(...q.data.data);
      }
    }
    // Sort by created_at descending
    result.sort(
      (a, b) =>
        new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    );
    return result;
  }, [meetingQueries]);

  const activeMeetings = useMemo(
    () => allMeetings.filter((m) => m.status === 'active'),
    [allMeetings],
  );

  const concludedMeetings = useMemo(
    () => allMeetings.filter((m) => m.status === 'concluded'),
    [allMeetings],
  );

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-2">
        <MessageSquare className="h-5 w-5 text-muted-foreground" />
        <h1 className="text-lg font-semibold">会议室</h1>
      </div>

      {isLoading ? (
        <div className="space-y-4">
          <Skeleton className="h-6 w-32" />
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-24" />
            ))}
          </div>
        </div>
      ) : allMeetings.length === 0 ? (
        <div className="rounded-lg border bg-muted/30 p-12 text-center">
          <MessageSquare className="mx-auto h-10 w-10 text-muted-foreground/50" />
          <p className="mt-3 text-sm text-muted-foreground">暂无会议</p>
        </div>
      ) : (
        <>
          {/* Active meetings */}
          {activeMeetings.length > 0 && (
            <section className="space-y-3">
              <h2 className="text-sm font-medium text-muted-foreground">
                进行中的会议 ({activeMeetings.length})
              </h2>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {activeMeetings.map((meeting) => (
                  <MeetingCard key={meeting.id} meeting={meeting} />
                ))}
              </div>
            </section>
          )}

          {/* Concluded meetings */}
          {concludedMeetings.length > 0 && (
            <section className="space-y-3">
              <h2 className="text-sm font-medium text-muted-foreground">
                已结束的会议 ({concludedMeetings.length})
              </h2>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {concludedMeetings.map((meeting) => (
                  <MeetingCard key={meeting.id} meeting={meeting} />
                ))}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}
