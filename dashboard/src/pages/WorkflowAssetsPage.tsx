import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useT } from '@/i18n';
import { formatDateTimeSystemLocale } from '@/lib/datetime';
import {
  useAssetBackupStatus,
  type AssetBackupStatus,
  type AssetHost,
  type AssetRootStatus,
} from '@/api/assets';

// /assets: the workflow backup, read-only. Claude Code and Codex are separate tabs
// with separate folders: their assets are never shown in one table.

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function RootCard({ root }: { root: AssetRootStatus }) {
  const t = useT().assets;
  const counts = Object.entries(root.backed_up).sort(([a], [b]) => a.localeCompare(b));
  const problems = Object.entries(root.not_backed_up);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base break-all">{root.path}</CardTitle>
        <p className="text-sm text-muted-foreground">
          {t.profile}: {root.profile || '—'} · {root.status === 'enrolled' ? t.enrolled : t.ignored}
          {root.this_machine ? '' : ` · ${t.otherMachine}`}
          {' · '}{t.lastBackup}: {formatDateTimeSystemLocale(root.last_backup_at)}
        </p>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {root.stopped && (
          <p role="alert" className="rounded border border-destructive/40 p-3">{t.stopped(root.stopped)}</p>
        )}
        <div>
          <h3 className="font-medium">{t.backedUp}</h3>
          {counts.length === 0 ? <p className="text-muted-foreground">{t.nothingYet}</p> : (
            <ul className="mt-1 grid grid-cols-2 gap-x-6 gap-y-1 md:grid-cols-3">
              {counts.map(([category, n]) => (
                <li key={category} className="flex justify-between gap-2">
                  <span>{t.category(category)}</span><span className="tabular-nums">{n}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        {problems.length > 0 && (
          <div>
            <h3 className="font-medium">{t.notBackedUp}</h3>
            {problems.map(([state, group]) => (
              <div key={state} className="mt-1">
                <p className="text-muted-foreground">{t.state(state)} ({group.total})</p>
                <ul className="ml-4 list-disc break-all">
                  {group.items.map((item) => <li key={`${state}:${item.rel_path}`}>{item.rel_path}</li>)}
                </ul>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function HostPanel({ host }: { host: AssetHost }) {
  const t = useT().assets;
  const status = useAssetBackupStatus(host);
  if (status.isLoading) return <p role="status" className="text-sm">{t.loading}</p>;
  if (status.isError || !status.data) return <p role="alert" className="text-sm">{t.loadError}</p>;
  const data: AssetBackupStatus = status.data;
  return (
    <div className="space-y-4">
      {data.warnings.map((warning) => (
        <p key={warning.code} role="alert" className="rounded border border-destructive/40 p-3 text-sm">
          {warning.code === 'storage_over_limit'
            ? t.storageOverLimit(formatBytes(warning.stored_bytes))
            : t.diskLow(Math.round(warning.free_ratio * 100))}
        </p>
      ))}
      {data.machine_refusal && (
        <p role="alert" className="rounded border border-destructive/40 p-3 text-sm">{data.machine_refusal}</p>
      )}
      {data.roots.length === 0 ? (
        <p className="text-sm text-muted-foreground">{host === 'claude' ? t.emptyClaude : t.emptyCodex}</p>
      ) : data.roots.map((root) => <RootCard key={root.id} root={root} />)}
      {data.pending_findings.total > 0 && (
        <Card>
          <CardHeader><CardTitle className="text-base">{t.findings(data.pending_findings.total)}</CardTitle></CardHeader>
          <CardContent className="space-y-1 text-sm">
            <p className="text-muted-foreground">{t.findingsHint}</p>
            <ul className="ml-4 list-disc break-all">
              {data.pending_findings.items.map((finding) => (
                <li key={finding.finding_id}>
                  {finding.rel_path} · {finding.rule} · {t.line(finding.line)}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
      <p className="text-xs text-muted-foreground">{t.stored(formatBytes(data.stored_bytes))}</p>
    </div>
  );
}

export function WorkflowAssetsPage() {
  const t = useT().assets;
  const [host, setHost] = useState<AssetHost>('claude');
  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold">{t.title}</h1>
        <p className="text-sm text-muted-foreground">{t.subtitle}</p>
      </header>
      <Tabs value={host} onValueChange={(value) => setHost(value as AssetHost)}>
        <TabsList variant="line" className="gap-3">
          <TabsTrigger value="claude">Claude Code</TabsTrigger>
          <TabsTrigger value="codex">Codex</TabsTrigger>
        </TabsList>
        <TabsContent value="claude" className="mt-4"><HostPanel host="claude" /></TabsContent>
        <TabsContent value="codex" className="mt-4"><HostPanel host="codex" /></TabsContent>
      </Tabs>
    </div>
  );
}
