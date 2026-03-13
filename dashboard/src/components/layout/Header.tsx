import { useLocation } from 'react-router-dom';
import { SidebarTrigger } from '@/components/ui/sidebar';
import { Separator } from '@/components/ui/separator';

const pageTitles: Record<string, string> = {
  '/': '总览',
  '/teams': '团队管理',
  '/tasks': '任务看板',
  '/events': '事件日志',
  '/meetings': '会议室',
  '/settings': '设置',
};

export function Header() {
  const location = useLocation();
  const title = pageTitles[location.pathname] || '总览';

  return (
    <header className="flex h-14 items-center gap-3 border-b px-4">
      <SidebarTrigger />
      <Separator orientation="vertical" className="h-5" />
      <h1 className="text-lg font-semibold">{title}</h1>
    </header>
  );
}
