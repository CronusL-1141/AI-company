export const accountReadOptions = {
  refetchOnWindowFocus: true, refetchOnReconnect: true, retry: false,
  // Read retries recover a page left open while the local OS service restarts.
  // Keep the cached result and never replay a capture or settings mutation.
  refetchInterval: (query: { state: { status: string } }) => query.state.status === 'error' ? 10_000 : false,
} as const;

export function accountErrorMessage(error: unknown, connectionMessage: string, fallback: string): string {
  if (!(error instanceof Error)) return fallback;
  return error.name === 'ApiConnectionError' ? connectionMessage : error.message;
}
