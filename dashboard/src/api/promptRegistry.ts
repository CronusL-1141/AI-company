import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';

// Content-hash version tracking (usePromptVersions / PromptTemplate) was retired
// 2026-07-27 together with GET /api/prompt-registry/versions: nothing ever called
// the /track endpoint that fed it, so the list was permanently empty.

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

export function usePromptEffectiveness(templateName?: string) {
  const params = templateName ? `?template_name=${encodeURIComponent(templateName)}` : '';
  return useQuery({
    queryKey: ['prompt-registry', 'effectiveness', templateName],
    queryFn: () => apiFetch<PromptEffectivenessResponse>(`/api/prompt-registry/effectiveness${params}`),
  });
}
