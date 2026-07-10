import { useMemo, useRef, useState, useEffect } from 'react';
import { Search, Star, Tag, X, ChevronDown, Check } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from '@/components/ui/select';
import type { EcosystemFilters, EcosystemFacetCounts } from '@/api/ecosystem';

interface FilterBarProps {
  /** 当前筛选条件 */
  filters: EcosystemFilters;
  /** 筛选条件变更回调 */
  onChange: (next: EcosystemFilters) => void;
  /** 命中数量（用于展示） */
  totalCount?: number;
  /** 后端 facet 聚合（启用时类别筛选项后跟数量） */
  facetCounts?: EcosystemFacetCounts;
}

const STAR_OPTIONS: { value: number; label: string }[] = [
  { value: 0, label: '不限星标' },
  { value: 100, label: '≥ 100' },
  { value: 1000, label: '≥ 1k' },
  { value: 5000, label: '≥ 5k' },
  { value: 15000, label: '≥ 15k' },
  { value: 50000, label: '≥ 50k' },
];

const ALL = '__all__';

/**
 * 列表页筛选栏 — 关键词搜索 + Topics 多选 + 星标阈值 + 深扫状态。
 * v1.6.0：删除"类别"单选（启发式分类废弃），改为 GitHub topics 多选筛选（客户端 filter）。
 * 移动端单列堆叠，桌面端横向铺开。
 */
