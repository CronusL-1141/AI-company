import { useCallback, useRef, useState } from 'react';

/**
 * 轻量级 toast hook：用于把后端响应里已有的诚实提示（如 message/_hint/next_action）
 * 透出到 UI，而不是让用户误以为"点了按钮=AI 已经在做了"。
 *
 * 每个页面各自持有一份状态（沿用 SettingsPage 已有的内联 toast 视觉样式），
 * 不引入全局 Context——这些提示都是页面内一次性动作的反馈，无需跨页面共享。
 */
export function useToast() {
  const [message, setMessage] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = useCallback((msg: string, durationMs = 4000) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    setMessage(msg);
    timerRef.current = setTimeout(() => setMessage(null), durationMs);
  }, []);

  const toastNode = message ? (
    <div className="fixed top-4 right-4 z-50 max-w-sm rounded-lg border bg-background px-4 py-3 text-sm shadow-lg ring-1 ring-foreground/10 animate-in fade-in slide-in-from-top-2">
      {message}
    </div>
  ) : null;

  return { showToast, toastNode };
}
