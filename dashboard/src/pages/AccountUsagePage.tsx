import { useState } from 'react';
import { RefreshCw, ScanLine } from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { PricingPlanCapacityPanel } from '@/components/usage/PricingPlanCapacityPanel';
import { useT } from '@/i18n';
import { formatDateTimeSystemLocale } from '@/lib/datetime';
import {
  useCapturePricingAccount, useLabelPricingAccount, usePricingAccount,
  usePricingAccounts, usePricingMonitor, useUpdatePricingMonitor,
  type PricingAccount,
} from '@/api/accountUsage';
import { monitorIntervalMilliseconds } from '@/lib/account-usage';

const controlClass = 'w-full rounded-lg border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50';
const time = (value: string) => formatDateTimeSystemLocale(value);

function MonitorSettingsPanel({ accountKey, externalBusy, onBusyChange }: {
  accountKey: string; externalBusy: boolean; onBusyChange: (busy: boolean) => void;
}) {
  const t = useT().accountUsage;
  const monitor = usePricingMonitor(accountKey);
  const update = useUpdatePricingMonitor();
  const [intervalDraft, setIntervalDraft] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const state = monitor.data;
  const seconds = intervalDraft ?? (state ? String(state.settings.interval_ms / 1_000) : '');
  const interval = monitorIntervalMilliseconds(seconds);
  const busy = externalBusy || update.isPending;
  const statuses = {
    disabled: t.monitorDisabled, waiting: t.monitorWaiting, sampling: t.monitorSampling,
    error: t.monitorError, paused_account_changed: t.monitorAccountChanged,
  };

  async function save(enabled: boolean) {
    if (!state || (enabled && interval === null)) { setError(t.monitorIntervalError); return; }
    setError(''); setNotice(''); onBusyChange(true);
    try {
      await update.mutateAsync({ key: accountKey, settings: enabled
        ? { enabled: true, interval_ms: interval! }
        : { enabled: false } });
      setIntervalDraft(null);
      setNotice(enabled ? t.monitorSaved : t.monitorPaused);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t.saveError);
    } finally { onBusyChange(false); }
  }

  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="account-monitor-heading">
      <div><h2 id="account-monitor-heading" className="text-lg font-semibold">{t.monitorTitle}</h2>
        <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{t.monitorNotice}</p>
        <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{t.monitorSingleSource}</p>
      </div>
      {monitor.isLoading && <p role="status" className="text-sm">{t.loading}</p>}
      {monitor.isError && <p role="alert" className="rounded border border-destructive/40 p-3 text-sm">{monitor.error.message}</p>}
      {state && <>
        <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm">
          <p>{t.monitorConfiguration}: <span className="font-medium">{state.settings.enabled ? t.monitorEnabled : t.monitorDisabled}</span></p>
          <p>{t.monitorStatus}: <span className="font-medium">{statuses[state.status]}</span></p>
        </div>
        {!state.runtime_running && <p className="rounded border border-amber-500/50 p-3 text-sm text-amber-800 dark:text-amber-300">{t.monitorRuntimeStopped}</p>}
        <dl className="grid gap-3 text-sm md:grid-cols-3">
          <div><dt className="text-muted-foreground">{t.monitorLastStarted}</dt><dd>{state.last_started_at ? time(state.last_started_at) : t.unknown}</dd></div>
          <div><dt className="text-muted-foreground">{t.monitorLastFinished}</dt><dd>{state.last_finished_at ? time(state.last_finished_at) : t.unknown}</dd></div>
          <div><dt className="text-muted-foreground">{t.monitorNextRun}</dt><dd>{state.next_run_at ? time(state.next_run_at) : t.unknown}</dd></div>
        </dl>
        {state.last_error && <p className="rounded bg-muted p-3 text-sm">{state.last_error}</p>}
      </>}
      <form aria-label={t.monitorTitle} className="flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); void save(true); }}>
        <label className="block space-y-1 text-sm"><span>{t.monitorInterval}</span>
          <Input type="number" min={30} max={1800} step={1} value={seconds} className="w-44" disabled={!state || busy}
            onChange={(event) => { setIntervalDraft(event.target.value); setError(''); setNotice(''); }} required />
        </label>
        <Button type="submit" disabled={!state || busy || interval === null}>
          {update.isPending ? t.saving : state?.settings.enabled && state.status !== 'paused_account_changed' ? t.monitorSaveInterval : t.monitorEnable}
        </Button>
        {state?.settings.enabled && <Button type="button" variant="outline" disabled={busy} onClick={() => { void save(false); }}>{t.monitorPause}</Button>}
      </form>
      {error && <p role="alert" className="rounded border border-destructive/40 p-3 text-sm">{error}</p>}
      {notice && <p role="status" className="text-sm">{notice}</p>}
    </section>
  );
}

