import { useMemo, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useExternalInvocations, useExternalInvocation, useExternalReconciliation, useCodexLaunchObservations, type ExternalInvocationRow, type ReconciliationBucket } from '@/api/externalExecs';
import { useT } from '@/i18n';
import { epochMsToIso, formatDateTime } from '@/lib/datetime';

function Execution({ item, coverage }: { item: ExternalInvocationRow; coverage: string }) {
  const t = useT();
  const text = (value: string | number | null | undefined) => value == null || value === '' ? t.externalExec.unknown : String(value);
  const [open, setOpen] = useState(false);
  const [receiptOffset, setReceiptOffset] = useState(0);
  const detail = useExternalInvocation(item.invocation_id, open, receiptOffset);
  const receiptPage = detail.data?.data;
  const record = receiptPage ?? item;
  const receipts = Object.values(detail.data?.data.receipts ?? {});
  return <details className="rounded-md border p-3" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer break-all text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
      <Badge variant="outline" className="mr-2">{record.status}</Badge>{record.invocation_id}
      <span className="ml-2 text-xs text-muted-foreground">{formatDateTime(record.started_at)} · {record.ephemeral ? 'ephemeral' : 'persisted'}</span>
    </summary>
    <div className="mt-3 space-y-2 break-words text-xs text-muted-foreground">
      <p>thread: {text(record.thread_id)} · task: {text(record.task_id)} · project: {text(record.project_id)}</p>
      <p>caller: {text(record.caller_harness)} / {text(record.caller_session_id)} · source: {record.source_namespace}</p>
      <p>exit: {text(record.exit_code)} · {record.finished_at ? formatDateTime(record.finished_at) : '—'}</p>
      <p>{t.externalExec.association}: {t.externalExec.associationStates[record.association_status] ?? t.externalExec.unknown}{record.association_reason && <> · {t.externalExec.associationReasons[record.association_reason]}</>}</p>
      {(record.requested_project_id || record.requested_task_id) && <p>{t.externalExec.requestedAssociation}: project {text(record.requested_project_id)} · task {text(record.requested_task_id)}</p>}
      {record.association_status === 'quarantined' && <p className="text-amber-700 dark:text-amber-400">{t.externalExec.quarantineNotice}</p>}
      {record.diagnostics.length > 0 && <p>{t.externalExec.diagnostics}: {record.diagnostics.join(', ')}</p>}
      {detail.isLoading && <p>{t.common.loading}</p>}
      {detail.error && <p role="alert" className="text-destructive">{t.common.loadFailed(detail.error.message)}</p>}
      <p>{coverage} · {t.externalExec.pageCoverageHint}</p>
      {detail.data && (receipts.length === 0 ? <p>{t.externalExec.missing}</p> : <>
        {receipts.map((receipt) => <div key={receipt.receipt_id} className="space-y-1 rounded bg-muted/40 p-2">
          <p>{receipt.counting_level === 'thread_aggregate' ? t.externalExec.threadReceipt : receipt.counting_level === 'turn_aggregate' ? t.externalExec.receipt : t.externalExec.unknownReceipt} · {t.externalExec.metric}</p>
          {receipt.usage_evidence === 'zero_unverified' && <p className="text-amber-700 dark:text-amber-400">{t.externalExec.zeroUnverifiedNotice}</p>}
          <p className="break-all">{receipt.receipt_id} · turn: {text(receipt.turn_id)}</p>
          <p>{t.externalExec.model}: {text(receipt.model)} / {text(receipt.provider)} / {text(receipt.service_tier)}</p>
          <p>{t.externalExec.account}: {text(receipt.account_key)}</p>
          <dl className="grid grid-cols-1 gap-1 sm:grid-cols-2">
            {([
              [t.externalExec.input, receipt.usage?.input_tokens], [t.externalExec.cached, receipt.usage?.cached_input_tokens],
              [t.externalExec.cacheWrite, receipt.usage?.cache_write_input_tokens], [t.externalExec.output, receipt.usage?.output_tokens], [t.externalExec.reasoning, receipt.usage?.reasoning_output_tokens],
              [t.externalExec.tokens, receipt.usage?.total_tokens],
            ] as const).map(([label, value]) => <div key={label} className="flex flex-wrap gap-x-2"><dt>{label}</dt><dd className="font-mono text-foreground">{value == null || !Number.isSafeInteger(value) || value < 0 ? '—' : `${value.toLocaleString()}${receipt.usage_evidence === 'zero_unverified' && value === 0 ? ` (${t.externalExec.zeroUnverified})` : ''}`}</dd></div>)}
          </dl>
          <p>{text(receipt.metadata_source)} · {formatDateTime(receipt.observed_at)}</p>
        </div>)}
      </>)}
      {receiptPage && <div className="flex flex-wrap items-center gap-2">
        <span>{t.externalExec.receiptPage}: {receipts.length ? `${receiptPage.receipts_offset + 1}-${receiptPage.receipts_offset + receipts.length}` : '0'} / {receiptPage.receipts_count ?? t.externalExec.unknown}</span>
        {receiptPage.receipts_truncated && <span>{t.externalExec.truncated}</span>}
        <Button type="button" size="sm" variant="outline" disabled={receiptOffset === 0 || detail.isFetching} onClick={() => setReceiptOffset(Math.max(0, receiptOffset - 100))}>{t.externalExec.previous}</Button>
        <Button type="button" size="sm" variant="outline" disabled={!receiptPage.receipts_truncated || detail.isFetching} onClick={() => setReceiptOffset(receiptOffset + 100)}>{t.externalExec.next}</Button>
      </div>}
    </div>
  </details>;
}

