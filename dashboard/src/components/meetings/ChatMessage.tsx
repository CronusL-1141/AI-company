import { cn } from '@/lib/utils';
import type { MeetingMessage } from '@/types';

const AGENT_COLORS = [
  'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400',
  'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400',
  'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400',
  'bg-teal-100 text-teal-800 dark:bg-teal-900/30 dark:text-teal-400',
  'bg-pink-100 text-pink-800 dark:bg-pink-900/30 dark:text-pink-400',
  'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-400',
  'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-400',
  'bg-cyan-100 text-cyan-800 dark:bg-cyan-900/30 dark:text-cyan-400',
];

function hashName(name: string): number {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = (hash << 5) - hash + name.charCodeAt(i);
    hash |= 0;
  }
  return Math.abs(hash);
}

export function ChatMessage({ message }: { message: MeetingMessage }) {
  const colorIdx = hashName(message.agent_name) % AGENT_COLORS.length;
  const colorClass = AGENT_COLORS[colorIdx];

  return (
    <div className="group flex gap-3 py-2">
      {/* Agent avatar */}
      <div
        className={cn(
          'flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs font-semibold',
          colorClass,
        )}
      >
        {message.agent_name.charAt(0).toUpperCase()}
      </div>

      {/* Message body */}
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className={cn('rounded px-1.5 py-0.5 text-xs font-medium', colorClass)}>
            {message.agent_name}
          </span>
          <span className="text-xs text-muted-foreground">
            {new Date(message.timestamp).toLocaleTimeString('zh-CN')}
          </span>
        </div>
        <p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed">
          {message.content}
        </p>
      </div>
    </div>
  );
}
