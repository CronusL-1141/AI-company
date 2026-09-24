import { useState } from 'react';
import { Bell, CheckCircle, ShieldAlert, XCircle } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { useLang, useT } from '@/i18n';
import { noticeText, useNoticeAction, useNotices } from '@/api/notices';
import type { NoticeItem } from '@/api/notices';
import { formatDateTime } from '@/lib/datetime';
import {
  useBriefings,
  useResolveBriefing,
  useDismissBriefing,
} from '@/api/briefings';
import type { Briefing, BriefingStatus } from '@/api/briefings';
import { useProjects } from '@/api/projects';
import type { Project } from '@/types';

type TabStatus = 'pending' | 'resolved' | 'dismissed' | 'expired';

function urgencyVariant(urgency: string): 'destructive' | 'outline' | 'secondary' {
  if (urgency === 'high') return 'destructive';
  if (urgency === 'medium') return 'outline';
  return 'secondary';
}

function UrgencyBadge({ urgency }: { urgency: string }) {
  const t = useT();
  return (
    <Badge variant={urgencyVariant(urgency)} className="text-[11px]">
      {t.briefings.urgency[urgency as 'high' | 'medium' | 'low'] ?? urgency}
    </Badge>
  );
}

function ProjectBadge({ projectName }: { projectName: string }) {
  return (
    <Badge variant="secondary" className="text-[10px] font-normal max-w-[120px] truncate shrink-0">
      {projectName}
    </Badge>
  );
}

function TagBadges({ tags }: { tags: string[] }) {
  if (tags.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {tags.map((tag) => (
        <Badge key={tag} variant="outline" className="text-[10px] font-normal">
          {tag}
        </Badge>
      ))}
    </div>
  );
}

interface BriefingCardProps {
  briefing: Briefing;
  projectName?: string;
  onResolve: (b: Briefing) => void;
  onDismiss: (id: string) => void;
  dismissing: boolean;
}

function BriefingCard({ briefing, projectName, onResolve, onDismiss, dismissing }: BriefingCardProps) {
  const t = useT();
  const isPending = briefing.status === 'pending';

  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm space-y-3">
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-semibold leading-snug">{briefing.title}</h3>
        <div className="flex items-center gap-1.5 shrink-0">
          {projectName && <ProjectBadge projectName={projectName} />}
          <UrgencyBadge urgency={briefing.urgency} />
        </div>
      </div>

      <TagBadges tags={briefing.tags ?? []} />

      <p className="text-xs text-muted-foreground leading-relaxed">{briefing.description}</p>

      {briefing.options && (
        <div className="space-y-1">
          <p className="text-xs font-medium">{t.briefings.options}</p>
          <p className="text-xs text-muted-foreground">{briefing.options}</p>
        </div>
      )}

      {briefing.recommendation && (
        <div className="rounded-md bg-muted/50 px-3 py-2">
          <p className="text-xs text-muted-foreground">
            <span className="font-medium text-foreground">{t.briefings.recommendation}:</span>{' '}
            {briefing.recommendation}
          </p>
        </div>
      )}

      {briefing.resolution && (
        <div className="rounded-md bg-green-50 dark:bg-green-950/30 px-3 py-2">
          <p className="text-xs text-muted-foreground">
            <span className="font-medium text-foreground">{t.briefings.resolution}:</span>{' '}
            {briefing.resolution}
          </p>
        </div>
      )}

      {isPending && (
        <div className="flex gap-2 pt-1">
          <Button
            size="sm"
            variant="default"
            className="h-7 text-xs"
            onClick={() => onResolve(briefing)}
          >
            <CheckCircle className="h-3.5 w-3.5 mr-1" />
            {t.briefings.resolve}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            onClick={() => onDismiss(briefing.id)}
            disabled={dismissing}
          >
            <XCircle className="h-3.5 w-3.5 mr-1" />
            {t.briefings.dismiss}
          </Button>
        </div>
      )}
    </div>
  );
}

