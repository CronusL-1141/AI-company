export interface Team {
  id: string;
  name: string;
  mode: string;
  project_id?: string | null;
  leader_agent_id?: string | null;
  status: 'active' | 'completed' | 'archived';
  summary?: string;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
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
  source?: 'api' | 'hook';       // 来源标记
  session_id?: string | null;     // CC会话ID
  cc_tool_use_id?: string | null; // CC内部agent ID
  current_task?: string | null;   // 当前正在执行的任务描述
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

export interface Meeting {
  id: string;
  team_id: string;
  topic: string;
  status: 'active' | 'concluded';
  participants: string[];
  created_at: string;
  concluded_at: string | null;
}

export interface MeetingMessage {
  id: string;
  meeting_id: string;
  agent_id: string;
  agent_name: string;
  content: string;
  round_number: number;
  timestamp: string;
}

export interface WSEvent {
  type: string;
  source: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface Project {
  id: string;
  name: string;
  root_path: string;
  description: string;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface Phase {
  id: string;
  project_id: string;
  name: string;
  description: string;
  status: 'planning' | 'active' | 'completed' | 'archived';
  order: number;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}
