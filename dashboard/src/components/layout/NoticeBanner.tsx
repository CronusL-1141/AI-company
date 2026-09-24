import { Link } from 'react-router-dom';
import { AlertTriangle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { noticeText, useNoticeAction, useNoticeSummary } from '@/api/notices';
import { useLang, useT } from '@/i18n';

/** One line across every page: the most severe, newest notice that waits on the user. */
export function NoticeBanner() {
  const t = useT();
  const lang = useLang();
  const { data } = useNoticeSummary(lang);
  const action = useNoticeAction();
  const top = data?.top;
  if (!top) return null;
  const others = Math.max(0, (data?.total ?? 0) - 1);

  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-2 border-b border-yellow-500/30 bg-yellow-500/10 px-6 py-2 text-sm"
    >
      <AlertTriangle className="h-4 w-4 shrink-0 text-yellow-600" />
      <span className="min-w-0 flex-1 break-words">{noticeText(top.user_line)}</span>
      {others > 0 && (
        <span className="text-xs text-muted-foreground">{t.notices.bannerMore(others)}</span>
      )}
      <Button
        size="sm"
        variant="outline"
        className="h-7 text-xs"
        disabled={action.isPending}
        onClick={() => action.mutate({ key: top.key, action: 'snooze', hours: 24 })}
      >
        {t.notices.snooze24}
      </Button>
      <Button size="sm" variant="ghost" className="h-7 text-xs" nativeButton={false} render={<Link to="/briefings" />}>
        {t.notices.viewAll}
      </Button>
    </div>
  );
}
