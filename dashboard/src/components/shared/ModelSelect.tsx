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

/**
 * 模型选择器 — 下拉清单来自文件真相源自动拉取（本机真实用过的模型），
 * 替换全部硬编码过时清单（claude-opus-4-7 等）。支持自由输入兜底
 * （新模型/未用过的模型），全名直显无别名映射。
 */
export function ModelSelect({
  value,
  onChange,
  placeholder = '选择或输入模型 ID',
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  const { data } = useAvailableModels();
  const models = (data?.data ?? []).filter((m) => !m.alias);
  const [custom, setCustom] = useState(false);
  const inList = models.some((m) => m.model === value);

  if (custom || (value && !inList && models.length > 0)) {
    return (
      <div className="flex gap-2">
        <Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className="flex-1"
        />
        {models.length > 0 && (
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground whitespace-nowrap"
            onClick={() => setCustom(false)}
          >
            选清单
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="flex gap-2">
      <Select value={value} onValueChange={(v) => v && onChange(v)}>
        <SelectTrigger className="flex-1">
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent>
          {models.map((m) => (
            <SelectItem key={m.model} value={m.model}>
              {m.model}
              <span className="ml-2 text-[10px] text-muted-foreground">
                {m.file_count} 会话用过
              </span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <button
        type="button"
        className="text-xs text-muted-foreground hover:text-foreground whitespace-nowrap"
        onClick={() => setCustom(true)}
      >
        自由输入
      </button>
    </div>
  );
}
