import { useState } from 'react';
import { Save, ExternalLink, Plus, Trash2, Users } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { Separator } from '@/components/ui/separator';
import { Badge } from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  useTeamDefaults,
  useUpdateTeamDefaults,
  useAddPermanentMember,
  useRemovePermanentMember,
  useTogglePermanentMember,
  type PermanentMember,
} from '@/api/teamConfig';
import { useTeamTemplates, type TeamTemplate } from '@/api/teamTemplates';

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

  // 团队配置
  const { data: teamDefaults, isLoading: teamDefaultsLoading } = useTeamDefaults();
  const updateDefaults = useUpdateTeamDefaults();
  const addMember = useAddPermanentMember();
  const removeMember = useRemovePermanentMember();
  const toggleMember = useTogglePermanentMember();

  // 团队模板
  const { data: teamTemplates, isLoading: templatesLoading } = useTeamTemplates();

  const [autoCreateTeam, setAutoCreateTeam] = useState<boolean | null>(null);
  const [teamNamePrefix, setTeamNamePrefix] = useState<string | null>(null);
  const [editingMember, setEditingMember] = useState<string | null>(null);
  const [editValues, setEditValues] = useState<Partial<PermanentMember>>({});
  const [newMember, setNewMember] = useState<PermanentMember | null>(null);

  // 计算实际显示值（本地编辑值优先，否则用服务端数据）
  const currentAutoCreate = autoCreateTeam ?? teamDefaults?.auto_create_team ?? false;
  const currentPrefix = teamNamePrefix ?? teamDefaults?.team_name_prefix ?? '';
  const members = teamDefaults?.permanent_members ?? [];

  // toast状态
  const [showToast, setShowToast] = useState(false);
  const [toastMessage, setToastMessage] = useState('设置已保存');

  const handleStorageChange = (value: string | null) => {
    if (!value) return;
    setStorageBackend(value);
    setDbUrl(value === 'sqlite' ? 'sqlite:///data/aiteam.db' : 'postgresql://localhost:5432/aiteam');
  };

  const showNotification = (msg: string) => {
    setToastMessage(msg);
    setShowToast(true);
    setTimeout(() => setShowToast(false), 2500);
  };

  const handleSave = () => {
    showNotification('设置已保存');
  };

  const handleTeamConfigSave = () => {
    if (!teamDefaults) return;
    updateDefaults.mutate(
      {
        auto_create_team: currentAutoCreate,
        team_name_prefix: currentPrefix,
        permanent_members: teamDefaults.permanent_members,
      },
      {
        onSuccess: () => {
          setAutoCreateTeam(null);
          setTeamNamePrefix(null);
          showNotification('团队配置已保存');
        },
      },
    );
  };

  const handleAddMember = () => {
    if (!newMember?.name) return;
    addMember.mutate(newMember, {
      onSuccess: () => {
        setNewMember(null);
        showNotification('成员已添加');
      },
    });
  };

  const handleRemoveMember = (name: string) => {
    removeMember.mutate(name, {
      onSuccess: () => showNotification('成员已删除'),
    });
  };

  const handleToggleMember = (name: string, enabled: boolean) => {
    toggleMember.mutate({ name, enabled });
  };

  const handleApplyTemplate = (template: TeamTemplate) => {
    if (!teamDefaults) return;
    const existingNames = new Set(teamDefaults.permanent_members.map((m) => m.name));
    const newMembers: PermanentMember[] = template.members
      .filter((m) => !existingNames.has(m.name))
      .map((m) => ({ name: m.name, role: m.role, model: 'claude-sonnet-4-6', enabled: true }));
    if (newMembers.length === 0) {
      showNotification('模板中的成员已全部存在');
      return;
    }
    updateDefaults.mutate(
      {
        ...teamDefaults,
        permanent_members: [...teamDefaults.permanent_members, ...newMembers],
      },
      {
        onSuccess: () => showNotification(`已从"${template.name}"添加 ${newMembers.length} 个成员`),
      },
    );
  };

  const startEditing = (member: PermanentMember) => {
    setEditingMember(member.name);
    setEditValues({ name: member.name, role: member.role, model: member.model });
  };

  const saveEditing = () => {
    if (!editingMember || !teamDefaults) return;
    // 用PUT整体更新来保存编辑
    const updatedMembers = teamDefaults.permanent_members.map((m) =>
      m.name === editingMember
        ? { ...m, name: editValues.name || m.name, role: editValues.role || m.role, model: editValues.model || m.model }
        : m,
    );
    updateDefaults.mutate(
      {
        ...teamDefaults,
        permanent_members: updatedMembers,
      },
      {
        onSuccess: () => {
          setEditingMember(null);
          setEditValues({});
          showNotification('成员信息已更新');
        },
      },
    );
  };

  const cancelEditing = () => {
    setEditingMember(null);
    setEditValues({});
  };

  return (
    <div className="space-y-6">
      {/* Toast通知 */}
      {showToast && (
        <div className="fixed top-4 right-4 z-50 rounded-lg border bg-background px-4 py-3 text-sm shadow-lg ring-1 ring-foreground/10 animate-in fade-in slide-in-from-top-2">
          {toastMessage}
        </div>
      )}

      <Tabs defaultValue={0}>
        <TabsList>
          <TabsTrigger value={0}>通用设置</TabsTrigger>
          <TabsTrigger value={1}>基础设施</TabsTrigger>
          <TabsTrigger value={2}>团队配置</TabsTrigger>
          <TabsTrigger value={3}>关于</TabsTrigger>
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

        {/* Tab 3: 团队配置 */}
        <TabsContent value={2}>
          <div className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle>团队默认配置</CardTitle>
                <CardDescription>配置自动创建团队和团队名称前缀</CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">
                <div className="flex items-center justify-between">
                  <div className="space-y-0.5">
                    <Label>自动创建团队</Label>
                    <p className="text-xs text-muted-foreground">新项目启动时自动创建团队</p>
                  </div>
                  <Switch
                    checked={currentAutoCreate}
                    onCheckedChange={(checked) => setAutoCreateTeam(checked)}
                    disabled={teamDefaultsLoading}
                  />
                </div>

                <div className="grid gap-2">
                  <Label htmlFor="team-prefix">团队名称前缀</Label>
                  <Input
                    id="team-prefix"
                    value={currentPrefix}
                    onChange={(e) => setTeamNamePrefix(e.target.value)}
                    placeholder="例如: project-alpha"
                    disabled={teamDefaultsLoading}
                  />
                  <p className="text-xs text-muted-foreground">自动创建团队时使用的名称前缀</p>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>团队模板</CardTitle>
                <CardDescription>选择预设模板快速添加成员到常驻列表</CardDescription>
              </CardHeader>
              <CardContent>
                {templatesLoading ? (
                  <p className="text-sm text-muted-foreground">加载中...</p>
                ) : !teamTemplates?.length ? (
                  <p className="text-sm text-muted-foreground">暂无可用模板</p>
                ) : (
                  <div className="grid gap-3 sm:grid-cols-2">
                    {teamTemplates.map((tpl) => (
                      <div
                        key={tpl.id}
                        className="flex items-start justify-between rounded-lg border p-3"
                      >
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <Users className="size-4 shrink-0 text-muted-foreground" />
                            <span className="text-sm font-medium">{tpl.name}</span>
                            <Badge variant="secondary">{tpl.members.length} 人</Badge>
                          </div>
                          <p className="mt-1 text-xs text-muted-foreground">{tpl.description}</p>
                        </div>
                        <Button
                          size="sm"
                          variant="outline"
                          className="ml-2 shrink-0"
                          onClick={() => handleApplyTemplate(tpl)}
                          disabled={updateDefaults.isPending || teamDefaultsLoading}
                        >
                          使用此模板
                        </Button>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>常驻团队成员</CardTitle>
                <CardDescription>
                  配置每次创建团队时自动添加的常驻成员
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                {teamDefaultsLoading ? (
                  <p className="text-sm text-muted-foreground">加载中...</p>
                ) : members.length === 0 && !newMember ? (
                  <p className="text-sm text-muted-foreground">暂无常驻成员，点击下方按钮添加</p>
                ) : (
                  <div className="space-y-3">
                    {members.map((member) => (
                      <div
                        key={member.name}
                        className="flex items-center gap-3 rounded-lg border p-3"
                      >
                        {editingMember === member.name ? (
                          <>
                            <div className="grid flex-1 gap-2">
                              <div className="grid grid-cols-3 gap-2">
                                <Input
                                  value={editValues.name ?? ''}
                                  onChange={(e) =>
                                    setEditValues((v) => ({ ...v, name: e.target.value }))
                                  }
                                  placeholder="名称"
                                />
                                <Input
                                  value={editValues.role ?? ''}
                                  onChange={(e) =>
                                    setEditValues((v) => ({ ...v, role: e.target.value }))
                                  }
                                  placeholder="角色描述"
                                />
                                <Select
                                  value={editValues.model ?? 'claude-sonnet-4-6'}
                                  onValueChange={(v) =>
                                    v && setEditValues((prev) => ({ ...prev, model: v }))
                                  }
                                >
                                  <SelectTrigger className="w-full">
                                    <SelectValue />
                                  </SelectTrigger>
                                  <SelectContent>
                                    <SelectItem value="claude-opus-4-6">Claude Opus 4.6</SelectItem>
                                    <SelectItem value="claude-sonnet-4-6">Claude Sonnet 4.6</SelectItem>
                                    <SelectItem value="claude-haiku-4-5">Claude Haiku 4.5</SelectItem>
                                  </SelectContent>
                                </Select>
                              </div>
                            </div>
                            <Button size="sm" onClick={saveEditing} disabled={updateDefaults.isPending}>
                              保存
                            </Button>
                            <Button size="sm" variant="outline" onClick={cancelEditing}>
                              取消
                            </Button>
                          </>
                        ) : (
                          <>
                            <div
                              className="flex flex-1 cursor-pointer items-center gap-3"
                              onClick={() => startEditing(member)}
                            >
                              <div className="min-w-0 flex-1">
                                <div className="flex items-center gap-2">
                                  <span className="text-sm font-medium">{member.name}</span>
                                  <Badge variant={member.enabled ? 'default' : 'secondary'}>
                                    {member.enabled ? '启用' : '禁用'}
                                  </Badge>
                                </div>
                                <p className="text-xs text-muted-foreground">{member.role}</p>
                              </div>
                              <span className="shrink-0 text-xs text-muted-foreground">
                                {member.model}
                              </span>
                            </div>
                            <Switch
                              checked={member.enabled}
                              onCheckedChange={(checked) =>
                                handleToggleMember(member.name, checked)
                              }
                            />
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => handleRemoveMember(member.name)}
                              disabled={removeMember.isPending}
                            >
                              <Trash2 className="size-4" />
                            </Button>
                          </>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {/* 新增成员行 */}
                {newMember && (
                  <div className="flex items-center gap-3 rounded-lg border border-dashed p-3">
                    <div className="grid flex-1 gap-2">
                      <div className="grid grid-cols-3 gap-2">
                        <Input
                          value={newMember.name}
                          onChange={(e) =>
                            setNewMember((m) => m && { ...m, name: e.target.value })
                          }
                          placeholder="成员名称"
                          autoFocus
                        />
                        <Input
                          value={newMember.role}
                          onChange={(e) =>
                            setNewMember((m) => m && { ...m, role: e.target.value })
                          }
                          placeholder="角色描述"
                        />
                        <Select
                          value={newMember.model}
                          onValueChange={(v) =>
                            v && setNewMember((m) => m && { ...m, model: v })
                          }
                        >
                          <SelectTrigger className="w-full">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="claude-opus-4-6">Claude Opus 4.6</SelectItem>
                            <SelectItem value="claude-sonnet-4-6">Claude Sonnet 4.6</SelectItem>
                            <SelectItem value="claude-haiku-4-5">Claude Haiku 4.5</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                    </div>
                    <Button size="sm" onClick={handleAddMember} disabled={addMember.isPending || !newMember.name}>
                      添加
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setNewMember(null)}>
                      取消
                    </Button>
                  </div>
                )}

                {!newMember && (
                  <Button
                    variant="outline"
                    className="w-full"
                    onClick={() =>
                      setNewMember({ name: '', role: '', model: 'claude-sonnet-4-6', enabled: true })
                    }
                  >
                    <Plus className="size-4" data-icon="inline-start" />
                    添加常驻成员
                  </Button>
                )}
              </CardContent>
            </Card>

            <div className="flex justify-end">
              <Button onClick={handleTeamConfigSave} disabled={updateDefaults.isPending}>
                <Save className="size-4" data-icon="inline-start" />
                保存团队配置
              </Button>
            </div>
          </div>
        </TabsContent>

        {/* Tab 4: 关于 */}
        <TabsContent value={3}>
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
