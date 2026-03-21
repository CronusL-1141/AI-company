const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000';

export const WS_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws/events';

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  });
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(error.detail || error.error || 'API request failed');
  }
  return res.json();
}
