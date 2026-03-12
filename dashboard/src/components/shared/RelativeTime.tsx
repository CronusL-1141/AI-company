import { useMemo } from 'react';

function formatRelativeTime(dateStr: string): string {
  const date = new Date(dateStr);
  const now = Date.now();
  const diff = now - date.getTime();
  const seconds = Math.floor(diff / 1000);

  if (seconds < 60) return '刚刚';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}天前`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}个月前`;
  return `${Math.floor(months / 12)}年前`;
}

export function RelativeTime({ date }: { date: string }) {
  const text = useMemo(() => formatRelativeTime(date), [date]);
  const full = useMemo(() => new Date(date).toLocaleString('zh-CN'), [date]);

  return (
    <time dateTime={date} title={full}>
      {text}
    </time>
  );
}
