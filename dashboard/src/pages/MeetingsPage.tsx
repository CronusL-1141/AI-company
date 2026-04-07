import { useState, useMemo } from 'react';
import { useQueries } from '@tanstack/react-query';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { MeetingCard } from '@/components/meetings/MeetingCard';
import { useTeams } from '@/api/teams';
import { useProjects } from '@/api/projects';
import { useCreateMeeting } from '@/api/meetings';
import { apiFetch } from '@/api/client';
import { useT } from '@/i18n';
import { MessageSquare, Plus, Filter, FolderOpen } from 'lucide-react';
import type { Meeting, APIListResponse } from '@/types';

type StatusFilter = 'all' | 'active' | 'concluded';

export function MeetingsPage() {
  const t = useT();
  const { data: teamsData, isLoading: teamsLoading } = useTeams();
  const { data: projectsData } = useProjects();
  const allTeams = teamsData?.data ?? [];
  const projects = projectsData?.data ?? [];

  const [projectFilter, setProjectFilter] = useState('__all__');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [createOpen, setCreateOpen] = useState(false);
  const [newTopic, setNewTopic] = useState('');
  const [newAgenda, setNewAgenda] = useState('');
  const [newTeamId, setNewTeamId] = useState('');

  const teams = useMemo(() => {
    if (projectFilter === '__all__') return allTeams;
    return allTeams.filter((team) => team.project_id === projectFilter);
  }, [allTeams, projectFilter]);

  const createMeeting = useCreateMeeting();

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
    result.sort(
      (a, b) =>
        new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    );
    return result;
  }, [meetingQueries]);

  const filteredMeetings = useMemo(() => {
    if (statusFilter === 'all') return allMeetings;
    return allMeetings.filter((m) => m.status === statusFilter);
  }, [allMeetings, statusFilter]);

  const activeMeetings = useMemo(
    () => filteredMeetings.filter((m) => m.status === 'active'),
    [filteredMeetings],
  );

  const concludedMeetings = useMemo(
    () => filteredMeetings.filter((m) => m.status === 'concluded'),
    [filteredMeetings],
  );

  const activeCount = allMeetings.filter((m) => m.status === 'active').length;
  const concludedCount = allMeetings.filter(
    (m) => m.status === 'concluded',
  ).length;

  function handleCreate() {
    if (!newTopic.trim() || !newTeamId) return;
    const topic = newAgenda.trim()
      ? `${newTopic.trim()}\n\n${t.meetings.agendaPrefix}: ${newAgenda.trim()}`
      : newTopic.trim();
    createMeeting.mutate(
      { team_id: newTeamId, topic },
      {
        onSuccess: () => {
          setCreateOpen(false);
          setNewTopic('');
          setNewAgenda('');
          setNewTeamId('');
        },
      },
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <MessageSquare className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">{t.meetings.title}</h1>
        </div>

        <div className="flex items-center gap-3">
          {/* Project filter */}
          <Select value={projectFilter} onValueChange={(v) => setProjectFilter(v ?? '__all__')}>
            <SelectTrigger className="h-8 w-[180px] text-sm">
              <FolderOpen className="mr-1.5 h-3.5 w-3.5" />
              <SelectValue placeholder={t.common.allProjects} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all__">{t.common.allProjects}</SelectItem>
              {projects.map((p) => (
                <SelectItem key={p.id} value={p.id}>
                  {p.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {/* Status filter */}
          <div className="flex items-center gap-1.5">
            <Filter className="h-3.5 w-3.5 text-muted-foreground" />
            <div className="flex rounded-lg border border-input bg-muted/30 p-0.5">
              <button
                onClick={() => setStatusFilter('all')}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  statusFilter === 'all'
                    ? 'bg-background text-foreground shadow-sm'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {t.meetings.all(allMeetings.length)}
              </button>
              <button
                onClick={() => setStatusFilter('active')}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  statusFilter === 'active'
                    ? 'bg-background text-foreground shadow-sm'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {t.meetings.active(activeCount)}
              </button>
              <button
                onClick={() => setStatusFilter('concluded')}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  statusFilter === 'concluded'
                    ? 'bg-background text-foreground shadow-sm'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {t.meetings.concluded(concludedCount)}
              </button>
            </div>
          </div>

          {/* Create meeting */}
          {teams.length > 0 && (
            <Dialog open={createOpen} onOpenChange={setCreateOpen}>
              <DialogTrigger
                render={
                  <Button size="sm">
                    <Plus className="h-4 w-4" />
                    {t.meetings.createMeeting}
                  </Button>
                }
              />
              <DialogContent className="sm:max-w-md">
                <DialogHeader>
                  <DialogTitle>{t.meetings.createTitle}</DialogTitle>
                  <DialogDescription>
                    {t.meetings.createDesc}
                  </DialogDescription>
                </DialogHeader>
                <div className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="meeting-team">{t.meetings.selectTeam}</Label>
                    <Select
                      value={newTeamId}
                      onValueChange={(v) => setNewTeamId(v ?? '')}
                    >
                      <SelectTrigger className="w-full" id="meeting-team">
                        <SelectValue placeholder={t.meetings.selectTeamPlaceholder} />
                      </SelectTrigger>
                      <SelectContent>
                        {teams.map((tm) => (
                          <SelectItem key={tm.id} value={tm.id}>
                            {tm.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="meeting-topic">{t.meetings.topicLabel}</Label>
                    <Input
                      id="meeting-topic"
                      placeholder={t.meetings.topicPlaceholder}
                      value={newTopic}
                      onChange={(e) => setNewTopic(e.target.value)}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="meeting-agenda">
                      {t.meetings.agendaLabel}{' '}
                      <span className="text-muted-foreground font-normal">
                        {t.meetings.agendaOptional}
                      </span>
                    </Label>
                    <Textarea
                      id="meeting-agenda"
                      placeholder={t.meetings.agendaPlaceholder}
                      value={newAgenda}
                      onChange={(e) => setNewAgenda(e.target.value)}
                      rows={3}
                    />
                  </div>
                </div>
                <DialogFooter>
                  <Button
                    onClick={handleCreate}
                    disabled={
                      !newTopic.trim() ||
                      !newTeamId ||
                      createMeeting.isPending
                    }
                  >
                    {createMeeting.isPending ? t.common.creating : t.meetings.createMeeting}
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          )}
        </div>
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
      ) : filteredMeetings.length === 0 ? (
        <div className="rounded-lg border bg-muted/30 p-12 text-center">
          <MessageSquare className="mx-auto h-10 w-10 text-muted-foreground/50" />
          <p className="mt-3 text-sm text-muted-foreground">
            {statusFilter === 'all'
              ? t.meetings.noMeetings
              : statusFilter === 'active'
                ? t.meetings.noActiveMeetings
                : t.meetings.noConcludedMeetings}
          </p>
        </div>
      ) : (
        <>
          {/* Active meetings */}
          {activeMeetings.length > 0 && (
            <section className="space-y-3">
              <h2 className="text-sm font-medium text-muted-foreground">
                {t.meetings.activeSection(activeMeetings.length)}
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
                {t.meetings.concludedSection(concludedMeetings.length)}
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
