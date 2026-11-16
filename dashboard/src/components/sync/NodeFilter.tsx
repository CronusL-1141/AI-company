import { useSyncNodes } from '@/api/sync';
import { EVERY_NODE, nodeChoices } from '@/lib/nodes';
import { useNodeFilter } from '@/stores/nodeFilter';
import { useT } from '@/i18n';

/** Header control: which machine's sessions the session views show (multi-host sync).
 * On an edge, a link to the center's dashboard, which holds every machine's sessions. */
export function NodeFilter() {
  const { data: nodes } = useSyncNodes();
  const { node, setNode } = useNodeFilter();
  const t = useT();
  const choices = nodeChoices(nodes);
  return (
    <div className="flex items-center gap-2 text-xs">
      {choices.length > 2 && (
        <label className="flex items-center gap-1 text-muted-foreground">
          {t.sync.nodeFilter}
          <select
            className="rounded border bg-background px-1.5 py-0.5 text-foreground"
            value={node ?? '*'}
            onChange={(event) => setNode(event.target.value === '*' ? EVERY_NODE : event.target.value)}
          >
            <option value="*">{t.sync.everyNode}</option>
            {choices.map((choice) => (
              <option key={choice || 'before'} value={choice}>{choice || t.sync.beforeCutOver}</option>
            ))}
          </select>
        </label>
      )}
      {nodes?.role === 'edge' && nodes.center_url && (
        <a className="text-primary underline-offset-2 hover:underline" href={nodes.center_url}
          title={t.sync.centerViewHint} target="_blank" rel="noreferrer">
          {t.sync.centerView}
        </a>
      )}
    </div>
  );
}
