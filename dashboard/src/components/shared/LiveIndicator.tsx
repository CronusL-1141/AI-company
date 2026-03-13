import { cn } from '@/lib/utils';

export function LiveIndicator({ className }: { className?: string }) {
  return (
    <span className={cn('relative flex h-2.5 w-2.5', className)}>
      <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-green-400 opacity-75" />
      <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-green-500" />
    </span>
  );
}
