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
  // 拥有此会话容器队的 CC 进程；后端派生，不落库。同一进程的多支容器队靠它归组。
  // 证不出来就是 null（历史会话在 CC 的进程登记里查不到），非容器队恒为 null。
  cc_pid?: number | null;
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
  last_active_at?: string | null; // 最后活跃时间
  // Sub-agent context watermark ledger (docs/agent-reuse-design.md section 4).
  // Populated on SubagentStop; agents with no capture yet leave these null.
  ctx_tokens?: number | null;
  ctx_window?: number | null;
  ctx_pct?: number | null;
  transcript_path?: string | null;
  ctx_measured_at?: string | null;
  reuse_domain?: string | null;
}

export interface Task {
  id: string;
  team_id: string;
  title: string;
  description: string;
  status: string;
  result: string | null;
  priority: 'critical' | 'high' | 'medium' | 'low';
  horizon: 'short' | 'mid' | 'long';
  tags: string[];
  score?: number;
  assigned_to?: string | null;
  team_name?: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface TaskWallResponse {
  wall: {
    short: Task[];
    mid: Task[];
    long: Task[];
  };
  completed?: Task[];
  stats: {
    total: number;
    by_priority: Record<string, number>;
    by_status: Record<string, number>;
    avg_score: number;
    completed_count?: number;
  };
}

export interface Event {
  id: string;
  type: string;
  source: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface Memory {
  id: string;
  scope: 'global' | 'team' | 'agent' | 'user';
  scope_id: string;
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
  accessed_at: string | null;
}

export interface APIResponse<T> {
  data: T;
  message: string;
  /** 部分写操作端点（如 task run/decompose）会额外回一句操作指引，
   *  诚实说明"这只是入队，需要 CC 会话领取"——前端应尽量透出而非丢弃。 */
  _hint?: string;
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

export interface AgentActivity {
  id: string;
  agent_id: string;
  agent_name?: string;
  session_id: string;
  tool_name: string;
  input_summary: string;
  output_summary: string;
  status: 'running' | 'completed' | 'error';
  duration_ms: number | null;
  timestamp: string;
  error?: string;
}
