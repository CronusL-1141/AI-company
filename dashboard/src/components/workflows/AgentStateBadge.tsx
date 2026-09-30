import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { useT } from '@/i18n';

// workflow_ingest stores the agent state verbatim from the Workflow JSON
// (workflowProgress[].state), and the live tail writes running/done from the
// journal, so every value needs a label here or the badge prints it raw. In stored
// runs: start = launched, no tokens yet; running = started, not finished; progress =
// reporting tokens, not finished; done = finished with a result; error = ended
// without a result, i.e. failed. queued is the pre-start value; failed is treated
// like error.
const TONE = {
  queued: 'border-slate-400 text-slate-600 dark:text-slate-300',
  start: 'border-slate-400 text-slate-600 dark:text-slate-300',
  running: 'border-blue-400 text-blue-700 dark:text-blue-300',
  progress: 'border-blue-400 text-blue-700 dark:text-blue-300',
  done: 'border-green-400 text-green-700 dark:text-green-300',
  error: 'border-red-400 text-red-700 dark:text-red-300',
  failed: 'border-red-400 text-red-700 dark:text-red-300',
} as const;

type KnownAgentState = keyof typeof TONE;

export function AgentStateBadge({ state }: { state: string }) {
  const t = useT();
  const labels: Record<KnownAgentState, string> = t.workflows.agentState;
  const known = Object.hasOwn(TONE, state) ? (state as KnownAgentState) : undefined;
  return (
    <Badge variant="outline" className={cn('text-[10px]', TONE[known ?? 'queued'])}>
      {known ? labels[known] : state}
    </Badge>
  );
}