export function CodexLaunchObservations() {
  const t = useT();
  const labels = t.externalExec;
  const [open, setOpen] = useState(false);
  const [offset, setOffset] = useState(0);
  const query = useCodexLaunchObservations(offset, open);
  const data = query.data?.data;
  const text = (value: string | null) => value || labels.unknown;
  return <details className="rounded-md border p-3" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{labels.launchTitle}</summary>
    {open && <div className="mt-3 space-y-3 text-xs">
      <p className="text-muted-foreground">{labels.launchNotice}</p>
      {query.isLoading && <p>{t.common.loading}</p>}
      {query.error && <p role="alert" className="text-destructive">{t.common.loadFailed(query.error.message)}</p>}
      {data && <>
        {data.items.length === 0 ? <p className="text-muted-foreground">{labels.launchEmpty}</p> : <ul className="max-h-96 space-y-2 overflow-y-auto" tabIndex={0} aria-label={labels.launchTitle}>
          {data.items.map((item) => <li key={item.launch_id} className="space-y-1 break-words rounded bg-muted/40 p-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline">{labels.launchStates[item.state] ?? labels.unknown}</Badge>
              {item.execution_failed && item.state !== 'failed' && <Badge variant="outline">{labels.launchExecutionFailed}</Badge>}
              <span className="font-mono">{item.launch_id.slice(0, 12)}</span>
              <span className="text-muted-foreground">{formatDateTime(item.started_at)} · {labels.launchExecModes[item.exec_mode] ?? labels.unknown}</span>
            </div>
            <p>{labels.launchRequestedTier}: <span className="font-mono">{text(item.requested_service_tier)}</span>
              {' · '}{labels.launchRequestedModel}: <span className="font-mono">{text(item.requested_model)}</span>
              {' · '}{labels.launchRequestedEffort}: <span className="font-mono">{text(item.requested_reasoning_effort)}</span></p>
            {item.state !== 'bound' && <p className="text-muted-foreground">{labels.launchBindingReason}: {labels.launchReasons[item.binding_reason] ?? labels.unknown}</p>}
            {item.thread_id && <p className="break-all text-muted-foreground">{labels.launchThread}: {item.thread_id}</p>}
          </li>)}
        </ul>}
        <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
          <span>{labels.page}: {data.items.length ? `${data.offset + 1}-${data.offset + data.items.length}` : '0'} / {data.total}</span>
          <Button type="button" size="sm" variant="outline" disabled={offset === 0 || query.isFetching} onClick={() => setOffset(Math.max(0, offset - 20))}>{labels.previous}</Button>
          <Button type="button" size="sm" variant="outline" disabled={!data.truncated || query.isFetching} onClick={() => setOffset(offset + 20)}>{labels.next}</Button>
        </div>
      </>}
    </div>}
  </details>;
}

