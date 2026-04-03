import { useState } from 'react';
import { Bell, CheckCircle, XCircle } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { useT } from '@/i18n';
import {
  useBriefings,
  useResolveBriefing,
  useDismissBriefing,
} from '@/api/briefings';
import type { Briefing, BriefingStatus } from '@/api/briefings';

type TabStatus = 'pending' | 'resolved' | 'dismissed';

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

interface BriefingCardProps {
  briefing: Briefing;
  onResolve: (b: Briefing) => void;
  onDismiss: (id: string) => void;
  dismissing: boolean;
}

function BriefingCard({ briefing, onResolve, onDismiss, dismissing }: BriefingCardProps) {
  const t = useT();
  const isPending = briefing.status === 'pending';

  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm space-y-3">
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-semibold leading-snug">{briefing.title}</h3>
        <UrgencyBadge urgency={briefing.urgency} />
      </div>

      <p className="text-xs text-muted-foreground leading-relaxed">{briefing.description}</p>

      {briefing.options.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-medium">{t.briefings.options}</p>
          <ul className="space-y-0.5">
            {briefing.options.map((opt, i) => (
              <li key={i} className="text-xs text-muted-foreground flex gap-1.5">
                <span className="text-primary font-medium">{i + 1}.</span>
                {opt}
              </li>
            ))}
          </ul>
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

export function BriefingsPage() {
  const t = useT();
  const [tab, setTab] = useState<TabStatus>('pending');
  const [resolveTarget, setResolveTarget] = useState<Briefing | null>(null);
  const [resolutionText, setResolutionText] = useState('');

  const { data, isLoading } = useBriefings(tab as BriefingStatus);
  const resolveMutation = useResolveBriefing();
  const dismissMutation = useDismissBriefing();

  const briefings = data?.data ?? [];

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

  const tabs: { key: TabStatus; label: string }[] = [
    { key: 'pending', label: t.briefings.tabPending },
    { key: 'resolved', label: t.briefings.tabResolved },
    { key: 'dismissed', label: t.briefings.tabDismissed },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-2">
        <Bell className="h-5 w-5 text-muted-foreground" />
        <h1 className="text-lg font-semibold">{t.briefings.title}</h1>
      </div>

      {/* Tabs */}
      <div className="flex rounded-lg border border-input bg-muted/30 p-0.5 w-fit">
        {tabs.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
              tab === key
                ? 'bg-background text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Content */}
      {isLoading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-40" />
          ))}
        </div>
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
