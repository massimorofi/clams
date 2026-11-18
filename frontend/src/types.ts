export interface Executor {
  executor_id: string;
  version: string | null;
  capacity: number | null;
  last_seen: string | null;
}

export interface Alert {
  alert_id: number | string;
  probe: string | null;
  severity: string;
  message: string;
  status: string;
  created_at: string | null;
}

export interface Overview {
  workflows: Record<string, number>;
  tasks: Record<string, number>;
  success_rate: number;
  executors: Executor[];
  alerts: Alert[];
  paused: boolean;
  paused_by: string | null;
  outbox: Record<string, number>;
}

export interface Workflow {
  workflow_id: string;
  project_id: number | string;
  source: string;
  gh_issue_number: number | null;
  goal: string;
  status: string;
  replans: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface TaskSummary {
  task_id: string;
  sequence_index: number;
  title: string;
  status: string;
  attempt: number;
  depends_on: number[];
  commit_sha: string | null;
  branch: string | null;
  gh_issue_number: number | null;
}

export interface WorkflowDetail extends Workflow {
  tasks: TaskSummary[];
}

export interface TaskBrief {
  objective: string | null;
  acceptance: string | null;
  run: string[] | null;
  writable: string[] | null;
  forbidden: string[] | null;
  repo: string | null;
  base_ref: string | null;
  max_turns: number | null;
  max_wall_s: number | null;
  answer?: string | null;
  session_ref?: string | null;
}

export type EvidenceCheck = Record<string, unknown>;

export interface Evidence {
  passed: boolean | null;
  reason: string | null;
  checks: EvidenceCheck[] | null;
}

export interface TaskResult {
  outcome: string | null;
  evidence: Evidence | null;
  commit_sha: string | null;
  branch: string | null;
  changed_files: string[] | null;
  log_tail: string | null;
  session_ref: string | null;
}

export interface TaskDetail extends TaskSummary {
  workflow_id?: string | null;
  brief: TaskBrief | null;
  result: TaskResult | null;
  lease_token?: string | null;
  lease_expires_at?: string | null;
  executor_id?: string | null;
  session_ref?: string | null;
  error_log?: string | null;
}

export interface Question {
  question_id: number | string;
  task_id: string;
  attempt: number;
  question: string;
  options: string | null;
  status: string;
  answer: string | null;
  asked_at: string | null;
  expires_at: string | null;
}

export type CommandStatus = 'new' | 'applied' | 'rejected';

export interface Command {
  command_id: number | string;
  kind: string;
  target: string | null;
  payload: unknown;
  status: CommandStatus;
  result: unknown;
  created_at: string | null;
  applied_at: string | null;
}

export interface CommandRequest {
  kind: string;
  target?: string | null;
  payload?: unknown;
  issued_by: string;
}

export interface CommandAck {
  command_id: number | string;
  status: string;
}

export interface Project {
  project_id?: number | string;
  repo?: string;
  gh_project_node_id?: string | null;
  enabled?: boolean | number;
  created_at?: string | null;
  [key: string]: unknown;
}

export type TranscriptMessage = Record<string, unknown>;

export interface Transcript {
  messages: TranscriptMessage[];
}
