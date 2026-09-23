import { useState, useEffect, createContext, useContext } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '@/api/client';
import { zh } from './zh';
import { en } from './en';

const langs = { zh, en } as const;
export type Lang = keyof typeof langs;
export type LanguageMode = 'follow' | Lang;
export type { Translations } from './zh';

function detectLang(): Lang {
  // A cached effective language avoids a flash while the server preference loads.
  // It is never sent back as an override, including the old manual browser value.
  try {
    const stored = localStorage.getItem('lang');
    if (stored === 'zh' || stored === 'en') return stored;
  } catch { /* Storage may be disabled; language settings still work via the API. */ }
  const browserLang = navigator.language?.toLowerCase() || '';
  if (browserLang.startsWith('zh')) return 'zh';
  return 'en';
}

type LanguageSetting = {
  mode: LanguageMode;
  effective: Lang;
  source: 'dashboard' | 'cc_settings' | 'codex' | 'system' | 'default';
};

const languageKey = ['settings', 'language'] as const;
// This Dashboard has no session-host context. Hooks pass cc/codex explicitly.
const languagePath = '/api/settings/language?host=system';

async function fetchLanguage(mode?: LanguageMode, signal?: AbortSignal): Promise<LanguageSetting> {
  const result = await apiFetch<LanguageSetting>(languagePath, mode === undefined ? { signal } : {
    method: 'PUT', body: JSON.stringify({ mode }),
  });
  if (!result || !['follow', 'zh', 'en'].includes(result.mode)
      || !['zh', 'en'].includes(result.effective)) {
    throw new Error('Invalid language setting response');
  }
  return result;
}

function cacheLanguage(lang: Lang) {
  try { localStorage.setItem('lang', lang); } catch { /* Cache is optional. */ }
}

export function useLanguage() {
  const [cachedLang] = useState<Lang>(detectLang);
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: languageKey,
    queryFn: ({ signal }) => fetchLanguage(undefined, signal),
    staleTime: 0,
  });
  useEffect(() => {
    if (query.data) cacheLanguage(query.data.effective);
  }, [query.data]);
  const mutation = useMutation({
    mutationFn: (mode: LanguageMode) => fetchLanguage(mode),
    // Cancel an older startup/focus read so it cannot undo a successful save.
    onMutate: () => queryClient.cancelQueries({ queryKey: languageKey }),
    onSuccess: async (result) => {
      await queryClient.cancelQueries({ queryKey: languageKey });
      cacheLanguage(result.effective);
      queryClient.setQueryData(languageKey, result);
    },
  });
  const lang = query.data?.effective ?? cachedLang;
  return {
    t: langs[lang], lang, mode: query.data?.mode ?? 'follow', switchLang: mutation.mutate,
    isLoading: query.isLoading, isSaving: mutation.isPending,
    error: mutation.isError ? 'save' : query.isError ? 'load' : null,
  };
}

// Context-based approach for sharing language state across the app
export const LanguageContext = createContext<ReturnType<typeof useLanguage> | null>(null);

export function useT() {
  const ctx = useContext(LanguageContext);
  if (!ctx) {
    // Fallback: return zh translations directly when used outside provider
    return langs['zh'];
  }
  return ctx.t;
}
