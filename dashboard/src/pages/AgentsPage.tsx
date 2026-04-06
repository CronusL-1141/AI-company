import { useState, useMemo } from 'react';
import { Input } from '@/components/ui/input';
import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Bot, Search, AlertCircle, CheckCircle2 } from 'lucide-react';
import { useAgentTemplates, useUpdateAgentTemplate } from '@/api/agentsConfig';
import type { AgentTemplate } from '@/api/agentsConfig';
import { useT } from '@/i18n';

const COLOR_OPTIONS = [
  { value: 'purple', label: 'Purple', cls: 'bg-purple-500' },
  { value: 'blue', label: 'Blue', cls: 'bg-blue-500' },
  { value: 'green', label: 'Green', cls: 'bg-green-500' },
  { value: 'yellow', label: 'Yellow', cls: 'bg-yellow-500' },
  { value: 'red', label: 'Red', cls: 'bg-red-500' },
  { value: 'orange', label: 'Orange', cls: 'bg-orange-500' },
  { value: 'pink', label: 'Pink', cls: 'bg-pink-500' },
  { value: 'cyan', label: 'Cyan', cls: 'bg-cyan-500' },
  { value: 'gray', label: 'Gray', cls: 'bg-gray-500' },
];

const MODEL_OPTIONS = [
  { value: 'opus', labelKey: 'modelOpus' as const },
  { value: 'sonnet', labelKey: 'modelSonnet' as const },
  { value: 'haiku', labelKey: 'modelHaiku' as const },
];

function getGroupKey(filename: string): string {
  const prefix = filename.split('-')[0];
  const knownGroups: Record<string, string> = {
    engineering: 'engineering',
    management: 'management',
    testing: 'testing',
    support: 'support',
    specialized: 'specialized',
    meeting: 'support',
    team: 'management',
    tech: 'management',
  };
  return knownGroups[prefix] ?? 'other';
}

function getColorClass(color: string): string {
  return COLOR_OPTIONS.find((c) => c.value === color)?.cls ?? 'bg-gray-400';
}

// Left panel: single agent card
function AgentCard({
  agent,
  selected,
  onClick,
}: {
  agent: AgentTemplate;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full text-left rounded-lg border p-3 transition-colors hover:bg-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
        selected ? 'border-primary bg-accent' : 'border-border'
      }`}
      aria-pressed={selected}
      aria-label={agent.name}
    >
      <div className="flex items-start gap-2">
        <span
          className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${getColorClass(agent.color)}`}
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium text-sm leading-snug">{agent.name}</p>
          {agent.description && (
            <p className="mt-0.5 line-clamp-2 text-[11px] text-muted-foreground leading-snug">
              {agent.description}
            </p>
          )}
          <div className="mt-1 flex flex-wrap gap-1">
            {agent.model && (
              <Badge variant="secondary" className="text-[10px] px-1.5 py-0">
                {agent.model}
              </Badge>
            )}
          </div>
        </div>
      </div>
    </button>
  );
}

