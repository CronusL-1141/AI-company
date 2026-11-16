import { useSyncNodes } from '@/api/sync';
import { showsOrigins } from '@/lib/nodes';
import { useT } from '@/i18n';

/** The machine a session row came from; nothing on a node that does not sync. */
export function NodeBadge({ origin, className = '' }: { origin?: string; className?: string }) {
  const { data: nodes } = useSyncNodes();
  const t = useT();
  if (!showsOrigins(nodes)) return null;
  const label = origin || t.sync.beforeCutOver;
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground ${className}`}
      title={origin ? t.sync.originTitle(origin) : t.sync.beforeCutOver}
      data-origin={origin ?? ''}
    >
      {label}
    </span>
  );
}
