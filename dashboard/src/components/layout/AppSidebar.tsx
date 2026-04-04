import { useLocation, Link } from 'react-router-dom';
import {
  LayoutDashboard,
  Users,
  ListTodo,
  Activity,
  MessageSquare,
  Settings,
  Bot,
  BarChart3,
  BookOpen,
  Cpu,
  Bell,
} from 'lucide-react';
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarFooter,
} from '@/components/ui/sidebar';
import { Badge } from '@/components/ui/badge';
import { useWSStore } from '@/stores/websocket';
import { useT } from '@/i18n';

export function AppSidebar() {
  const location = useLocation();
  const connected = useWSStore((s) => s.connected);
  const t = useT();

  const navItems = [
    { title: t.nav.overview, path: '/', icon: LayoutDashboard },
    { title: t.nav.projects, path: '/projects', icon: Users },
    { title: t.nav.tasks, path: '/tasks', icon: ListTodo },
    { title: t.nav.events, path: '/events', icon: Activity },
    { title: t.nav.meetings, path: '/meetings', icon: MessageSquare },
    { title: t.nav.analytics, path: '/analytics', icon: BarChart3 },
    { title: t.nav.agents, path: '/agents', icon: Cpu },
    { title: t.nav.agentLive, path: '/agent-live', icon: Activity },
    { title: t.nav.reports, path: '/reports', icon: BookOpen },
    { title: t.nav.briefings, path: '/briefings', icon: Bell },
    { title: t.nav.settings, path: '/settings', icon: Settings },
  ];

  return (
    <Sidebar>
      <SidebarHeader className="border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <Bot className="h-6 w-6 text-primary" />
          <span className="text-lg font-semibold">AI Team OS</span>
        </div>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>{t.nav.label}</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {navItems.map((item) => (
                <SidebarMenuItem key={item.path}>
                  <SidebarMenuButton
                    render={<Link to={item.path} />}
                    isActive={
                      item.path === '/'
                        ? location.pathname === '/'
                        : location.pathname.startsWith(item.path)
                    }
                  >
                    <item.icon className="h-4 w-4" />
                    <span>{item.title}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter className="border-t px-4 py-3">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span
            className={`h-2 w-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`}
          />
          <span>{connected ? t.status.connected : t.status.disconnected}</span>
          <Badge variant="secondary" className="ml-auto text-[10px]">
            v0.1
          </Badge>
        </div>
      </SidebarFooter>
    </Sidebar>
  );
}