// Right panel: edit form
function AgentEditPanel({ agent }: { agent: AgentTemplate }) {
  const t = useT();
  const { mutateAsync, isPending } = useUpdateAgentTemplate();

  const [description, setDescription] = useState(agent.description);
  const [model, setModel] = useState(agent.model);
  const [color, setColor] = useState(agent.color);
  const [prompt, setPrompt] = useState(agent.prompt);
  const [saveStatus, setSaveStatus] = useState<'idle' | 'success' | 'error'>('idle');
  const [errorMsg, setErrorMsg] = useState('');

  // Reset fields when switching agents
  const agentKey = agent.filename;

  // Use key prop on the parent in the parent component to force remount on agent change

  async function handleSave() {
    setSaveStatus('idle');
    setErrorMsg('');
    try {
      await mutateAsync({
        filename: agent.filename,
        data: { name: agent.name, description, model, color, prompt },
      });
      setSaveStatus('success');
      setTimeout(() => setSaveStatus('idle'), 2000);
    } catch (err) {
      setSaveStatus('error');
      setErrorMsg(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div key={agentKey} className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="border-b px-4 py-3 shrink-0 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span
            className={`h-3 w-3 rounded-full ${getColorClass(color)}`}
            aria-hidden="true"
          />
          <h2 className="font-semibold text-base">{agent.name}</h2>
        </div>
        <div className="flex items-center gap-2">
          {saveStatus === 'success' && (
            <span className="flex items-center gap-1 text-sm text-green-600">
              <CheckCircle2 className="h-4 w-4" />
              {t.agents.saveSuccess}
            </span>
          )}
          {saveStatus === 'error' && (
            <span className="flex items-center gap-1 text-sm text-destructive">
              <AlertCircle className="h-4 w-4" />
              {t.agents.saveFailed(errorMsg)}
            </span>
          )}
          <Button
            onClick={handleSave}
            disabled={isPending}
            size="sm"
            aria-label={t.agents.save}
          >
            {isPending ? t.agents.saving : t.agents.save}
          </Button>
        </div>
      </div>

      {/* Form */}
      <div className="flex-1 overflow-auto p-4 space-y-4">
        {/* Name (read-only) */}
        <div className="space-y-1.5">
          <Label className="text-sm font-medium">{t.agents.fieldName}</Label>
          <Input value={agent.name} readOnly className="bg-muted text-muted-foreground" />
        </div>

        {/* Description — single-line to avoid newlines breaking YAML frontmatter */}
        <div className="space-y-1.5">
          <Label htmlFor="agent-description" className="text-sm font-medium">
            {t.agents.fieldDescription}
          </Label>
          <Input
            id="agent-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t.agents.descPlaceholder}
          />
        </div>

        {/* Model + Color row */}
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <Label htmlFor="agent-model" className="text-sm font-medium">
              {t.agents.fieldModel}
            </Label>
            <Select value={model} onValueChange={(v) => setModel(v ?? '')}>
              <SelectTrigger id="agent-model" className="h-9 text-sm">
                <SelectValue placeholder={t.agents.fieldModel} />
              </SelectTrigger>
              <SelectContent>
                {MODEL_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    {t.agents[opt.labelKey]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="agent-color" className="text-sm font-medium">
              {t.agents.fieldColor}
            </Label>
            <Select value={color} onValueChange={(v) => setColor(v ?? '')}>
              <SelectTrigger id="agent-color" className="h-9 text-sm">
                <SelectValue placeholder={t.agents.fieldColor} />
              </SelectTrigger>
              <SelectContent>
                {COLOR_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    <div className="flex items-center gap-2">
                      <span className={`h-3 w-3 rounded-full ${opt.cls}`} aria-hidden="true" />
                      {opt.label}
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        {/* Prompt */}
        <div className="space-y-1.5">
          <Label htmlFor="agent-prompt" className="text-sm font-medium">
            {t.agents.fieldPrompt}
          </Label>
          <Textarea
            id="agent-prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder={t.agents.promptPlaceholder}
            rows={18}
            className="font-mono text-sm resize-y"
          />
        </div>
      </div>
    </div>
  );
}

// Empty state for right panel
function EmptyEditPanel() {
  const t = useT();
  return (
    <div className="flex h-full items-center justify-center text-muted-foreground">
      <div className="text-center space-y-2">
        <Bot className="mx-auto h-10 w-10 opacity-30" />
        <p className="text-sm">{t.agents.selectAgent}</p>
      </div>
    </div>
  );
}

// Group label mapping
function groupLabel(key: string, t: ReturnType<typeof useT>): string {
  const map: Record<string, string> = {
    engineering: t.agents.groupEngineering,
    management: t.agents.groupManagement,
    testing: t.agents.groupTesting,
    support: t.agents.groupSupport,
    specialized: t.agents.groupSpecialized,
    other: t.agents.groupOther,
  };
  return map[key] ?? key;
}

export function AgentsPage() {
  const t = useT();
  const { data: agents = [], isLoading, error } = useAgentTemplates();
  const [search, setSearch] = useState('');
  const [selectedFilename, setSelectedFilename] = useState<string | null>(null);

  const selectedAgent = useMemo(
    () => agents.find((a) => a.filename === selectedFilename) ?? null,
    [agents, selectedFilename],
  );

  // Filter and group
  const grouped = useMemo(() => {
    const q = search.trim().toLowerCase();
    const filtered = q
      ? agents.filter(
          (a) =>
            a.name.toLowerCase().includes(q) ||
            a.description.toLowerCase().includes(q),
        )
      : agents;

    const groups: Record<string, AgentTemplate[]> = {};
    for (const agent of filtered) {
      const key = getGroupKey(agent.filename);
      (groups[key] ??= []).push(agent);
    }
    return groups;
  }, [agents, search]);

  const groupOrder = ['engineering', 'management', 'testing', 'support', 'specialized', 'other'];

  return (
    <div className="flex h-full flex-col gap-0">
      {/* Page header */}
      <div className="border-b px-6 py-4">
        <h1 className="text-xl font-semibold">{t.agents.title}</h1>
      </div>

      {/* Two-panel layout */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: agent list */}
        <aside className="flex w-72 shrink-0 flex-col border-r">
          {/* Search */}
          <div className="border-b p-3">
            <div className="relative">
              <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
              <Input
                placeholder={t.agents.searchPlaceholder}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-8 h-8 text-sm"
                aria-label={t.agents.searchPlaceholder}
              />
            </div>
          </div>

          {/* List */}
          <div className="flex-1 overflow-y-auto p-2 space-y-3">
            {isLoading && (
              <>
                <Skeleton className="h-16 w-full rounded-lg" />
                <Skeleton className="h-16 w-full rounded-lg" />
                <Skeleton className="h-16 w-full rounded-lg" />
              </>
            )}
            {error && (
              <div className="flex items-center gap-2 p-3 text-destructive text-sm">
                <AlertCircle className="h-4 w-4 shrink-0" />
                <span>{t.agents.loadFailed(error.message)}</span>
              </div>
            )}
            {!isLoading && !error && agents.length === 0 && (
              <div className="p-4 text-center text-sm text-muted-foreground space-y-1">
                <p>{t.agents.noAgents}</p>
                <p className="text-xs">{t.agents.noAgentsHint}</p>
              </div>
            )}
            {groupOrder.map((key) => {
              const items = grouped[key];
              if (!items || items.length === 0) return null;
              return (
                <div key={key}>
                  <p className="mb-1 px-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    {groupLabel(key, t)}
                  </p>
                  <div className="space-y-1">
                    {items.map((agent) => (
                      <AgentCard
                        key={agent.filename}
                        agent={agent}
                        selected={selectedFilename === agent.filename}
                        onClick={() => setSelectedFilename(agent.filename)}
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </aside>

        {/* Right: edit panel */}
        <main className="flex-1 overflow-hidden">
          <Card className="h-full rounded-none border-0 shadow-none">
            <CardContent className="h-full p-0">
              {selectedAgent ? (
                <AgentEditPanel key={selectedAgent.filename} agent={selectedAgent} />
              ) : (
                <EmptyEditPanel />
              )}
            </CardContent>
          </Card>
        </main>
      </div>
    </div>
  );
}
