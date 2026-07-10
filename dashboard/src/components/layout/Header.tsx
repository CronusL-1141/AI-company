import { useLocation } from 'react-router-dom';
import { SidebarTrigger } from '@/components/ui/sidebar';
import { Separator } from '@/components/ui/separator';
import { GlobalSearch } from '@/components/layout/GlobalSearch';
import { useT } from '@/i18n';

export function Header() {
  const location = useLocation();
  const t = useT();

  const pageTitles: Record<string, string> = {
    '/': t.nav.overview,
    '/projects': t.nav.projects,
    '/tasks': t.nav.tasks,
    '/events': t.nav.events,
    '/meetings': t.nav.meetings,
    '/analytics': t.nav.analytics,
    '/settings': t.nav.settings,
    '/ecosystem': t.nav.ecosystem ?? '生态档案',
  };

  // ecosystem 详情页特殊处理
  let title = pageTitles[location.pathname] || t.nav.overview;
  if (location.pathname.startsWith('/ecosystem/')) {
    title = t.nav.ecosystem ?? '生态档案';
  }

  return (
    <header className="flex h-14 items-center gap-3 border-b px-4">
      <SidebarTrigger />
      <Separator orientation="vertical" className="h-5" />
      <h1 className="text-lg font-semibold">{title}</h1>
      <GlobalSearch />
    </header>
  );
}
