import { useCallback } from 'react';
import useWebSocket from 'react-use-websocket';
import { useQueryClient } from '@tanstack/react-query';
import { WS_URL } from '../api/client';
import { useWSStore } from '../stores/websocket';
import type { WSEvent } from '../types';

export function useRealtimeEvents() {
  const queryClient = useQueryClient();
  const { setConnected, addEvent } = useWSStore();

  const onMessage = useCallback(
    (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data as string) as WSEvent;
        addEvent(data);

        // Invalidate relevant queries based on event type
        if (data.type.startsWith('team')) {
          void queryClient.invalidateQueries({ queryKey: ['teams'] });
        }
        if (data.type.startsWith('task')) {
          void queryClient.invalidateQueries({ queryKey: ['tasks'] });
        }
        if (data.type.startsWith('agent')) {
          void queryClient.invalidateQueries({ queryKey: ['agents'] });
        }
      } catch {
        // ignore malformed messages
      }
    },
    [queryClient, addEvent],
  );

  const { readyState } = useWebSocket(WS_URL, {
    onOpen: () => setConnected(true),
    onClose: () => setConnected(false),
    onError: () => setConnected(false),
    onMessage,
    shouldReconnect: () => true,
    reconnectAttempts: 10,
    reconnectInterval: 3000,
  });

  return { readyState };
}