export function FilterBar({ filters, onChange, totalCount, facetCounts }: FilterBarProps) {
  const topicFacets = useMemo(() => facetCounts?.topics ?? {}, [facetCounts]);
  const update = (patch: Partial<EcosystemFilters>) => {
    onChange({ ...filters, ...patch });
  };

  const resetAll = () => {
    onChange({ limit: filters.limit ?? 200 });
  };

  // v1.6.0：默认全集（含已删除，因数量极少），勾选则切换为"仅已删除"视图
  const onlyDeleted = filters.isDeleted === true;
  const selectedTopics = filters.topics ?? [];

  const hasActiveFilter = Boolean(
    filters.keyword ||
      filters.topic ||
      selectedTopics.length > 0 ||
      (filters.minStars && filters.minStars > 0) ||
      filters.stageStatus ||
      onlyDeleted,
  );

  // 按数量降序排序 topics，取 top 50（避免 2425 个全部塞进 dropdown）
  const sortedTopics = useMemo(() => {
    return Object.entries(topicFacets)
      .sort(([, a], [, b]) => b - a)
      .slice(0, 50);
  }, [topicFacets]);

  // 星标 trigger 显示文本
  const minStarsValue = filters.minStars ?? 0;
  const starLabel = STAR_OPTIONS.find((o) => o.value === minStarsValue)?.label ?? '不限星标';

  // 研究阶段 trigger 显示文本（v1.5.1：3 类互斥，语义对齐 StatsBar）
  const STAGE_LABELS: Record<string, string> = {
    queued: '待浅扫',
    shallow_done: '已浅扫未研究',
    'architecture_done,debated,referenced,integrated': '已被研究',
  };
  const stageLabel = filters.stageStatus
    ? (STAGE_LABELS[filters.stageStatus] ?? filters.stageStatus)
    : '全部仓';

  return (
    <div className="flex flex-col gap-3 p-4 border-b bg-muted/20">
      {/* 第一行：搜索框 + 命中计数 */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1">
          <Search
            className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            placeholder="搜索仓库名 / owner / 描述..."
            value={filters.keyword ?? ''}
            onChange={(e) => update({ keyword: e.target.value })}
            className="pl-9 h-9"
            aria-label="搜索仓库"
          />
        </div>
        {typeof totalCount === 'number' && (
          <div className="text-sm text-muted-foreground whitespace-nowrap">
            共 <span className="font-semibold text-foreground">{totalCount}</span> 个仓库
          </div>
        )}
      </div>

      {/* 第二行：维度筛选 */}
      <div className="flex flex-wrap items-center gap-2">
        {/* v1.6.0: Topics 多选筛选（替换原 relevance_category 单选） */}
        <TopicsMultiSelect
          options={sortedTopics}
          selected={selectedTopics}
          onChange={(next) => update({ topics: next.length > 0 ? next : undefined })}
        />

        {/* 星标阈值 */}
        <Select
          value={String(minStarsValue)}
          onValueChange={(v) => update({ minStars: Number(v) })}
        >
          <SelectTrigger className="h-8 min-w-[140px] text-sm" aria-label="星标阈值">
            <Star className="mr-1.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span className="truncate">{starLabel}</span>
          </SelectTrigger>
          <SelectContent>
            {STAR_OPTIONS.map((opt) => (
              <SelectItem key={opt.value} value={String(opt.value)}>
                {opt.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {/* 研究阶段（v1.5.0 漏斗，v1.5.1 加语义说明）*/}
        <Select
          value={filters.stageStatus || ALL}
          onValueChange={(v) =>
            update({
              stageStatus: !v || v === ALL ? '' : v,
            })
          }
        >
          <SelectTrigger
            className="h-8 min-w-[170px] text-sm"
            aria-label="研究阶段"
            title="浅扫=读 README/CHANGELOG 摘要功能与方向；研究=按需调研代码结构与设计，含相关性与采纳记录"
          >
            <span className="truncate">{stageLabel}</span>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>全部仓</SelectItem>
            <SelectItem value="queued">
              <span className="flex flex-col">
                <span>待浅扫</span>
                <span className="text-[10px] text-muted-foreground">尚未读 README/CHANGELOG</span>
              </span>
            </SelectItem>
            <SelectItem value="shallow_done">
              <span className="flex flex-col">
                <span>已浅扫未研究</span>
                <span className="text-[10px] text-muted-foreground">已摘要功能/设计方向</span>
              </span>
            </SelectItem>
            <SelectItem value="architecture_done,debated,referenced,integrated">
              <span className="flex flex-col">
                <span>已被研究</span>
                <span className="text-[10px] text-muted-foreground">为系统改动做过调研</span>
              </span>
            </SelectItem>
          </SelectContent>
        </Select>

        {/* v1.6.0：替代被删除的"已删除" tab — 切换"仅看已删除"视图 */}
        <label
          className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer select-none h-8 px-2 rounded-md hover:bg-muted/40"
          title="勾选后仅显示已被检测为删除/转为私有的仓（默认视图包含全部仓）"
        >
          <input
            type="checkbox"
            checked={onlyDeleted}
            onChange={(e) =>
              // 勾选 → isDeleted=true（仅已删除）；取消 → undefined（全集，默认）
              update({ isDeleted: e.target.checked ? true : undefined })
            }
            className="h-3.5 w-3.5 rounded border-border accent-primary"
            aria-label="仅看已删除仓"
          />
          仅看已删除
        </label>

        {/* TODO(Stage E v2): 增加 has_deep_review / is_archived / tags 多选筛选 */}

        {hasActiveFilter && (
          <Button
            variant="ghost"
            size="sm"
            onClick={resetAll}
            className="h-8 ml-auto text-muted-foreground"
            aria-label="清除所有筛选"
          >
            <X className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
            清除
          </Button>
        )}
      </div>
    </div>
  );
}

/**
 * Topics 多选下拉 — 显示带数量的 topic 列表，支持搜索过滤。
 * v1.6.0: 替代原 relevance_category 单选；用 facet_counts.topics 作为选项数据源。
 */
function TopicsMultiSelect({
  options,
  selected,
  onChange,
}: {
  options: [string, number][];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const rootRef = useRef<HTMLDivElement>(null);

  // 点击外部关闭
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('mousedown', onClick);
    return () => window.removeEventListener('mousedown', onClick);
  }, [open]);

  const filtered = useMemo(() => {
    if (!query.trim()) return options;
    const q = query.toLowerCase();
    return options.filter(([name]) => name.toLowerCase().includes(q));
  }, [options, query]);

  const toggle = (topic: string) => {
    const next = selected.includes(topic)
      ? selected.filter((t) => t !== topic)
      : [...selected, topic];
    onChange(next);
  };

  const triggerLabel =
    selected.length === 0
      ? '全部 Topics'
      : selected.length === 1
        ? selected[0]
        : `${selected.length} 个 Topics`;

  return (
    <div ref={rootRef} className="relative">
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="h-8 min-w-[170px] justify-between text-sm font-normal"
        onClick={() => setOpen((v) => !v)}
        aria-label="筛选 Topics（多选）"
        aria-expanded={open}
      >
        <span className="flex items-center gap-1.5 truncate">
          <Tag className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span className="truncate">{triggerLabel}</span>
        </span>
        <ChevronDown className="h-3.5 w-3.5 shrink-0 opacity-60" aria-hidden="true" />
      </Button>
      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 w-72 rounded-md border bg-popover shadow-md">
          <div className="p-2 border-b">
            <Input
              autoFocus
              placeholder="搜索 topic..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="h-7 text-xs"
            />
            {selected.length > 0 && (
              <div className="flex items-center justify-between mt-1.5">
                <span className="text-[10px] text-muted-foreground">
                  已选 {selected.length} 个
                </span>
                <button
                  type="button"
                  className="text-[10px] text-primary hover:underline"
                  onClick={() => onChange([])}
                >
                  清空
                </button>
              </div>
            )}
          </div>
          <div className="max-h-72 overflow-y-auto py-1">
            {filtered.length === 0 ? (
              <p className="text-xs text-muted-foreground text-center py-3">无匹配 topic</p>
            ) : (
              filtered.map(([topic, count]) => {
                const isSelected = selected.includes(topic);
                return (
                  <button
                    key={topic}
                    type="button"
                    onClick={() => toggle(topic)}
                    className={`w-full flex items-center justify-between gap-2 px-2.5 py-1.5 text-xs text-left hover:bg-accent ${
                      isSelected ? 'bg-accent/50' : ''
                    }`}
                  >
                    <span className="flex items-center gap-1.5 min-w-0">
                      <span
                        className={`inline-flex h-3.5 w-3.5 items-center justify-center rounded border ${
                          isSelected
                            ? 'bg-primary border-primary text-primary-foreground'
                            : 'border-border'
                        }`}
                      >
                        {isSelected && <Check className="h-2.5 w-2.5" aria-hidden="true" />}
                      </span>
                      <span className="truncate">{topic}</span>
                    </span>
                    <span className="text-[10px] text-muted-foreground shrink-0">{count}</span>
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}
