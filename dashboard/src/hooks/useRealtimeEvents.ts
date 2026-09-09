import { useCallback, useEffect, useRef } from 'react';
import useWebSocket from 'react-use-websocket';
import { useQueryClient } from '@tanstack/react-query';
import { WS_URL } from '../api/client';
import { useWSStore } from '../stores/websocket';
import type { WSEvent } from '../types';

const REFRESH_TIMEOUT_MS = 30_000;

export function useRealtimeEvents() {
  const queryClient = useQueryClient();
  const { setConnected, addEvent } = useWSStore();
  const enqueue = useRef<((prefix: string) => void) | null>(null);

  useEffect(() => {
    const pending = new Set<string>();
    const inFlight = new Set<string>();
    const deadlines = new Map<string, ReturnType<typeof setTimeout>>();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let active = true;

    function schedule() {
      if (!active || timer !== null) return;
      if (![...pending].some((key) => !inFlight.has(key))) return;
      // Do not extend this window when more events arrive.
      timer = setTimeout(flush, 200);
    }

    function flush() {
      timer = null;
      for (const key of [...pending]) {
        if (inFlight.has(key)) continue;
        const queryKey = [key];
        // A request started before this event may return an older snapshot.
        // Keep it dirty and refetch once it settles, without cancelling it.
        if (!queryClient.isFetching({ queryKey, type: 'active' })) pending.delete(key);
        inFlight.add(key);
        let finished = false;
        const settled = () => {
          if (finished) return;
          finished = true;
          clearTimeout(deadlines.get(key));
          deadlines.delete(key);
          inFlight.delete(key);
          schedule();
        };
        const refresh = queryClient.invalidateQueries({ queryKey }, { cancelRefetch: false });
        const captured = queryClient.getQueryCache().findAll({ queryKey, type: 'active' })
          .filter((query) => query.state.fetchStatus === 'fetching')
          .map((query) => ({ query, promise: query.promise }));
        deadlines.set(key, setTimeout(() => {
          if (active && !finished) {
            for (const { query, promise } of captured) {
              // A newer request on the same key must not be cancelled by this deadline.
              if (promise && query.promise === promise && query.state.fetchStatus === 'fetching') {
                void query.cancel({ silent: true, revert: true });
              }
            }
          }
          settled();
        }, REFRESH_TIMEOUT_MS));
        void refresh.then(settled, settled);
      }
    }

    enqueue.current = (prefix) => {
      pending.add(prefix);
      schedule();
    };
    return () => {
      active = false;
      enqueue.current = null;
      if (timer !== null) clearTimeout(timer);
      for (const deadline of deadlines.values()) clearTimeout(deadline);
      deadlines.clear();
      pending.clear();
    };
  }, [queryClient]);

  const invalidate = useCallback((prefix: string) => {
    enqueue.current?.(prefix);
  }, []);

  const onMessage = useCallback(
    (event: MessageEvent) => {
      try {
        const raw = JSON.parse(event.data as string);

        // 跳过非事件消息（如ack）
        if (raw.type !== 'event') return;

        // 用 event_type 作为实际类型
        const data: WSEvent = {
          type: raw.event_type ?? raw.type,
          source: raw.channel ?? '',
          data: raw.data ?? {},
          timestamp: raw.timestamp ?? '',
        };
        addEvent(data);

        // Invalidate relevant queries based on event type
        // Note: invalidateQueries matches by prefix, so ['teams'] matches
        // ['teams'], ['teams', id], ['teams', id, 'agents'], etc.
        const t = data.type;
        if (t.startsWith('team')) {
          invalidate('teams');
        }
        if (t.startsWith('task')) {
          // useTasks uses ['teams', teamId, 'tasks'] — covered by ['teams'] invalidation
          // but also need standalone task queries and task-wall
          invalidate('teams');
          invalidate('tasks');
          invalidate('task-wall');
          invalidate('project-task-wall');
        }
        if (t.startsWith('agent')) {
          // useAgents uses ['teams', teamId, 'agents'] — covered by ['teams']
          invalidate('teams');
          invalidate('activities');
        }
        if (t.startsWith('meeting')) {
          invalidate('meetings');
        }
        if (t.startsWith('workflow')) {
          // workflow.planned/started/completed → 失效运行列表与详情（前缀匹配）
          invalidate('workflows');
        }
        if (t.startsWith('project')) {
          invalidate('projects');
          invalidate('project-task-wall');
        }
        if (t.startsWith('cc.')) {
          // CC hook事件：刷新agents和teams（可能有auto-created agent）
          invalidate('teams');
          invalidate('activities');
        }
        // 所有事件都应刷新事件列表
        invalidate('events');
      } catch {
        // ignore malformed messages
      }
    },
    [invalidate, addEvent],
  );

  const { readyState, sendJsonMessage } = useWebSocket(WS_URL, {
    onOpen: () => {
      setConnected(true);
      sendJsonMessage({ type: 'subscribe', channel: '*' });
    },
    onClose: () => setConnected(false),
    onError: () => setConnected(false),
    onMessage,
    shouldReconnect: () => true,
    reconnectAttempts: 10,
    reconnectInterval: 3000,
  });

  return { readyState };
}
