import { useEffect, useState } from 'react';
import { Save, Settings2, Loader2, CheckCircle2, AlertCircle, Tag as TagIcon } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import {
  useEcosystemProjectSettings,
  useUpdateProjectSettings,
} from '@/api/ecosystem';

interface EcosystemSettingsPanelProps {
  projectId: string;
}

interface FormState {
  min_stars: number;
  top_n: number;
  refresh_interval_days: number;
  auto_shallow_on_archive: boolean;
  focus_topics: string[];
  focus_languages: string[];
  shallow_concurrency: number;
  deep_concurrency: number;
  alert_max_new_per_scan: number;
}

const DEFAULTS: FormState = {
  min_stars: 1000,
  top_n: 200,
  refresh_interval_days: 7,
  auto_shallow_on_archive: true,
  focus_topics: [],
  focus_languages: [],
  shallow_concurrency: 5,
  deep_concurrency: 3,
  alert_max_new_per_scan: 50,
};

/**
 * 项目级 ecosystem 配置面板（v1.5.0-E §8.4 / 决策 12.1）。
 * 嵌入到 ProjectDetailPage 作为 "Ecosystem 设置" tab。
 */
export function EcosystemSettingsPanel({ projectId }: EcosystemSettingsPanelProps) {
  const { data, isLoading, error } = useEcosystemProjectSettings(projectId);
  const update = useUpdateProjectSettings(projectId);

  const [form, setForm] = useState<FormState>(DEFAULTS);
  const [topicInput, setTopicInput] = useState('');
  const [languageInput, setLanguageInput] = useState('');
  const [savedAt, setSavedAt] = useState<number | null>(null);

  // 数据加载后填充表单（异步 data 到达后初始化可编辑表单，合法 effect-setState）
  useEffect(() => {
    if (data) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setForm({
        min_stars: data.min_stars,
        top_n: data.top_n,
        refresh_interval_days: data.refresh_interval_days,
        auto_shallow_on_archive: data.auto_shallow_on_archive,
        focus_topics: data.focus_topics ?? [],
        focus_languages: data.focus_languages ?? [],
        shallow_concurrency: data.shallow_concurrency,
        deep_concurrency: data.deep_concurrency,
        alert_max_new_per_scan: data.alert_max_new_per_scan ?? 50,
      });
    }
  }, [data]);

  const onSave = () => {
    update.mutate(form, {
      onSuccess: () => {
        setSavedAt(Date.now());
        setTimeout(() => setSavedAt(null), 3000);
      },
    });
  };

  const addTopic = () => {
    const v = topicInput.trim();
    if (!v || form.focus_topics.includes(v)) return;
    setForm({ ...form, focus_topics: [...form.focus_topics, v] });
    setTopicInput('');
  };

  const removeTopic = (t: string) => {
    setForm({ ...form, focus_topics: form.focus_topics.filter((x) => x !== t) });
  };

  const addLanguage = () => {
    const v = languageInput.trim();
    if (!v || form.focus_languages.includes(v)) return;
    setForm({ ...form, focus_languages: [...form.focus_languages, v] });
    setLanguageInput('');
  };

  const removeLanguage = (l: string) => {
    setForm({ ...form, focus_languages: form.focus_languages.filter((x) => x !== l) });
  };

  if (isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (error) {
    return (
      <Card>
        <CardContent className="p-4 flex items-start gap-2 text-destructive">
          <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" aria-hidden="true" />
          <div className="text-sm">
            <p className="font-medium">加载项目设置失败</p>
            <p className="text-xs mt-1 opacity-80">{error.message}</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Settings2 className="h-4 w-4" aria-hidden="true" />
          Ecosystem 设置
        </CardTitle>
        <p className="text-xs text-muted-foreground mt-1">
          项目级生态仓配置：阈值、活跃集大小、刷新周期、关注 topic/语言。修改后影响该项目的扫描/浅扫行为。
        </p>
      </CardHeader>
      <CardContent className="space-y-5">
        {/* 数值字段网格 */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-1">
            <Label htmlFor="min-stars">入档 stars 阈值</Label>
            <Input
              id="min-stars"
              type="number"
              min={0}
              step={500}
              value={form.min_stars}
              onChange={(e) =>
                setForm({ ...form, min_stars: Math.max(0, Number(e.target.value) || 0) })
              }
            />
            <p className="text-[10px] text-muted-foreground">stars 低于此值的仓不入档</p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="top-n">活跃集大小 (top_n)</Label>
            <Input
              id="top-n"
              type="number"
              min={1}
              max={1000}
              step={10}
              value={form.top_n}
              onChange={(e) =>
                setForm({ ...form, top_n: Math.max(1, Number(e.target.value) || 1) })
              }
            />
            <p className="text-[10px] text-muted-foreground">按 stars 排序前 N 个进入活跃集</p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="refresh-interval">自动检测周期（天）</Label>
            <Input
              id="refresh-interval"
              type="number"
              min={1}
              max={90}
              step={1}
              value={form.refresh_interval_days}
              onChange={(e) =>
                setForm({
                  ...form,
                  refresh_interval_days: Math.max(1, Number(e.target.value) || 1),
                })
              }
            />
            <p className="text-[10px] text-muted-foreground">
              自动检测周期（每 N 天 cron 触发新批次扫描，检测新增库 + 已入档库的更新）
            </p>
            <p className="text-[10px] text-muted-foreground/70 mt-0.5">
              提示：这是 cron 调度周期，不是过期阈值。老库重扫由 GitHub repo 的 push 时间决定（pushed_at &gt; last_shallow_refreshed_at）
            </p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="shallow-conc">浅扫并发</Label>
            <Input
              id="shallow-conc"
              type="number"
              min={1}
              max={20}
              value={form.shallow_concurrency}
              onChange={(e) =>
                setForm({
                  ...form,
                  shallow_concurrency: Math.max(1, Number(e.target.value) || 1),
                })
              }
            />
            <p className="text-[10px] text-muted-foreground">Stage 0 worker 并发数（rate limit 调整）</p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="deep-conc">深扫并发</Label>
            <Input
              id="deep-conc"
              type="number"
              min={1}
              max={10}
              value={form.deep_concurrency}
              onChange={(e) =>
                setForm({
                  ...form,
                  deep_concurrency: Math.max(1, Number(e.target.value) || 1),
                })
              }
            />
            <p className="text-[10px] text-muted-foreground">Stage 1+ batch 派遣并发数</p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="alert-max-new">告警阈值：单次新增上限</Label>
            <Input
              id="alert-max-new"
              type="number"
              min={1}
              max={10000}
              step={10}
              value={form.alert_max_new_per_scan}
              onChange={(e) =>
                setForm({
                  ...form,
                  alert_max_new_per_scan: Math.max(1, Number(e.target.value) || 1),
                })
              }
            />
            <p className="text-[10px] text-muted-foreground">单次扫描新增仓超过此数时触发告警（不提交结果）</p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="auto-shallow">入档自动浅扫</Label>
            <div className="flex items-center gap-2 pt-2">
              <Switch
                id="auto-shallow"
                checked={form.auto_shallow_on_archive}
                onCheckedChange={(checked) =>
                  setForm({ ...form, auto_shallow_on_archive: checked })
                }
              />
              <span className="text-xs text-muted-foreground">
                {form.auto_shallow_on_archive ? '自动派遣 Stage 0' : '关闭，仅按需触发'}
              </span>
            </div>
          </div>
        </div>

        {/* focus_topics */}
        <div className="space-y-1">
          <Label htmlFor="topic-input">关注 topics（白名单，空=全 topic）</Label>
          <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-background p-2">
            {form.focus_topics.map((t) => (
              <Badge
                key={t}
                variant="secondary"
                className="gap-1 cursor-pointer"
                onClick={() => removeTopic(t)}
                title="点击移除"
              >
                <TagIcon className="h-3 w-3" aria-hidden="true" />
                {t} ×
              </Badge>
            ))}
            <Input
              id="topic-input"
              className="flex-1 min-w-[120px] border-0 shadow-none focus-visible:ring-0 p-0 h-7"
              placeholder={form.focus_topics.length === 0 ? '例：claude-code / mcp / agent-framework' : '继续添加…'}
              value={topicInput}
              onChange={(e) => setTopicInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  addTopic();
                }
              }}
            />
          </div>
        </div>

        {/* focus_languages */}
        <div className="space-y-1">
          <Label htmlFor="language-input">关注语言（白名单，空=全语言）</Label>
          <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-background p-2">
            {form.focus_languages.map((l) => (
              <Badge
                key={l}
                variant="secondary"
                className="gap-1 cursor-pointer"
                onClick={() => removeLanguage(l)}
                title="点击移除"
              >
                <TagIcon className="h-3 w-3" aria-hidden="true" />
                {l} ×
              </Badge>
            ))}
            <Input
              id="language-input"
              className="flex-1 min-w-[120px] border-0 shadow-none focus-visible:ring-0 p-0 h-7"
              placeholder={form.focus_languages.length === 0 ? '例：Python / TypeScript / Rust' : '继续添加…'}
              value={languageInput}
              onChange={(e) => setLanguageInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  addLanguage();
                }
              }}
            />
          </div>
        </div>

        {/* 保存条 */}
        <div className="flex items-center justify-between pt-2 border-t">
          <div className="text-xs text-muted-foreground">
            {data?.updated_at && (
              <>最近更新: {new Date(data.updated_at).toLocaleString('zh-CN')}</>
            )}
          </div>
          <div className="flex items-center gap-2">
            {savedAt && (
              <span className="inline-flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400">
                <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                已保存
              </span>
            )}
            <Button onClick={onSave} disabled={update.isPending}>
              {update.isPending ? (
                <>
                  <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden="true" />
                  保存中…
                </>
              ) : (
                <>
                  <Save className="mr-1 h-4 w-4" aria-hidden="true" />
                  保存设置
                </>
              )}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
