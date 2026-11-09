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
