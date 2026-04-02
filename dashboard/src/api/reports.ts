import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';
import { useProject } from '@/context/ProjectContext';

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
  const { projectPath } = useProject();
  return useQuery({
    queryKey: ['reports', projectPath],
    queryFn: () => apiFetch<ReportMeta[]>('/api/reports'),
  });
}

export function useReportDetail(filename: string | null) {
  const { projectPath } = useProject();
  return useQuery({
    queryKey: ['reports', projectPath, filename],
    queryFn: () => apiFetch<ReportDetail>(`/api/reports/${encodeURIComponent(filename!)}`),
    enabled: !!filename,
  });
}
