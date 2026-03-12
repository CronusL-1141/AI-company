import { useState } from 'react';
import { Save, ExternalLink } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { Separator } from '@/components/ui/separator';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

export function SettingsPage() {
  // 通用设置
  const [projectName, setProjectName] = useState('AI Team OS');
  const [projectDesc, setProjectDesc] = useState('通用可复用的AI Agent团队操作系统框架');
  const [defaultModel, setDefaultModel] = useState('claude-sonnet-4-6');
  const [defaultLang, setDefaultLang] = useState('zh');
  const [darkMode, setDarkMode] = useState(false);

  // 基础设施设置
  const [storageBackend, setStorageBackend] = useState('sqlite');
  const [dbUrl, setDbUrl] = useState('sqlite:///data/aiteam.db');
  const [cacheBackend, setCacheBackend] = useState('memory');
  const [redisUrl, setRedisUrl] = useState('redis://localhost:6379');
  const [memoryBackend, setMemoryBackend] = useState('file');
  const [apiPort, setApiPort] = useState('8000');
  const [dashboardPort, setDashboardPort] = useState('5173');

  // toast状态
  const [showToast, setShowToast] = useState(false);

  const handleStorageChange = (value: string | null) => {
    if (!value) return;
    setStorageBackend(value);
    setDbUrl(value === 'sqlite' ? 'sqlite:///data/aiteam.db' : 'postgresql://localhost:5432/aiteam');
  };

  const handleSave = () => {
    setShowToast(true);
    setTimeout(() => setShowToast(false), 2500);
  };

  return (
    <div className="space-y-6">
      {/* Toast通知 */}
      {showToast && (
        <div className="fixed top-4 right-4 z-50 rounded-lg border bg-background px-4 py-3 text-sm shadow-lg ring-1 ring-foreground/10 animate-in fade-in slide-in-from-top-2">
          设置已保存
        </div>
      )}

      <Tabs defaultValue={0}>
        <TabsList>
          <TabsTrigger value={0}>通用设置</TabsTrigger>
          <TabsTrigger value={1}>基础设施</TabsTrigger>
          <TabsTrigger value={2}>关于</TabsTrigger>
        </TabsList>

        {/* Tab 1: 通用设置 */}
        <TabsContent value={0}>
          <Card>
            <CardHeader>
              <CardTitle>通用设置</CardTitle>
              <CardDescription>配置项目基本信息和界面偏好</CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="grid gap-2">
                <Label htmlFor="project-name">项目名称</Label>
                <Input
                  id="project-name"
                  value={projectName}
                  onChange={(e) => setProjectName(e.target.value)}
                  placeholder="请输入项目名称"
                />
              </div>

              <div className="grid gap-2">
                <Label htmlFor="project-desc">项目描述</Label>
                <Textarea
                  id="project-desc"
                  value={projectDesc}
                  onChange={(e) => setProjectDesc(e.target.value)}
                  placeholder="请输入项目描述"
                  rows={3}
                />
              </div>

              <div className="grid gap-2">
                <Label>默认LLM模型</Label>
                <Select value={defaultModel} onValueChange={(v) => v && setDefaultModel(v)}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="claude-opus-4-6">Claude Opus 4.6</SelectItem>
                    <SelectItem value="claude-sonnet-4-6">Claude Sonnet 4.6</SelectItem>
                    <SelectItem value="gpt-4o">GPT-4o</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className="grid gap-2">
                <Label>默认语言</Label>
                <Select value={defaultLang} onValueChange={(v) => v && setDefaultLang(v)}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="zh">中文</SelectItem>
                    <SelectItem value="en">English</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className="flex items-center justify-between">
                <div className="space-y-0.5">
                  <Label>深色主题</Label>
                  <p className="text-xs text-muted-foreground">切换深色/浅色主题模式</p>
                </div>
                <Switch
                  checked={darkMode}
                  onCheckedChange={(checked) => setDarkMode(checked)}
                />
              </div>

              <Separator />

              <div className="flex justify-end">
                <Button onClick={handleSave}>
                  <Save className="size-4" data-icon="inline-start" />
                  保存设置
                </Button>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* Tab 2: 基础设施 */}
        <TabsContent value={1}>
          <div className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle>存储配置</CardTitle>
                <CardDescription>数据库和缓存后端设置</CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">
                <div className="grid gap-2">
                  <Label>存储后端</Label>
                  <Select value={storageBackend} onValueChange={handleStorageChange}>
                    <SelectTrigger className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="sqlite">SQLite</SelectItem>
                      <SelectItem value="postgresql">PostgreSQL</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                <div className="grid gap-2">
                  <Label htmlFor="db-url">数据库URL</Label>
                  <Input
                    id="db-url"
                    value={dbUrl}
                    onChange={(e) => setDbUrl(e.target.value)}
                    placeholder={storageBackend === 'sqlite' ? 'sqlite:///data/aiteam.db' : 'postgresql://localhost:5432/aiteam'}
                  />
                  <p className="text-xs text-muted-foreground">
                    {storageBackend === 'sqlite' ? 'SQLite数据库文件路径' : 'PostgreSQL连接字符串'}
                  </p>
                </div>

                <Separator />

                <div className="grid gap-2">
                  <Label>缓存后端</Label>
                  <Select value={cacheBackend} onValueChange={(v) => v && setCacheBackend(v)}>
                    <SelectTrigger className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="memory">内存缓存</SelectItem>
                      <SelectItem value="redis">Redis</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                {cacheBackend === 'redis' && (
                  <div className="grid gap-2">
                    <Label htmlFor="redis-url">Redis URL</Label>
                    <Input
                      id="redis-url"
                      value={redisUrl}
                      onChange={(e) => setRedisUrl(e.target.value)}
                      placeholder="redis://localhost:6379"
                    />
                  </div>
                )}

                <Separator />

                <div className="grid gap-2">
                  <Label>记忆后端</Label>
                  <Select value={memoryBackend} onValueChange={(v) => v && setMemoryBackend(v)}>
                    <SelectTrigger className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="file">文件系统</SelectItem>
                      <SelectItem value="mem0">Mem0</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>服务端口</CardTitle>
                <CardDescription>API和Dashboard服务端口配置</CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">
                <div className="grid gap-2">
                  <Label htmlFor="api-port">API端口</Label>
                  <Input
                    id="api-port"
                    type="number"
                    value={apiPort}
                    onChange={(e) => setApiPort(e.target.value)}
                    placeholder="8000"
                  />
                </div>

                <div className="grid gap-2">
                  <Label htmlFor="dashboard-port">Dashboard端口</Label>
                  <Input
                    id="dashboard-port"
                    type="number"
                    value={dashboardPort}
                    onChange={(e) => setDashboardPort(e.target.value)}
                    placeholder="5173"
                  />
                </div>
              </CardContent>
            </Card>

            <div className="flex justify-end">
              <Button onClick={handleSave}>
                <Save className="size-4" data-icon="inline-start" />
                保存设置
              </Button>
            </div>
          </div>
        </TabsContent>

        {/* Tab 3: 关于 */}
        <TabsContent value={2}>
          <Card>
            <CardHeader>
              <CardTitle>关于 AI Team OS</CardTitle>
              <CardDescription>版本和项目信息</CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">版本</span>
                  <span className="text-sm text-muted-foreground">v0.2.0</span>
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">技术栈</span>
                  <span className="text-sm text-muted-foreground">LangGraph + FastAPI + React</span>
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">许可证</span>
                  <span className="text-sm text-muted-foreground">MIT License</span>
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">Python</span>
                  <span className="text-sm text-muted-foreground">3.11+</span>
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">Node.js</span>
                  <span className="text-sm text-muted-foreground">18+</span>
                </div>
              </div>

              <Separator />

              <div className="space-y-3">
                <h4 className="text-sm font-medium">核心依赖</h4>
                <div className="grid grid-cols-2 gap-2 text-sm text-muted-foreground">
                  <span>LangGraph — AI编排引擎</span>
                  <span>FastAPI — REST API框架</span>
                  <span>Mem0 — 记忆管理</span>
                  <span>React + TypeScript — 前端</span>
                  <span>SQLite / PostgreSQL — 数据存储</span>
                  <span>Zustand — 状态管理</span>
                </div>
              </div>

              <Separator />

              <div className="flex gap-3">
                <Button
                  variant="outline"
                  size="sm"
                  render={<a href="https://github.com/anthropics/ai-team-os" target="_blank" rel="noopener noreferrer" />}
                >
                  <ExternalLink className="size-3.5" data-icon="inline-start" />
                  GitHub
                </Button>
              </div>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
