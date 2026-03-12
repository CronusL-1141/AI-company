import { useLocation, Link } from 'react-router-dom';
import {
  LayoutDashboard,
  Users,
  ListTodo,
  Activity,
  Settings,
  Bot,
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

const navItems = [
  { title: '总览', path: '/', icon: LayoutDashboard },
  { title: '团队管理', path: '/teams', icon: Users },
  { title: '任务看板', path: '/tasks', icon: ListTodo },
  { title: '事件日志', path: '/events', icon: Activity },
  { title: '设置', path: '/settings', icon: Settings },
];

export function AppSidebar() {
  const location = useLocation();
  const connected = useWSStore((s) => s.connected);

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
          <SidebarGroupLabel>导航</SidebarGroupLabel>
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
          <span>{connected ? '已连接' : '未连接'}</span>
          <Badge variant="secondary" className="ml-auto text-[10px]">
            v0.1
          </Badge>
        </div>
      </SidebarFooter>
    </Sidebar>
  );
}
