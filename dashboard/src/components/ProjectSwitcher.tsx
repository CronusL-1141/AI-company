import { ChevronsUpDown, FolderOpen, Check } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { apiFetch } from '@/api/client';
import { useProject } from '@/context/ProjectContext';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Skeleton } from '@/components/ui/skeleton';
import type { Project } from '@/types';

interface ProjectListResponse {
  success: boolean;
  data: Project[];
}

export function ProjectSwitcher() {
  const { projectId, projectName, switchProject, clearProject } = useProject();

  const { data, isLoading } = useQuery({
    queryKey: ['projects-switcher'],
    queryFn: () => apiFetch<ProjectListResponse>('/api/projects'),
    staleTime: 60_000,
  });

  const projects = data?.data ?? [];

  if (isLoading) {
    return <Skeleton className="h-8 w-36" />;
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className="inline-flex items-center gap-2 rounded-md border border-input bg-background px-3 py-1.5 text-sm shadow-sm hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring max-w-[200px]"
        aria-label="Switch project"
      >
        <FolderOpen className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="truncate">
          {projectName ?? 'All Projects'}
        </span>
        <ChevronsUpDown className="h-3 w-3 shrink-0 opacity-50" />
      </DropdownMenuTrigger>

      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel>Switch Project</DropdownMenuLabel>
        <DropdownMenuSeparator />

        {/* All Projects (no filter) */}
        <DropdownMenuItem
          onClick={clearProject}
          className="flex items-center justify-between"
        >
          <span>All Projects</span>
          {!projectId && <Check className="h-4 w-4" />}
        </DropdownMenuItem>

        {projects.length > 0 && <DropdownMenuSeparator />}

        {projects.map((p: Project) => (
          <DropdownMenuItem
            key={p.id}
            onClick={() => switchProject(p.id, p.root_path, p.name)}
            className="flex items-center justify-between"
          >
            <div className="flex flex-col min-w-0">
              <span className="truncate font-medium">{p.name}</span>
              {p.root_path && (
                <span className="truncate text-xs text-muted-foreground">
                  {p.root_path}
                </span>
              )}
            </div>
            {projectId === p.id && <Check className="h-4 w-4 shrink-0 ml-2" />}
          </DropdownMenuItem>
        ))}

        {projects.length === 0 && (
          <DropdownMenuItem disabled>No projects found</DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
