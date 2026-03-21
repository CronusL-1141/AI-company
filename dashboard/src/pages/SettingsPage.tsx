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
import { useContext } from 'react';
import { LanguageContext, type Lang, useT } from '@/i18n';

export function SettingsPage() {
  const t = useT();

  // 通用设置
  const [projectName, setProjectName] = useState('AI Team OS');
  const [projectDesc, setProjectDesc] = useState(t.settings.defaultProjectDesc);
  const [defaultModel, setDefaultModel] = useState('claude-sonnet-4-6');
  const [darkMode, setDarkMode] = useState(false);

  const langCtx = useContext(LanguageContext);
  const currentLang = langCtx?.lang ?? 'zh';
  const handleLangChange = (v: string | null) => {
    if (v && langCtx) langCtx.switchLang(v as Lang);
  };

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

  const currentAutoCreate = autoCreateTeam ?? teamDefaults?.auto_create_team ?? false;
  const currentPrefix = teamNamePrefix ?? teamDefaults?.team_name_prefix ?? '';
  const members = teamDefaults?.permanent_members ?? [];

  const [showToast, setShowToast] = useState(false);
  const [toastMessage, setToastMessage] = useState(t.settings.savedMsg);

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
    showNotification(t.settings.savedMsg);
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
          showNotification(t.settings.teamConfigSavedMsg);
        },
      },
    );
  };

  const handleAddMember = () => {
    if (!newMember?.name) return;
    addMember.mutate(newMember, {
      onSuccess: () => {
        setNewMember(null);
        showNotification(t.settings.memberAddedMsg);
      },
    });
  };

  const handleRemoveMember = (name: string) => {
    removeMember.mutate(name, {
      onSuccess: () => showNotification(t.settings.memberDeletedMsg),
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
      showNotification(t.settings.templateAlreadyExists);
      return;
    }
    updateDefaults.mutate(
      {
        ...teamDefaults,
        permanent_members: [...teamDefaults.permanent_members, ...newMembers],
      },
      {
        onSuccess: () => showNotification(t.settings.templateApplied(template.name, newMembers.length)),
      },
    );
  };

  const startEditing = (member: PermanentMember) => {
    setEditingMember(member.name);
    setEditValues({ name: member.name, role: member.role, model: member.model });
  };

  const saveEditing = () => {
    if (!editingMember || !teamDefaults) return;
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
          showNotification(t.settings.memberUpdatedMsg);
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
          <TabsTrigger value={0}>{t.settings.tabGeneral}</TabsTrigger>
          <TabsTrigger value={1}>{t.settings.tabInfra}</TabsTrigger>
          <TabsTrigger value={2}>{t.settings.tabTeam}</TabsTrigger>
          <TabsTrigger value={3}>{t.settings.tabAbout}</TabsTrigger>
        </TabsList>

        {/* Tab 1: 通用设置 */}
        <TabsContent value={0}>
          <Card>
            <CardHeader>
              <CardTitle>{t.settings.generalTitle}</CardTitle>
              <CardDescription>{t.settings.generalDesc}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="grid gap-2">
                <Label htmlFor="project-name">{t.settings.projectName}</Label>
                <Input
                  id="project-name"
                  value={projectName}
                  onChange={(e) => setProjectName(e.target.value)}
                  placeholder={t.settings.projectNamePlaceholder}
                />
              </div>

              <div className="grid gap-2">
                <Label htmlFor="project-desc">{t.settings.projectDesc}</Label>
                <Textarea
                  id="project-desc"
                  value={projectDesc}
                  onChange={(e) => setProjectDesc(e.target.value)}
                  placeholder={t.settings.projectDescPlaceholder}
                  rows={3}
                />
              </div>

              <div className="grid gap-2">
                <Label>{t.settings.defaultModel}</Label>
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
                <Label>{t.settings.interfaceLang}</Label>
                <Select value={currentLang} onValueChange={handleLangChange}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="zh">中文</SelectItem>
                    <SelectItem value="en">English</SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">{t.settings.langSwitchHint}</p>
              </div>

              <div className="flex items-center justify-between">
                <div className="space-y-0.5">
                  <Label>{t.settings.darkMode}</Label>
                  <p className="text-xs text-muted-foreground">{t.settings.darkModeHint}</p>
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
                  {t.settings.saveDemo}
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
                <CardTitle>{t.settings.storageTitle}</CardTitle>
                <CardDescription>{t.settings.storageDesc}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">
                <div className="grid gap-2">
                  <Label>{t.settings.storageBackend}</Label>
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
                  <Label htmlFor="db-url">{t.settings.dbUrl}</Label>
                  <Input
                    id="db-url"
                    value={dbUrl}
                    onChange={(e) => setDbUrl(e.target.value)}
                    placeholder={storageBackend === 'sqlite' ? 'sqlite:///data/aiteam.db' : 'postgresql://localhost:5432/aiteam'}
                  />
                  <p className="text-xs text-muted-foreground">
                    {storageBackend === 'sqlite' ? t.settings.dbUrlHintSqlite : t.settings.dbUrlHintPg}
                  </p>
                </div>

                <Separator />

                <div className="grid gap-2">
                  <Label>{t.settings.cacheBackend}</Label>
                  <Select value={cacheBackend} onValueChange={(v) => v && setCacheBackend(v)}>
                    <SelectTrigger className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="memory">{t.settings.cacheMemory}</SelectItem>
                      <SelectItem value="redis">{t.settings.cacheRedis}</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                {cacheBackend === 'redis' && (
                  <div className="grid gap-2">
                    <Label htmlFor="redis-url">{t.settings.redisUrl}</Label>
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
                  <Label>{t.settings.memoryBackend}</Label>
                  <Select value={memoryBackend} onValueChange={(v) => v && setMemoryBackend(v)}>
                    <SelectTrigger className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="file">{t.settings.memoryFile}</SelectItem>
                      <SelectItem value="mem0">Mem0</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>{t.settings.portsTitle}</CardTitle>
                <CardDescription>{t.settings.portsDesc}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">
                <div className="grid gap-2">
                  <Label htmlFor="api-port">{t.settings.apiPort}</Label>
                  <Input
                    id="api-port"
                    type="number"
                    value={apiPort}
                    onChange={(e) => setApiPort(e.target.value)}
                    placeholder="8000"
                  />
                </div>

                <div className="grid gap-2">
                  <Label htmlFor="dashboard-port">{t.settings.dashboardPort}</Label>
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
                {t.settings.saveDemo}
              </Button>
            </div>
          </div>
        </TabsContent>

        {/* Tab 3: 团队配置 */}
        <TabsContent value={2}>
          <div className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle>{t.settings.teamDefaultsTitle}</CardTitle>
                <CardDescription>{t.settings.teamDefaultsDesc}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">
                <div className="flex items-center justify-between">
                  <div className="space-y-0.5">
                    <Label>{t.settings.autoCreateTeam}</Label>
                    <p className="text-xs text-muted-foreground">{t.settings.autoCreateTeamHint}</p>
                  </div>
                  <Switch
                    checked={currentAutoCreate}
                    onCheckedChange={(checked) => setAutoCreateTeam(checked)}
                    disabled={teamDefaultsLoading}
                  />
                </div>

                <div className="grid gap-2">
                  <Label htmlFor="team-prefix">{t.settings.teamNamePrefix}</Label>
                  <Input
                    id="team-prefix"
                    value={currentPrefix}
                    onChange={(e) => setTeamNamePrefix(e.target.value)}
                    placeholder={t.settings.teamNamePrefixPlaceholder}
                    disabled={teamDefaultsLoading}
                  />
                  <p className="text-xs text-muted-foreground">{t.settings.teamNamePrefixHint}</p>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>{t.settings.templatesTitle}</CardTitle>
                <CardDescription>{t.settings.templatesDesc}</CardDescription>
              </CardHeader>
              <CardContent>
                {templatesLoading ? (
                  <p className="text-sm text-muted-foreground">{t.common.loading}</p>
                ) : !teamTemplates?.length ? (
                  <p className="text-sm text-muted-foreground">{t.settings.noTemplates}</p>
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
                            <Badge variant="secondary">{t.settings.membersUnit(tpl.members.length)}</Badge>
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
                          {t.settings.useTemplate}
                        </Button>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>{t.settings.membersTitle}</CardTitle>
                <CardDescription>
                  {t.settings.membersDesc}
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                {teamDefaultsLoading ? (
                  <p className="text-sm text-muted-foreground">{t.common.loading}</p>
                ) : members.length === 0 && !newMember ? (
                  <p className="text-sm text-muted-foreground">{t.settings.noMembers}</p>
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
                                  placeholder={t.settings.memberNamePlaceholder}
                                />
                                <Input
                                  value={editValues.role ?? ''}
                                  onChange={(e) =>
                                    setEditValues((v) => ({ ...v, role: e.target.value }))
                                  }
                                  placeholder={t.settings.memberRolePlaceholder}
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
                              {t.common.save}
                            </Button>
                            <Button size="sm" variant="outline" onClick={cancelEditing}>
                              {t.common.cancel}
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
                                    {member.enabled ? t.settings.memberEnabled : t.settings.memberDisabled}
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
                          placeholder={t.settings.memberNamePlaceholder}
                          autoFocus
                        />
                        <Input
                          value={newMember.role}
                          onChange={(e) =>
                            setNewMember((m) => m && { ...m, role: e.target.value })
                          }
                          placeholder={t.settings.memberRolePlaceholder}
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
                      {t.common.add}
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setNewMember(null)}>
                      {t.common.cancel}
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
                    {t.settings.addMember}
                  </Button>
                )}
              </CardContent>
            </Card>

            <div className="flex justify-end">
              <Button onClick={handleTeamConfigSave} disabled={updateDefaults.isPending}>
                <Save className="size-4" data-icon="inline-start" />
                {t.settings.saveTeamConfig}
              </Button>
            </div>
          </div>
        </TabsContent>

        {/* Tab 4: 关于 */}
        <TabsContent value={3}>
          <Card>
            <CardHeader>
              <CardTitle>{t.settings.aboutTitle}</CardTitle>
              <CardDescription>{t.settings.aboutDesc}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">{t.settings.version}</span>
                  <span className="text-sm text-muted-foreground">v0.2.0</span>
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">{t.settings.techStack}</span>
                  <span className="text-sm text-muted-foreground">LangGraph + FastAPI + React</span>
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">{t.settings.license}</span>
                  <span className="text-sm text-muted-foreground">MIT License</span>
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">{t.settings.python}</span>
                  <span className="text-sm text-muted-foreground">3.11+</span>
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">{t.settings.nodejs}</span>
                  <span className="text-sm text-muted-foreground">18+</span>
                </div>
              </div>

              <Separator />

              <div className="space-y-3">
                <h4 className="text-sm font-medium">{t.settings.coreDeps}</h4>
                <div className="grid grid-cols-2 gap-2 text-sm text-muted-foreground">
                  <span>{t.settings.depLangGraph}</span>
                  <span>{t.settings.depFastAPI}</span>
                  <span>{t.settings.depMem0}</span>
                  <span>{t.settings.depReact}</span>
                  <span>{t.settings.depDB}</span>
                  <span>{t.settings.depZustand}</span>
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
