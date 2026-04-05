import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';

export interface PromptVersion {
  content_hash: string;
  first_used_at: string;
  usage_count: number;
}

export interface PromptTemplate {
  template_name: string;
  versions: PromptVersion[];
  total_usage: number;
}

export interface PromptVersionsResponse {
  success: boolean;
  templates: PromptTemplate[];
  total: number;
}

export interface PromptEffectiveness {
  template_name: string;
  total_activities: number;
  success_count: number;
  failure_count: number;
  success_rate_pct: number | null;
  avg_duration_ms: number | null;
  top_failure_reasons: string[];
  failure_lesson_count: number;
}

export interface PromptEffectivenessResponse {
  success: boolean;
  effectiveness: PromptEffectiveness[];
  total: number;
}

export function usePromptVersions(templateName?: string) {
  const params = templateName ? `?template_name=${encodeURIComponent(templateName)}` : '';
  return useQuery({
    queryKey: ['prompt-registry', 'versions', templateName],
    queryFn: () => apiFetch<PromptVersionsResponse>(`/api/prompt-registry/versions${params}`),
  });
}

export function usePromptEffectiveness(templateName?: string) {
  const params = templateName ? `?template_name=${encodeURIComponent(templateName)}` : '';
  return useQuery({
    queryKey: ['prompt-registry', 'effectiveness', templateName],
    queryFn: () => apiFetch<PromptEffectivenessResponse>(`/api/prompt-registry/effectiveness${params}`),
  });
}
