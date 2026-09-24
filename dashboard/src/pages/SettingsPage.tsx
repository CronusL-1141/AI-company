import { useState } from 'react';
import { Save, ExternalLink, Users } from 'lucide-react';
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
import { useTeamTemplates } from '@/api/teamTemplates';
import { useContext } from 'react';
import { ModelSelect } from '@/components/shared/ModelSelect';
import { useAvailableModels, useDefaultModel, useSetDefaultModel } from '@/api/models';
import { LanguageContext, type LanguageMode, useT } from '@/i18n';
import { useApiVersion } from '@/api/health';

export function SettingsPage() {
  const version = useApiVersion();
  const { data: availData } = useAvailableModels();
  const { data: defaultData } = useDefaultModel();
  const setDefault = useSetDefaultModel();
  const availModels = (availData?.data ?? []).filter((m) => !m.alias);
  const currentDefault = defaultData?.data?.model ?? '';

  const t = useT();

  // 通用设置
  const [projectName, setProjectName] = useState('AI Team OS');
  const [projectDesc, setProjectDesc] = useState(t.settings.defaultProjectDesc);
  const [darkMode, setDarkMode] = useState(false);

  const langCtx = useContext(LanguageContext);
  const currentLang = langCtx?.lang ?? 'zh';
  const currentMode = langCtx?.mode ?? 'follow';
  const languageLabels: Record<LanguageMode, string> = {
    follow: t.settings.langFollow, zh: '中文', en: 'English',
  };
  const handleLangChange = (v: string | null) => {
    if (v && langCtx) langCtx.switchLang(v as LanguageMode);
  };

  // 基础设施设置
  const [storageBackend, setStorageBackend] = useState('sqlite');
  const [dbUrl, setDbUrl] = useState('sqlite:///data/aiteam.db');
  const [cacheBackend, setCacheBackend] = useState('memory');
  const [redisUrl, setRedisUrl] = useState('redis://localhost:6379');
  const [memoryBackend, setMemoryBackend] = useState('file');
  const [apiPort, setApiPort] = useState('8000');
  const [dashboardPort, setDashboardPort] = useState('5173');

  // 团队模板
  const { data: teamTemplates, isLoading: templatesLoading } = useTeamTemplates();

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
          {/* 模型治理：可用清单=文件真相源自动拉取（docs/model-governance-design.md） */}
          <Card className="mb-6">
            <CardHeader>
              <CardTitle>{t.settings.modelGovTitle}</CardTitle>
              <CardDescription>{t.settings.modelGovDesc}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-2">
                <Label>{t.settings.defaultModel}</Label>
                <div className="flex items-center gap-2">
                  <div className="flex-1">
                    <ModelSelect value={currentDefault} onChange={(v) => setDefault.mutate(v)} />
                  </div>
                  {currentDefault && (
                    <button
                      type="button"
                      className="whitespace-nowrap text-xs text-muted-foreground hover:text-destructive"
                      onClick={() => setDefault.mutate('')}
                    >
                      {t.settings.restoreCcDefault}
                    </button>
                  )}
                </div>
                {setDefault.isSuccess && (
                  <p className="text-xs text-green-600">{t.settings.modelSavedHint}</p>
                )}
              </div>
              <div className="grid gap-1">
                <Label className="text-muted-foreground">{t.settings.availableModels}</Label>
                <div className="divide-y rounded-md border">
                  {availModels.map((mm) => (
                    <div key={mm.model} className="flex items-center justify-between px-3 py-1.5 text-sm">
                      <span className="font-mono">{mm.model}</span>
                      <span className="text-xs text-muted-foreground">
                        {t.settings.modelSeenMeta(
                          mm.file_count,
                          new Date(mm.last_seen_ts * 1000).toLocaleDateString(
                            currentLang === 'zh' ? 'zh-CN' : 'en-US',
                          ),
                        )}
                      </span>
                    </div>
                  ))}
                  {availModels.length === 0 && (
                    <p className="px-3 py-2 text-xs text-muted-foreground">{t.settings.noRecords}</p>
                  )}
                </div>
              </div>
            </CardContent>
          </Card>
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
                <Label>{t.settings.interfaceLang}</Label>
                <Select value={currentMode} onValueChange={handleLangChange}
                  disabled={langCtx?.isLoading || langCtx?.isSaving}>
                  <SelectTrigger className="w-full">
                    {/* The closed trigger shows the item's label, not the stored value. */}
                    <SelectValue>{(value: string) => languageLabels[value as LanguageMode] ?? value}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="follow">{languageLabels.follow}</SelectItem>
                    <SelectItem value="zh">{languageLabels.zh}</SelectItem>
                    <SelectItem value="en">{languageLabels.en}</SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {langCtx?.isSaving ? t.settings.langSaving : t.settings.langPreferenceHint}
                </p>
                {langCtx?.error && (
                  <p role="alert" className="text-xs text-destructive">
                    {langCtx.error === 'save' ? t.settings.langSaveFailed : t.settings.langLoadFailed}
                  </p>
                )}
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
                <Button disabled title={t.settings.demoDisabledHint} onClick={handleSave}>
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
            <p role="note" className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
              {t.settings.infraExampleNotice}
            </p>
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
              <Button disabled title={t.settings.demoDisabledHint} onClick={handleSave}>
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
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
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
                  <span className="text-sm text-muted-foreground">
                    {version.isLoading ? t.common.loading : version.error
                      ? t.common.loadFailed(version.error.message)
                      : version.data?.version ? `v${version.data.version}` : t.settings.versionUnknown}
                  </span>
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
                  <span className="text-sm text-muted-foreground">3.12+</span>
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