/** An independent evidence ledger: no token sums or inferred subscription spend. */
export function ExternalExecPanel({ days }: { days: number }) {
  const t = useT();
  const since = useMemo(() => days > 0 ? epochMsToIso(Date.now() - days * 86400000) : null, [days]);
  const [bucketPage, setBucketPage] = useState({ days, offset: 0 });
  const bucketOffset = bucketPage.days === days ? bucketPage.offset : 0;
  const report = useExternalReconciliation(since, bucketOffset);
  const [offset, setOffset] = useState(0);
  const executions = useExternalInvocations(offset);
  const data = report.data?.data;
  const list = executions.data?.data;
  const coverage = list && list.items.length > 0
    ? t.externalExec.pageCoverage(list.items.filter((item) => item.usage_observed).length, list.items.length)
    : t.externalExec.pageCoverageEmpty;
  const bucketLabels: [ReconciliationBucket, string][] = [
    ['unregistered_threads', t.externalExec.unregistered], ['finished_without_usage', t.externalExec.noUsage],
    ['tokens_without_complete_pricing', t.externalExec.noPricing], ['external_without_journal', t.externalExec.noJournal],
    ['registered_without_evidence', t.externalExec.noEvidence], ['running_invocations', t.externalExec.running],
    ['journal_project_unknown', t.externalExec.journalProjectUnknown], ['quarantined_invocations', t.externalExec.quarantined],
    ['pricing_metadata_unverified', t.externalExec.metadataUnverified],
  ];
  const buckets = data ? bucketLabels.map(([key, label]) => ({ key, label,
    count: data.bucket_counts[key], truncated: data.bucket_truncated[key],
    items: (data.buckets[key] ?? []).map((item) => [item.invocation_id ?? item.thread_id ?? item.agent_id ?? '—',
      item.source_namespace, item.status, item.source, item.association_reason, ...item.reasons,
    ].filter(Boolean).join(' · ')),
  })) : [];
  const hasMoreBuckets = data && Object.values(data.bucket_truncated).some(Boolean);
  const truncated = data ? Object.entries(data.truncated).filter(([, value]) => value).map(([key]) => key) : [];
  return <Card>
    <CardHeader><CardTitle className="text-base">{t.externalExec.title}</CardTitle><p className="text-xs text-muted-foreground">{t.externalExec.hint}</p></CardHeader>
    <CardContent className="space-y-4">
      <CodexLaunchObservations />
      {report.isLoading && <p className="text-sm">{t.common.loading}</p>}
      {report.error && <p role="alert" className="text-sm text-destructive">{t.common.loadFailed(report.error.message)}</p>}
      {data && <>
        <div className="space-y-1 rounded-md bg-muted/40 p-3 text-xs text-muted-foreground">
          <p>{t.externalExec.source}: {data.scope.source_namespace || t.externalExec.scopeAll}</p>
          <p>{t.externalExec.scopeWindow}: {data.scope.since ? formatDateTime(data.scope.since) : t.externalExec.allTime} - {data.scope.until ? formatDateTime(data.scope.until) : formatDateTime(data.observed_at)}</p>
          <p>{t.externalExec.unknownProject} · {data.scope.project_id || t.externalExec.scopeAll}</p>
          <p className={truncated.length ? 'text-amber-700 dark:text-amber-400' : ''}>{truncated.length ? `${t.externalExec.truncated} ${truncated.join(', ')}` : t.externalExec.complete}</p>
          <p>{t.externalExec.saved}</p>
          <p>{t.externalExec.scanLimit}: {data.sample_limit} · {t.externalExec.bucketRange}: {data.bucket_offset + 1}-{data.bucket_offset + data.bucket_limit}</p>
          {data.journal_status && <p>{t.externalExec.cursors}: {data.journal_status.cursor_count} · {t.externalExec.incomplete}: {data.journal_status.incomplete_cursors} · {t.externalExec.unknownTimes}: {data.journal_status.unknown_observation_times_in_sample ? t.externalExec.yes : t.externalExec.no}</p>}
        </div>
        <section aria-label={t.externalExec.denominator} className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {([
            [t.externalExec.journal, `${data.counts.journal_observations} / ${data.counts.journal_threads}`],
            [t.externalExec.agents, data.counts.registered_codex_agents], [t.externalExec.invocations, data.counts.external_invocations],
            [t.externalExec.threads, data.counts.discovered_threads], [t.externalExec.overlap, data.counts.overlap_threads],
          ] as const).map(([label, value]) => <div key={label} className="rounded-md border p-2"><p className="text-xs text-muted-foreground">{label}</p><p className="font-mono text-sm">{value}</p></div>)}
        </section>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {buckets.map(({ key, label, items, count, truncated: bucketTruncated }) => <details key={key} className="rounded-md border p-3">
            <summary className="cursor-pointer text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{label} <Badge variant="secondary">{count ?? t.externalExec.unknown}</Badge></summary>
            <p className="mt-2 text-xs text-muted-foreground">{t.externalExec.bucketShown}: {items.length} / {count ?? t.externalExec.unknown}{bucketTruncated && <> · {t.externalExec.truncated}</>}</p>
            {items.length === 0 ? <p className="mt-2 text-xs text-muted-foreground">{t.externalExec.bucketPageEmpty}</p> : <ul className="mt-2 max-h-48 space-y-1 overflow-y-auto text-xs text-muted-foreground">{items.map((item, index) => <li key={`${item}-${index}`} className="break-all">{item}</li>)}</ul>}
          </details>)}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" variant="outline" disabled={bucketOffset === 0 || report.isFetching} onClick={() => setBucketPage({ days, offset: Math.max(0, bucketOffset - 20) })}>{t.externalExec.previousBucket}</Button>
          <Button type="button" size="sm" variant="outline" disabled={!hasMoreBuckets || report.isFetching} onClick={() => setBucketPage({ days, offset: bucketOffset + 20 })}>{t.externalExec.nextBucket}</Button>
        </div>
        <ul className="space-y-1 text-xs text-muted-foreground">{data.statements.map((statement) => <li key={statement} className="break-words">{statement}</li>)}</ul>
      </>}
      <section className="space-y-2" aria-label={t.externalExec.list}>
        <h3 className="text-sm font-medium">{t.externalExec.list}</h3>
        {executions.isLoading && <p className="text-sm">{t.common.loading}</p>}
        {executions.error && <p role="alert" className="text-sm text-destructive">{t.common.loadFailed(executions.error.message)}</p>}
        {list && <>
          <p className="text-xs text-muted-foreground">{coverage} · {t.externalExec.pageCoverageHint}</p>
          {list.items.length === 0 && <p className="text-xs text-muted-foreground">{t.externalExec.noData}</p>}
          {list.items.map((item) => <Execution key={item.invocation_id} item={item} coverage={coverage} />)}
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>{t.externalExec.page}: {list.items.length ? `${list.offset + 1}-${list.offset + list.items.length}` : '0'} / {list.total}</span>
            {list.truncated && <span>{t.externalExec.truncated}</span>}
            <Button type="button" size="sm" variant="outline" disabled={offset === 0 || executions.isFetching} onClick={() => setOffset(Math.max(0, offset - 20))}>{t.externalExec.previous}</Button>
            <Button type="button" size="sm" variant="outline" disabled={!list.truncated || executions.isFetching} onClick={() => setOffset(offset + 20)}>{t.externalExec.next}</Button>
          </div>
        </>}
      </section>
      <p className="text-xs text-muted-foreground">{t.externalExec.scopeNotice}</p>
    </CardContent>
  </Card>;
}
