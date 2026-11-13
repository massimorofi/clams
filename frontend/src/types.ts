export type BadgeState = 'Idle' | 'Executing' | 'Paused' | 'Error'

export interface Heartbeat {
  alive: boolean
  age_seconds: number | null
}

export interface StatusPayload {
  state: BadgeState
  repository: string
  active_agents: number
  heartbeats: Record<string, Heartbeat>
  unacked_alerts: number
  outbox: Record<string, number>
  timestamp: string
}

export interface ProjectInfo {
  owner: string
  repo: string
  full_name: string
  branch: string
  commit: string
  worktree_dir: string
  clean: boolean
  api_base: string
}

export interface EditableField {
  path: string
  label: string
  type: 'number' | 'boolean' | 'text'
  min?: number
  max?: number
  step?: number
}

export interface ConfigResponse {
  settings: Record<string, any>
  runtime_overlay: Record<string, any>
  editable: EditableField[]
}

export interface ConfigPutResponse {
  ok: boolean
  changed: Record<string, { from: any; to: any }>
  settings: Record<string, any>
}

export interface CliResult {
  ok: boolean
  exit_code: number
  stdout: string
  stderr: string
  command: string
}

export interface AlertRow {
  id: number
  severity: string
  source: string
  message: string
  created_at?: string
  acknowledged?: number
}

export interface WorkItemRow {
  requirement_number: number
  requirement_title: string
  status: string
  [k: string]: any
}

export interface ChatMessage {
  id: string
  role: 'user' | 'planner' | 'system'
  text: string
  ts: string
}

export interface ProjectRow {
  id: number
  name: string
  url: string
  status: string
  progress: number
  created_at?: string
  updated_at?: string
  updated?: string
}

export interface ProjectCreateResult {
  ok: boolean
  project: ProjectRow
}

export interface AlertAckResult {
  ok: boolean
  acknowledged?: number
  id?: number
  unacked_alerts: number
}

/** The four Clams roles. Only planner and executor are Claudio agent instances. */
export type AgentRole = 'planner' | 'executor' | 'orchestrator' | 'watchdog'

/** One configured agent as returned by the API (secrets masked). */
export interface AgentRow {
  id: string
  role: AgentRole
  base_url: string
  effective_base_url: string
  enabled: boolean
  request_timeout: number
  has_auth_token: boolean
}

export interface AgentsResponse {
  agents: AgentRow[]
  roles: AgentRole[]
  claudio_roles: string[]
  defaults: {
    host: string
    planner_port: number
    executor_port: number
    request_timeout: number
  }
}

export interface AgentMutationResponse {
  ok: boolean
  agent?: AgentRow
  agents: AgentRow[]
}

export interface AgentProbeResult {
  ok: boolean
  agent_id: string
  status: number | null
  url: string
  detail: string
}
