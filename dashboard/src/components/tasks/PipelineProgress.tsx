import { useState, useCallback } from 'react';
import { Progress } from '@/components/ui/progress';
import { ChevronDown } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { PipelineProgress as PipelineProgressType, PipelineStageStatus } from '@/types';

// ---------- stage icon helpers ----------

const STAGE_ICONS: Record<PipelineStageStatus, { icon: string; className: string }> = {
  completed: { icon: '✓', className: 'text-green-600 dark:text-green-400' },
  running:   { icon: '●', className: 'text-blue-500 dark:text-blue-400 animate-pulse' },
  pending:   { icon: '○', className: 'text-muted-foreground' },
  failed:    { icon: '✗', className: 'text-red-500 dark:text-red-400' },
};

function StageIcon({ status }: { status: PipelineStageStatus }) {
  const cfg = STAGE_ICONS[status] ?? STAGE_ICONS.pending;
  return (
    <span className={cn('shrink-0 w-4 text-center text-xs font-bold', cfg.className)}>
      {cfg.icon}
    </span>
  );
}

// ---------- main component ----------

interface PipelineProgressProps {
  progress: PipelineProgressType;
  /** Stop click event from bubbling to the card's onClick */
  stopPropagation?: boolean;
}

export function PipelineProgress({ progress, stopPropagation = true }: PipelineProgressProps) {
  const [expanded, setExpanded] = useState(false);

  const handleToggle = useCallback((e: React.MouseEvent) => {
    if (stopPropagation) e.stopPropagation();
    setExpanded((prev) => !prev);
  }, [stopPropagation]);

  // Progress percentage — use (current_index + 1) / total so running stage counts as partial
  const percent = progress.total_stages > 0
    ? Math.round(((progress.current_index) / progress.total_stages) * 100)
    : 0;

  const progressLabel = `${progress.current_stage} (${progress.current_index + 1}/${progress.total_stages})`;

  return (
    <div className="mt-2 space-y-1">
      {/* Clickable progress bar row */}
      <button
        type="button"
        aria-expanded={expanded}
        aria-label="Toggle pipeline stages"
        onClick={handleToggle}
        className="w-full text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 rounded"
      >
        <div className="flex items-center justify-between gap-2 mb-1">
          <span className="text-[11px] text-muted-foreground truncate leading-none">
            {progressLabel}
          </span>
          <ChevronDown
            className={cn('h-3 w-3 shrink-0 text-muted-foreground transition-transform', {
              'rotate-180': expanded,
            })}
          />
        </div>
        <Progress
          value={percent}
          indicatorClassName="bg-blue-500"
          className="h-1.5"
        />
      </button>

      {/* Expandable stage list */}
      {expanded && (
        <ul className="mt-2 space-y-0.5" role="list">
          {(progress.stages ?? []).map((stage, idx) => {
            const isRunning = stage.status === 'running';
            return (
              <li
                key={idx}
                className={cn(
                  'flex items-center gap-1.5 rounded px-1 py-0.5 text-[11px]',
                  isRunning && 'bg-blue-50 dark:bg-blue-950/30',
                )}
              >
                <StageIcon status={stage.status} />
                <span className={cn('truncate', isRunning && 'font-medium text-blue-700 dark:text-blue-300')}>
                  {stage.name}
                </span>
                {stage.agent_template && (
                  <span className="ml-auto shrink-0 text-muted-foreground opacity-70 hidden sm:inline">
                    {stage.agent_template}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
