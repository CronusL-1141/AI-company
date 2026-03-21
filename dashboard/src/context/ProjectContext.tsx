import { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { setCurrentProjectPath } from '@/api/client';

const STORAGE_KEY = 'ai-team-os:project';

interface ProjectState {
  projectId: string | null;
  projectPath: string | null;
  projectName: string | null;
}

interface ProjectContextValue extends ProjectState {
  switchProject: (id: string, path: string, name: string) => void;
  clearProject: () => void;
}

const ProjectContext = createContext<ProjectContextValue | null>(null);

function loadFromStorage(): ProjectState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch {
    // ignore parse errors
  }
  return { projectId: null, projectPath: null, projectName: null };
}

export function ProjectProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<ProjectState>(loadFromStorage);

  // Sync to api/client module-level state on every change
  useEffect(() => {
    setCurrentProjectPath(state.projectPath);
  }, [state.projectPath]);

  const switchProject = useCallback((id: string, path: string, name: string) => {
    const next: ProjectState = { projectId: id, projectPath: path, projectName: name };
    setState(next);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  }, []);

  const clearProject = useCallback(() => {
    setState({ projectId: null, projectPath: null, projectName: null });
    localStorage.removeItem(STORAGE_KEY);
  }, []);

  return (
    <ProjectContext.Provider value={{ ...state, switchProject, clearProject }}>
      {children}
    </ProjectContext.Provider>
  );
}

export function useProject(): ProjectContextValue {
  const ctx = useContext(ProjectContext);
  if (!ctx) throw new Error('useProject must be used within ProjectProvider');
  return ctx;
}
