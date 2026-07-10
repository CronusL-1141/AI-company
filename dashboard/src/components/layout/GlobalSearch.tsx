import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Search, FileText, ClipboardList, StickyNote } from 'lucide-react';
import { apiFetch } from '@/api/client';

interface SearchHit {
  kind: 'task' | 'task_memo' | 'report' | (string & {});
  id: string;
  title: string;
  snippet: string;
  score: number;
  project_id: string;
}

const KIND_ICON: Record<string, typeof FileText> = {
  task: ClipboardList,
  task_memo: StickyNote,
  report: FileText,
};

/** 知识层 P1b — 全局统一检索框（三臂 RRF：BM25 中文原生 / 引用图谱 / 精确 ID）。 */
export function GlobalSearch() {
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const boxRef = useRef<HTMLDivElement>(null);

  // 防抖 300ms 查询
  useEffect(() => {
    if (!q.trim()) {
      setHits([]);
      setOpen(false);
      return;
    }
    const timer = setTimeout(async () => {
      setLoading(true);
      try {
        const res = await apiFetch<{ data: SearchHit[] }>(
          `/api/search?q=${encodeURIComponent(q.trim())}&limit=8`,
        );
        setHits(res.data ?? []);
        setOpen(true);
      } catch {
        setHits([]);
      } finally {
        setLoading(false);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [q]);

  // 点击外部关闭
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, []);

  const jump = (h: SearchHit) => {
    setOpen(false);
    setQ('');
    if (h.kind === 'report') navigate('/reports');
    else if (h.kind === 'task' || h.kind === 'task_memo') navigate('/tasks');
    else navigate('/');
  };

  return (
    <div ref={boxRef} className="relative ml-auto w-64 max-w-[40vw]">
      <div className="flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-sm bg-background">
        <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onFocus={() => hits.length > 0 && setOpen(true)}
          placeholder="搜索 memo / 报告 / 任务 / wf_id / commit…"
          className="w-full bg-transparent outline-none placeholder:text-muted-foreground/60"
        />
        {loading && <span className="text-[10px] text-muted-foreground shrink-0">…</span>}
      </div>
      {open && hits.length > 0 && (
        <div className="absolute right-0 top-full z-50 mt-1 w-[26rem] max-w-[80vw] overflow-hidden rounded-lg border bg-popover shadow-lg">
          {hits.map((h) => {
            const Icon = KIND_ICON[h.kind] ?? FileText;
            return (
              <button
                key={`${h.kind}:${h.id}`}
                onClick={() => jump(h)}
                className="flex w-full items-start gap-2.5 border-b px-3 py-2.5 text-left last:border-0 hover:bg-accent/60"
              >
                <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium">{h.title}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {h.snippet}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      )}
      {open && hits.length === 0 && !loading && q.trim() && (
        <div className="absolute right-0 top-full z-50 mt-1 w-64 rounded-lg border bg-popover px-3 py-2.5 text-sm text-muted-foreground shadow-lg">
          无匹配结果
        </div>
      )}
    </div>
  );
}
