export interface Team {
  id: string;
  name: string;
  mode: string;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface Agent {
  id: string;
  team_id: string;
  name: string;
  role: string;
  system_prompt: string;
  model: string;
  status: string;
  config: Record<string, unknown>;
  created_at: string;
}

export interface Task {
  id: string;
  team_id: string;
  title: string;
  description: string;
  status: string;
  result: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface Event {
  id: string;
  type: string;
  source: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface APIResponse<T> {
  data: T;
  message: string;
}

export interface APIListResponse<T> {
  data: T[];
  total: number;
  message: string;
}

export interface TeamStatus {
  team: Team;
  agents: Agent[];
  active_tasks: Task[];
  completed_tasks: number;
  total_tasks: number;
}

export interface WSEvent {
  type: string;
  source: string;
  data: Record<string, unknown>;
  timestamp: string;
}