function TabBar<T extends string>({
  tabs,
  active,
  onChange,
}: {
  tabs: { key: T; label: string }[];
  active: T;
  onChange: (key: T) => void;
}) {
  return (
    <div className="flex rounded-lg border border-input bg-muted/30 p-0.5 w-fit">
      {tabs.map(({ key, label }) => (
        <button
          key={key}
          type="button"
          onClick={() => onChange(key)}
          className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
            active === key
              ? 'bg-background text-foreground shadow-sm'
              : 'text-muted-foreground hover:text-foreground'
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function DecisionsTab() {
  const t = useT();
  const [showAuto, setShowAuto] = useState(false);
  const [projectTab, setProjectTab] = useState<string>('all');
  const [statusTab, setStatusTab] = useState<TabStatus>('pending');
  const [tagTab, setTagTab] = useState<string>('all');
  const [resolveTarget, setResolveTarget] = useState<Briefing | null>(null);
  const [resolutionText, setResolutionText] = useState('');

  const { data: projectsData, error: projectsError } = useProjects();
  const projects: Project[] = projectsData?.data ?? [];

  const { data, isLoading, error: briefingsError } = useBriefings(
    statusTab as BriefingStatus,
    projectTab === 'all' ? undefined : projectTab,
    undefined,
    !showAuto,
  );
  const resolveMutation = useResolveBriefing();
  const dismissMutation = useDismissBriefing();

  const allItems = data?.items ?? [];
  const error = briefingsError ?? projectsError;

  // Tag options come from the unfiltered result, so selecting one never empties
  // the picker it was chosen from. The tag dimension is then applied here —
  // same exact-match semantics as the API's ?tag= filter.
  const tagOptions = Array.from(
    new Set(allItems.flatMap((b) => b.tags ?? [])),
  ).sort();
  // Switching project/status can retire the selected tag — fall back to "all"
  // instead of showing an empty list under a tab that is no longer offered.
  const activeTag = tagOptions.includes(tagTab) ? tagTab : 'all';
  const briefings =
    activeTag === 'all' ? allItems : allItems.filter((b) => (b.tags ?? []).includes(activeTag));

  // Build project name lookup map
  const projectNameMap = new Map<string, string>(projects.map((p) => [p.id, p.name]));

  function handleOpenResolve(b: Briefing) {
    setResolveTarget(b);
    setResolutionText('');
  }

  function handleConfirmResolve() {
    if (!resolveTarget || !resolutionText.trim()) return;
    resolveMutation.mutate(
      { id: resolveTarget.id, resolution: resolutionText.trim() },
      {
        onSuccess: () => {
          setResolveTarget(null);
          setResolutionText('');
        },
      },
    );
  }

  const projectTabs = [
    { key: 'all', label: t.allFilter },
    ...projects.map((p) => ({ key: p.id, label: p.name })),
  ];

  const statusTabs: { key: TabStatus; label: string }[] = [
    { key: 'pending', label: t.briefings.tabPending },
    { key: 'resolved', label: t.briefings.tabResolved },
    { key: 'dismissed', label: t.briefings.tabDismissed },
    { key: 'expired', label: t.briefings.tabExpired },
  ];

  return (
    <div className="space-y-4">
      {/* Project Tab */}
      <TabBar tabs={projectTabs} active={projectTab} onChange={setProjectTab} />

      {/* Status Tab */}
      <div className="flex flex-wrap items-center gap-3">
        <TabBar tabs={statusTabs} active={statusTab} onChange={setStatusTab} />
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          <Switch checked={showAuto} onCheckedChange={(checked) => setShowAuto(checked)} />
          {t.briefings.showAuto}
        </label>
      </div>

      {/* Tag Tab — only shown once briefings actually carry tags */}
      {tagOptions.length > 0 && (
        <TabBar
          tabs={[
            { key: 'all', label: t.briefings.allTags },
            ...tagOptions.map((tag) => ({ key: tag, label: tag })),
          ]}
          active={activeTag}
          onChange={setTagTab}
        />
      )}

      {/* Content */}
      {isLoading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-40" />
          ))}
        </div>
      ) : error ? (
        <p role="alert" className="text-sm text-destructive">{t.common.loadFailed(error.message)}</p>
      ) : briefings.length === 0 ? (
        <div className="rounded-lg border bg-muted/30 p-12 text-center">
          <Bell className="mx-auto h-10 w-10 text-muted-foreground/50" />
          <p className="mt-3 text-sm text-muted-foreground">{t.briefings.noItems}</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {briefings.map((b) => (
            <BriefingCard
              key={b.id}
              briefing={b}
              projectName={b.project_id ? projectNameMap.get(b.project_id) : undefined}
              onResolve={handleOpenResolve}
              onDismiss={(id) => dismissMutation.mutate(id)}
              dismissing={dismissMutation.isPending}
            />
          ))}
        </div>
      )}

      {/* Resolve Dialog */}
      <Dialog
        open={!!resolveTarget}
        onOpenChange={(open) => { if (!open) setResolveTarget(null); }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t.briefings.resolveDialogTitle}</DialogTitle>
            <DialogDescription>
              {resolveTarget?.title}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="resolution-input">{t.briefings.resolutionLabel}</Label>
            <Textarea
              id="resolution-input"
              placeholder={t.briefings.resolutionPlaceholder}
              value={resolutionText}
              onChange={(e) => setResolutionText(e.target.value)}
              rows={4}
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setResolveTarget(null)}
            >
              {t.common.cancel}
            </Button>
            <Button
              onClick={handleConfirmResolve}
              disabled={!resolutionText.trim() || resolveMutation.isPending}
            >
              {resolveMutation.isPending ? t.common.submitting : t.briefings.confirmResolve}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function kindVariant(kind: string): 'destructive' | 'outline' | 'secondary' | 'default' {
  if (kind === 'blocked') return 'destructive';
  if (kind === 'action' || kind === 'decision') return 'outline';
  if (kind === 'done') return 'default';
  return 'secondary';
}

function NoticeCard({ notice, readOnly }: { notice: NoticeItem; readOnly?: boolean }) {
  const t = useT();
  const action = useNoticeAction();
  const kindLabel = notice.kind ? t.notices.kind[notice.kind] : notice.catalog_id;
  const delivery = notice.last_delivery;
  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant={kindVariant(notice.kind)} className="text-[11px]">{kindLabel}</Badge>
        {notice.design_number && (
          <Badge variant="secondary" className="text-[10px] font-normal">{notice.design_number}</Badge>
        )}
        {notice.status === 'snoozed' && notice.snoozed_until && (
          <Badge variant="outline" className="text-[10px] font-normal">
            {t.notices.snoozedUntil(formatDateTime(notice.snoozed_until))}
          </Badge>
        )}
      </div>
      <p className="text-sm font-medium leading-relaxed break-words">{noticeText(notice.user_line)}</p>
      <div className="grid grid-cols-1 gap-x-4 gap-y-0.5 text-xs text-muted-foreground sm:grid-cols-2">
        <span>{t.notices.source}: {notice.source || '-'}</span>
        <span>{t.notices.host}: {notice.host || t.notices.hostAll}</span>
        <span>{t.notices.firstSeen}: {formatDateTime(notice.first_seen_at)}</span>
        <span>{t.notices.lastSeen}: {formatDateTime(notice.last_seen_at)}</span>
        <span className="sm:col-span-2">
          {delivery
            ? t.notices.lastDelivery(delivery.event, formatDateTime(delivery.emitted_at ?? delivery.claimed_at))
            : t.notices.neverDelivered}
        </span>
      </div>
      {!readOnly && (
        <div className="flex gap-2 pt-1">
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            disabled={action.isPending}
            onClick={() => action.mutate({ key: notice.key, action: 'snooze', hours: 24 })}
          >
            {t.notices.snooze24}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            disabled={action.isPending}
            onClick={() => action.mutate({ key: notice.key, action: 'dismiss' })}
          >
            <XCircle className="h-3.5 w-3.5 mr-1" />
            {t.notices.dismiss}
          </Button>
        </div>
      )}
    </div>
  );
}

function NoticesTab() {
  const t = useT();
  const lang = useLang();
  const queued = useNotices({ status: 'active', group: 'queued', language: lang });
  const blocks = useNotices({ status: 'all', group: 'immediate', language: lang, limit: 20 });
  const items = queued.data?.items ?? [];
  const recent = blocks.data?.items ?? [];

  return (
    <div className="space-y-6">
      {queued.isLoading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-28" />)}
        </div>
      ) : queued.error ? (
        <p role="alert" className="text-sm text-destructive">{t.common.loadFailed(queued.error.message)}</p>
      ) : items.length === 0 ? (
        <div className="rounded-lg border bg-muted/30 p-12 text-center">
          <Bell className="mx-auto h-10 w-10 text-muted-foreground/50" />
          <p className="mt-3 text-sm text-muted-foreground">{t.notices.noNotices}</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          {items.map((notice) => <NoticeCard key={notice.key} notice={notice} />)}
        </div>
      )}

      <section className="space-y-3">
        <div className="flex items-center gap-2">
          <ShieldAlert className="h-4 w-4 text-muted-foreground" />
          <h2 className="text-sm font-semibold">{t.notices.recentBlocks}</h2>
        </div>
        <p className="text-xs text-muted-foreground">{t.notices.recentBlocksHint}</p>
        {blocks.error ? (
          <p role="alert" className="text-sm text-destructive">{t.common.loadFailed(blocks.error.message)}</p>
        ) : recent.length === 0 && !blocks.isLoading ? (
          <p className="text-sm text-muted-foreground">{t.notices.noBlocks}</p>
        ) : (
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            {recent.map((notice) => <NoticeCard key={notice.key} notice={notice} readOnly />)}
          </div>
        )}
      </section>
    </div>
  );
}

type PageTab = 'notices' | 'decisions';

export function BriefingsPage() {
  const t = useT();
  const [tab, setTab] = useState<PageTab>('notices');
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Bell className="h-5 w-5 text-muted-foreground" />
        <h1 className="text-lg font-semibold">{t.briefings.title}</h1>
      </div>
      <TabBar
        tabs={[
          { key: 'notices', label: t.notices.tabNotices },
          { key: 'decisions', label: t.notices.tabDecisions },
        ]}
        active={tab}
        onChange={setTab}
      />
      {tab === 'notices' ? <NoticesTab /> : <DecisionsTab />}
    </div>
  );
}
