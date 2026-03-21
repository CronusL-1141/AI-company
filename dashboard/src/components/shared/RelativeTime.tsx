import { useMemo } from 'react';
import { useT } from '@/i18n';

export function RelativeTime({ date }: { date: string }) {
  const t = useT();

  const text = useMemo(() => {
    const d = new Date(date);
    const now = Date.now();
    const diff = now - d.getTime();
    const seconds = Math.floor(diff / 1000);

    if (seconds < 60) return t.analytics.timeJustNow;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return t.analytics.timeMinutesAgo(minutes);
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return t.analytics.timeHoursAgo(hours);
    const days = Math.floor(hours / 24);
    if (days < 30) return t.analytics.timeDaysAgo(days);
    const months = Math.floor(days / 30);
    if (months < 12) return t.analytics.timeMonthsAgo(months);
    return t.analytics.timeYearsAgo(Math.floor(months / 12));
  }, [date, t]);

  const full = useMemo(() => new Date(date).toLocaleString(), [date]);

  return (
    <time dateTime={date} title={full}>
      {text}
    </time>
  );
}
