import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';

export interface ReportMeta {
  filename: string;
  author: string;
  topic: string;
  date: string;
  size_bytes: number;
}

export interface ReportDetail extends ReportMeta {
  content: string;
}

export function useReports() {
  return useQuery({
    queryKey: ['reports'],
    queryFn: () => apiFetch<ReportMeta[]>('/api/reports'),
  });
}

export function useReportDetail(filename: string | null) {
  return useQuery({
    queryKey: ['reports', filename],
    queryFn: () => apiFetch<ReportDetail>(`/api/reports/${encodeURIComponent(filename!)}`),
    enabled: !!filename,
  });
}
