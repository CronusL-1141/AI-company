const API_BASE = import.meta.env.VITE_API_URL || '';

export const WS_URL = import.meta.env.VITE_WS_URL || `ws://${window.location.host}/ws/events`;

// Project scope state — backs the ecosystem 项目筛选 (EcosystemProjectFilter via ProjectContext).
// The selected X-Project-Id is attached ONLY to /api/ecosystem requests (see apiFetch gate below),
// so the backend scopes ecosystem data WITHOUT scoping other endpoints (teams/tasks/reports/...).
// NOTE: this is NOT a global app switcher. The only UI that sets it is the ecosystem filter;
// do not surface a project switcher in global chrome (Header/Sidebar). See EcosystemProjectFilter.tsx.
let currentProjectPath: string | null = null;
let currentProjectId: string | null = null;

export function setCurrentProjectPath(path: string | null) {
  currentProjectPath = path;
}

export function setCurrentProjectId(id: string | null) {
  currentProjectId = id;
}

export function getCurrentProjectPath(): string | null {
  return currentProjectPath;
}

export function getCurrentProjectId(): string | null {
  return currentProjectId;
}

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const projectHeaders: Record<string, string> = {};
  // Project scope is an ECOSYSTEM-ONLY filter — attach X-Project-Id/Dir ONLY to /api/ecosystem
  // requests. Attaching them globally (the original behavior) leaked the selected/persisted project
  // into every endpoint: e.g. /api/teams got scoped, so ProjectDetailPage's client-side
  // `project_id === projectId` filter matched nothing and ALL teams "disappeared". A stale pin in
  // localStorage was also unreachable once the switcher left the global Header — hence this hard gate.
  // (HTTP headers must be ISO-8859-1; CJK paths would crash fetch, so X-Project-Dir is ASCII-gated.)
  if (path.startsWith('/api/ecosystem')) {
    if (currentProjectPath && [...currentProjectPath].every((ch) => ch.charCodeAt(0) < 128)) {
      projectHeaders['X-Project-Dir'] = currentProjectPath;
    }
    if (currentProjectId) projectHeaders['X-Project-Id'] = currentProjectId;
  }

  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...projectHeaders, ...options?.headers },
    ...options,
  });
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(error.detail || error.error || 'API request failed');
  }
  return res.json();
}
