import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { Meeting, MeetingMessage, APIResponse, APIListResponse } from '@/types';

export function useMeetings(teamId?: string) {
  return useQuery({
    queryKey: ['meetings', teamId],
    queryFn: () =>
      apiFetch<APIListResponse<Meeting>>(
        `/api/teams/${teamId}/meetings`,
      ),
    enabled: !!teamId,
  });
}

export function useMeeting(meetingId: string) {
  return useQuery({
    queryKey: ['meetings', meetingId],
    queryFn: () => apiFetch<APIResponse<Meeting>>(`/api/meetings/${meetingId}`),
    enabled: !!meetingId,
  });
}

export function useMeetingMessages(meetingId: string) {
  return useQuery({
    queryKey: ['meetings', meetingId, 'messages'],
    queryFn: () =>
      apiFetch<APIListResponse<MeetingMessage>>(
        `/api/meetings/${meetingId}/messages`,
      ),
    enabled: !!meetingId,
    refetchInterval: 3000,
  });
}

export function useCreateMeeting() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      team_id,
      topic,
      participants,
    }: {
      team_id: string;
      topic: string;
      participants?: string[];
    }) =>
      apiFetch<APIResponse<Meeting>>(`/api/teams/${team_id}/meetings`, {
        method: 'POST',
        body: JSON.stringify({ topic, participants: participants ?? [] }),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['meetings'] });
    },
  });
}

