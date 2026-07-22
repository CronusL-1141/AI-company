import { useState } from 'react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Input } from '@/components/ui/input';
import { useAvailableModels } from '@/api/models';
import { useT } from '@/i18n';

/**
 * 模型选择器 — 下拉清单来自文件真相源自动拉取（本机真实用过的模型），
 * 替换全部硬编码过时清单（claude-opus-4-7 等）。支持自由输入兜底
 * （新模型/未用过的模型），全名直显无别名映射。
 */
export function ModelSelect({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  const t = useT();
  const ph = placeholder ?? t.modelSelect.placeholder;
  const { data } = useAvailableModels();
  const models = (data?.data ?? []).filter((m) => !m.alias);
  const [custom, setCustom] = useState(false);
  const inList = models.some((m) => m.model === value);
  // 当前值不在扫描清单（如 settings.json 里的别名/新模型）时，作为附加项
  // 显示在下拉顶部——绝不因此退化成纯输入框（用户 2026-07-10 实测反馈）。
  const options = value && !inList
    ? [{ model: value, file_count: 0, last_seen_ts: 0, alias: false }, ...models]
    : models;

  if (custom || models.length === 0) {
    return (
      <div className="flex gap-2">
        <Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={ph}
          className="flex-1"
        />
        {models.length > 0 && (
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground whitespace-nowrap"
            onClick={() => setCustom(false)}
          >
            {t.modelSelect.pickFromList}
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="flex gap-2">
      <Select value={value} onValueChange={(v) => v && onChange(v)}>
        <SelectTrigger className="flex-1">
          <SelectValue placeholder={ph} />
        </SelectTrigger>
        <SelectContent>
          {options.map((m) => (
            <SelectItem key={m.model} value={m.model}>
              {m.model}
              {m.file_count > 0 && (
                <span className="ml-2 text-[10px] text-muted-foreground">
                  {t.modelSelect.usedInSessions(m.file_count)}
                </span>
              )}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <button
        type="button"
        className="text-xs text-muted-foreground hover:text-foreground whitespace-nowrap"
        onClick={() => setCustom(true)}
      >
        {t.modelSelect.freeInput}
      </button>
    </div>
  );
}