function AccountLabelForm({ account, externalBusy, onBusyChange }: {
  account: PricingAccount; externalBusy: boolean; onBusyChange: (busy: boolean) => void;
}) {
  const t = useT().accountUsage;
  const [label, setLabel] = useState(account.label);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const saveLabel = useLabelPricingAccount();
  const busy = externalBusy || saveLabel.isPending;
  return (
    <form aria-label={t.label} className="space-y-2" onSubmit={async (event) => {
      event.preventDefault(); setError(''); setNotice(''); onBusyChange(true);
      try {
        await saveLabel.mutateAsync({ key: account.account_key, label: label.trim() });
        setNotice(t.labelSaved);
      } catch (cause) { setError(cause instanceof Error ? cause.message : t.saveError); }
      finally { onBusyChange(false); }
    }}>
      <div className="flex max-w-lg flex-wrap items-end gap-2">
        <label className="min-w-48 flex-1 space-y-1 text-sm">
          <span>{t.label}</span>
          <Input value={label} maxLength={80} onChange={(event) => setLabel(event.target.value)} disabled={busy} required />
        </label>
        <Button variant="outline" type="submit" disabled={busy || !label.trim()}>{t.saveLabel}</Button>
      </div>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {notice && <p role="status" className="text-sm">{notice}</p>}
    </form>
  );
}

export function AccountUsagePage() {
  const t = useT().accountUsage;
  const client = useQueryClient();
  const accounts = usePricingAccounts();
  const [selected, setSelected] = useState('');
  const [panelBusy, setPanelBusy] = useState(false);
  const accountKey = selected || accounts.data?.accounts[0]?.account_key || '';
  const detail = usePricingAccount(accountKey, false);
  const capture = useCapturePricingAccount();
  const busy = panelBusy || capture.isPending;
  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">{t.title}</h1>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" disabled={busy || accounts.isFetching || detail.isFetching}
            onClick={() => { void client.invalidateQueries({ queryKey: ['account-usage'] }); }}>
            <RefreshCw aria-hidden />{t.refresh}
          </Button>
          <Button disabled={busy} onClick={() => {
            capture.mutate(undefined, { onSuccess: (data) => setSelected(data.account.account_key) });
          }}><ScanLine aria-hidden />{capture.isPending ? t.capturing : t.capture}</Button>
        </div>
      </header>
      {capture.isError && <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm">{capture.error.message}</p>}
      {capture.isSuccess && <p role="status" className="text-sm">{t.captured}</p>}
      {(accounts.isError || detail.isError) && <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm">{accounts.error?.message || detail.error?.message}</p>}
      {accounts.isLoading && <p role="status" className="text-sm">{t.loading}</p>}
      {accounts.data?.accounts.length === 0 && <p className="rounded-lg border p-5 text-sm">{t.noAccounts}</p>}
      {Boolean(accounts.data?.accounts.length) && <label className="block max-w-lg space-y-1 text-sm">
        <span>{t.account}</span>
        <select className={controlClass} value={accountKey} disabled={busy} onChange={(event) => setSelected(event.target.value)}>
          {accounts.data?.accounts.map((account) => <option key={account.account_key} value={account.account_key}>
            {account.label || t.unlabeled} (…{account.account_key.slice(-8)})
          </option>)}
        </select>
      </label>}
      {detail.isLoading && Boolean(accountKey) && <p role="status" className="text-sm">{t.loading}</p>}
      {Boolean(accountKey) && <PricingPlanCapacityPanel accountKey={accountKey} />}
      {Boolean(accountKey) && <details className="rounded-lg border">
        <summary className="cursor-pointer px-4 py-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{t.accountSettings}</summary>
        <div className="space-y-5 border-t p-4">
          {detail.data && <AccountLabelForm key={detail.data.account.account_key} account={detail.data.account} externalBusy={busy} onBusyChange={setPanelBusy} />}
          <MonitorSettingsPanel key={'monitor-' + accountKey} accountKey={accountKey} externalBusy={busy} onBusyChange={setPanelBusy} />
        </div>
      </details>}
    </div>
  );
}
