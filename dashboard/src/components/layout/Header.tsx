import { useLocation } from 'react-router-dom';
import { SidebarTrigger } from '@/components/ui/sidebar';
import { Separator } from '@/components/ui/separator';
import { GlobalSearch } from '@/components/layout/GlobalSearch';
import { useT, type Translations } from '@/i18n';

type NavTitles = Translations['nav'];

// Every routed page, longest prefix first: detail pages take their section's title.
const TITLE_KEYS: Array<[string, Exclude<keyof NavTitles, 'label'>]> = [
  ['/usage/accounts', 'accountUsage'],
  ['/agent-live', 'agentLive'],
  ['/projects', 'projects'],
  ['/tasks', 'tasks'],
  ['/events', 'events'],
  ['/meetings', 'meetings'],
  ['/analytics', 'analytics'],
  ['/agents', 'agents'],
  ['/briefings', 'briefings'],
  ['/reports', 'reports'],
  ['/workflows', 'workflows'],
  ['/pipelines', 'workflows'],
  ['/failures', 'failures'],
  ['/prompts', 'prompts'],
  ['/ecosystem', 'ecosystem'],
  ['/usage', 'usage'],
  ['/settings', 'settings'],
];

/** Header title for a path: the matching section, else the overview. */
export function pageTitle(pathname: string, nav: NavTitles): string {
  const match = TITLE_KEYS.find(([prefix]) => pathname === prefix || pathname.startsWith(`${prefix}/`));
  return match ? nav[match[1]] : nav.overview;
}

export function Header() {
  const location = useLocation();
  const t = useT();
  const title = pageTitle(location.pathname, t.nav);

  return (
    <header className="flex h-14 items-center gap-3 border-b px-4">
      <SidebarTrigger />
      <Separator orientation="vertical" className="h-5" />
      <h1 className="text-lg font-semibold">{title}</h1>
      <GlobalSearch />
    </header>
  );
}
