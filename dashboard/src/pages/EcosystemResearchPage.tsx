import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowLeft,
  Search,
  Sparkles,
  CheckCircle2,
  Loader2,
  Send,
  Filter,
  Tag as TagIcon,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import {
  useEcosystemProfiles,
  useLifecycleRequestBatch,
  STAGE_STATUS_LABELS,
  stageBadgeClass,
} from '@/api/ecosystem';

/**
 * /ecosystem/research — 候选筛选页（v1.5.0-E §8.3）。
 *
 * 流程：
 *   1. 用户输入研究目标 + tag 关键词
 *   2. 系统按 tag 找候选（topic match）+ 浅扫 summary 展示
 *   3. 用户多选候选 → 触发 Stage 1 batch (deep_review_request_batch)
 *   4. 跳转/提示后续 finalists 由会议系统驱动
 */
export function EcosystemResearchPage() {
  const [researchGoal, setResearchGoal] = useState('');
  const [tagInput, setTagInput] = useState('');
  const [tags, setTags] = useState<string[]>([]);
  const [minStars, setMinStars] = useState(1000);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [batchResult, setBatchResult] = useState<{
    dispatched: number;
    repo_full_names: string[];
  } | null>(null);

  // 候选检索：用 tag 当作 topic 关键词（组合 OR）
  const filters = useMemo(
    () => ({
      keyword: tagInput.trim(),
      topic: tags[0] ?? '', // 后端只支持单 topic — 多 tag 用 keyword 兜底
      minStars,
      limit: 50,
      isActive: true as boolean | null,
    }),
    [tagInput, tags, minStars],
  );
  const { data, isLoading } = useEcosystemProfiles(filters);
  const candidates = useMemo(() => data?.profiles ?? [], [data]);

  // 用 tag 列表对候选做客户端二次过滤（topic ∩ tags）
  const filteredCandidates = useMemo(() => {
    if (tags.length === 0) return candidates;
    return candidates.filter((c) => {
      const repoTopics = c.topics?.map((t) => t.toLowerCase()) ?? [];
      const repoText = `${c.repo_full_name} ${c.description ?? ''} ${c.shallow_summary ?? ''}`.toLowerCase();
      return tags.every((t) => {
        const lower = t.toLowerCase();
        return repoTopics.includes(lower) || repoText.includes(lower);
      });
    });
  }, [candidates, tags]);

  const addTag = (t: string) => {
    const v = t.trim();
    if (!v || tags.includes(v)) return;
    setTags([...tags, v]);
    setTagInput('');
  };

  const removeTag = (t: string) => {
    setTags(tags.filter((x) => x !== t));
  };

  const toggleSelect = (id: string) => {
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelectedIds(next);
  };

  const batch = useLifecycleRequestBatch();

  const onDispatch = () => {
    if (selectedIds.size === 0) return;
    if (tags.length === 0) {
      alert('请至少添加一个 tag 用于 backend 候选筛选');
      return;
    }
    batch.mutate(
      {
        tags,
        min_stars: minStars,
        limit: selectedIds.size,
        research_goal: researchGoal,
      },
      {
        onSuccess: (resp) => {
          setBatchResult({
            dispatched: resp.dispatched,
            repo_full_names: resp.intents.map((i) => i.repo_full_name),
          });
          setSelectedIds(new Set());
        },
      },
    );
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-5xl mx-auto p-6 space-y-4">
        <div>
          <Button variant="ghost" size="sm" nativeButton={false} render={<Link to="/ecosystem" />}>
            <ArrowLeft className="mr-1 h-4 w-4" aria-hidden="true" />
            返回列表
          </Button>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Search className="h-5 w-5 text-primary" aria-hidden="true" />
              候选研究 — Stage 1 派遣
            </CardTitle>
            <p className="text-sm text-muted-foreground mt-1">
              输入研究目标 + tag 关键词，系统按活跃集筛选候选，多选后触发 Stage 1 架构分析。
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* 研究目标 */}
            <div className="space-y-1">
              <Label htmlFor="research-goal">研究目标</Label>
              <Input
                id="research-goal"
                placeholder="例：升级我们的记忆系统，对比当前生态主流方案"
                value={researchGoal}
                onChange={(e) => setResearchGoal(e.target.value)}
              />
            </div>

            {/* Tags */}
            <div className="space-y-1">
              <Label htmlFor="tag-input">候选 tags（回车添加）</Label>
              <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-background p-2">
                {tags.map((t) => (
                  <Badge
                    key={t}
                    variant="secondary"
                    className="gap-1 cursor-pointer"
                    onClick={() => removeTag(t)}
                    title="点击移除"
                  >
                    <TagIcon className="h-3 w-3" aria-hidden="true" />
                    {t} ×
                  </Badge>
                ))}
                <Input
                  id="tag-input"
                  className="flex-1 min-w-[120px] border-0 shadow-none focus-visible:ring-0 p-0 h-7"
                  placeholder={tags.length === 0 ? '例：memory / mcp / agent' : '继续添加…'}
                  value={tagInput}
                  onChange={(e) => setTagInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      addTag(tagInput);
                    }
                  }}
                />
              </div>
            </div>

            {/* min_stars */}
            <div className="space-y-1 max-w-xs">
              <Label htmlFor="min-stars">最少 stars</Label>
              <Input
                id="min-stars"
                type="number"
                min={0}
                step={500}
                value={minStars}
                onChange={(e) => setMinStars(Math.max(0, Number(e.target.value) || 0))}
              />
            </div>
          </CardContent>
        </Card>

        {/* 候选列表 */}
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between gap-3 flex-wrap">
              <CardTitle className="text-base flex items-center gap-2">
                <Filter className="h-4 w-4" aria-hidden="true" />
                候选列表
                <Badge variant="outline" className="font-normal">
                  {filteredCandidates.length}
                </Badge>
                {selectedIds.size > 0 && (
                  <Badge className="bg-primary/10 text-primary border-primary/30">
                    已选 {selectedIds.size}
                  </Badge>
                )}
              </CardTitle>
              <Button
                onClick={onDispatch}
                disabled={selectedIds.size === 0 || batch.isPending}
                size="sm"
              >
                {batch.isPending ? (
                  <>
                    <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden="true" />
                    派遣中…
                  </>
                ) : (
                  <>
                    <Send className="mr-1 h-4 w-4" aria-hidden="true" />
                    触发 Stage 1（{selectedIds.size}）
                  </>
                )}
              </Button>
            </div>
            {batchResult && (
              <div className="mt-2 rounded border border-emerald-500/40 bg-emerald-500/10 px-3 py-2">
                <div className="flex items-start gap-2">
                  <CheckCircle2 className="h-4 w-4 text-emerald-600 mt-0.5 shrink-0" aria-hidden="true" />
                  <div className="text-xs text-emerald-700 dark:text-emerald-300">
                    已派遣 {batchResult.dispatched} 个 deep_review intent，
                    Leader 需通过 Agent 工具实际派遣 backend-architect。
                    {batchResult.repo_full_names.length > 0 && (
                      <p className="mt-1">候选仓: {batchResult.repo_full_names.join(', ')}</p>
                    )}
                  </div>
                </div>
              </div>
            )}
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <div className="space-y-2">
                {Array.from({ length: 4 }).map((_, i) => (
                  <Skeleton key={i} className="h-20 w-full" />
                ))}
              </div>
            ) : filteredCandidates.length === 0 ? (
              <div className="py-8 text-center text-sm text-muted-foreground">
                <Sparkles className="h-8 w-8 mx-auto opacity-30 mb-2" aria-hidden="true" />
                {tags.length === 0
                  ? '请输入 tag 后开始检索候选'
                  : '当前 tag 组合下无活跃候选，可降低 min_stars 或切换 tag'}
              </div>
            ) : (
              <div className="space-y-2">
                {filteredCandidates.map((c) => {
                  const stage = c.stage_status ?? 'queued';
                  const checked = selectedIds.has(c.id);
                  return (
                    <button
                      type="button"
                      key={c.id}
                      onClick={() => toggleSelect(c.id)}
                      className={`w-full text-left rounded-lg border p-3 transition-colors ${
                        checked
                          ? 'border-primary/60 bg-primary/5'
                          : 'border-border bg-background hover:border-primary/30 hover:bg-accent/30'
                      }`}
                      aria-pressed={checked}
                    >
                      <div className="flex items-start gap-3">
                        <input
                          type="checkbox"
                          className="mt-1 h-4 w-4 accent-primary"
                          checked={checked}
                          onChange={() => toggleSelect(c.id)}
                          onClick={(e) => e.stopPropagation()}
                          aria-label={`选择 ${c.repo_full_name}`}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="font-medium text-sm">{c.repo_full_name}</span>
                            <span
                              className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium ${stageBadgeClass(stage)}`}
                            >
                              {STAGE_STATUS_LABELS[stage as keyof typeof STAGE_STATUS_LABELS] ?? stage}
                            </span>
                            <span className="text-xs text-muted-foreground">
                              ★ {c.stars.toLocaleString()}
                            </span>
                            {c.language && (
                              <span className="text-xs text-muted-foreground">{c.language}</span>
                            )}
                          </div>
                          <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
                            {c.shallow_summary || c.one_line_summary || c.description || '暂无描述'}
                          </p>
                          {c.topics && c.topics.length > 0 && (
                            <div className="flex flex-wrap gap-1 mt-1.5">
                              {c.topics.slice(0, 5).map((t) => (
                                <Badge key={t} variant="outline" className="text-[10px] px-1 py-0 h-4">
                                  {t}
                                </Badge>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
