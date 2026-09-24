import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';

// User notices (docs/user-notice-design.md §5.10): the lines OS shows the user
// in the terminal, kept in the API ledger so the Dashboard can show them too.

export type NoticeKind = 'status' | 'action' | 'decision' | 'blocked' | 'done';
export type NoticeStatus = 'active' | 'cleared' | 'dismissed' | 'snoozed' | 'expired';

export interface NoticeDelivery {
  host: 'cc' | 'codex';
  session_id: string;
  event: string;
  channel_reliable: boolean;
  language: string;
  claimed_at: string;
  emitted_at?: string | null;
  confirmed_at?: string | null;
  refired_at?: string | null;
  lost_at?: string | null;
}

export interface NoticeItem {
  key: string;
  catalog_id: string;
  design_number: string;
  kind: NoticeKind | '';
  color: string;
  severity: 'info' | 'action' | 'block' | '';
  status: NoticeStatus;
  variant: string;
  user_line: string;
  action: string;
  project_id: string;
  session_id: string;
  host: '' | 'cc' | 'codex';
  source: string;
  first_seen_at: string;
  last_seen_at: string;
  cleared_at: string | null;
  snoozed_until: string | null;
  last_delivery: NoticeDelivery | null;
}

export interface NoticeListResponse {
  items: NoticeItem[];
  total: number;
  limit: number;
  offset: number;
  language: string;
}

export interface NoticeSummary {
  notices: number;
  briefings: number;
  tasks: number;
  total: number;
  top: NoticeItem | null;
  language: string;
}

export const NOTICE_PREFIX = '[AI Team OS] ';

/** The line without the terminal prefix (the Dashboard already says who is talking). */
export function noticeText(line: string): string {
  return line.startsWith(NOTICE_PREFIX) ? line.slice(NOTICE_PREFIX.length) : line;
}

export function noticeSummaryPath(language: string): string {
  return `/api/notices/summary?language=${encodeURIComponent(language)}`;
}

export interface NoticeListQuery {
  status?: 'active' | 'all' | 'cleared' | 'dismissed' | 'snoozed' | 'expired';
  group?: '' | 'immediate' | 'queued';
  kind?: NoticeKind[];
  language: string;
  limit?: number;
}

export function noticeListPath(query: NoticeListQuery): string {
  const params = new URLSearchParams();
  params.set('status', query.status ?? 'active');
  params.set('language', query.language);
  params.set('limit', String(query.limit ?? 50));
  if (query.group) params.set('group', query.group);
  if (query.kind?.length) params.set('kind', query.kind.join(','));
  return `/api/notices?${params.toString()}`;
}

/** Notice keys can hold folder paths: keep the ":" separators, escape the rest. */
export function noticeActionPath(key: string, action: 'dismiss' | 'snooze', hours?: number): string {
  const encoded = key.split(':').map(encodeURIComponent).join(':');
  return action === 'snooze'
    ? `/api/notices/${encoded}/snooze?hours=${hours ?? 24}`
    : `/api/notices/${encoded}/dismiss`;
}

// The banner, the sidebar badge and the overview card share one query.
export function useNoticeSummary(language: string) {
  return useQuery({
    queryKey: ['notices', 'summary', language],
    queryFn: () => apiFetch<NoticeSummary>(noticeSummaryPath(language)),
    refetchInterval: 60_000,
  });
}

export function useNotices(query: NoticeListQuery) {
  return useQuery({
    queryKey: ['notices', 'list', query],
    queryFn: () => apiFetch<NoticeListResponse>(noticeListPath(query)),
  });
}

export function useNoticeAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ key, action, hours }: { key: string; action: 'dismiss' | 'snooze'; hours?: number }) =>
      apiFetch<NoticeItem>(noticeActionPath(key, action, hours), { method: 'POST' }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['notices'] });
    },
  });
}
