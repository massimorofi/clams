import type {
  Command,
  CommandAck,
  CommandRequest,
  Overview,
  Project,
  Question,
  TaskDetail,
  Transcript,
  Workflow,
  WorkflowDetail,
} from './types';

export const TOKEN_KEY = 'clams_api_token';

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? '';
  } catch {
    return '';
  }
}

export function setToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable */
  }
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers ?? {});
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (init?.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, 'Network error — is the Clams API reachable?');
  }

  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!res.ok) {
    let msg = res.statusText || `HTTP ${res.status}`;
    if (typeof data === 'string' && data) msg = data;
    else if (data && typeof data === 'object') {
      const d = data as Record<string, unknown>;
      if (typeof d.detail === 'string') msg = d.detail;
      else if (typeof d.message === 'string') msg = d.message;
    }
    if (res.status === 401 || res.status === 403) {
      msg = 'Unauthorized — set the API bearer token in Settings.';
    }
    throw new ApiError(res.status, msg);
  }
  return data as T;
}

export const api = {
  overview: () => request<Overview>('/overview'),

  workflows: (status?: string) =>
    request<Workflow[]>(`/workflows${status ? `?status=${encodeURIComponent(status)}` : ''}`),

  workflow: (id: string) => request<WorkflowDetail>(`/workflows/${encodeURIComponent(id)}`),

  task: (id: string) => request<TaskDetail>(`/tasks/${encodeURIComponent(id)}`),

  transcript: (id: string) =>
    request<Transcript>(`/tasks/${encodeURIComponent(id)}/transcript`),

  questions: (status = 'open') =>
    request<Question[]>(`/questions?status=${encodeURIComponent(status)}`),

  projects: async (): Promise<Project[]> => {
    const raw = await request<unknown>('/projects');
    if (Array.isArray(raw)) return raw as Project[];
    if (raw && typeof raw === 'object' && Array.isArray((raw as { projects?: unknown }).projects)) {
      return (raw as { projects: Project[] }).projects;
    }
    return [];
  },

  command: (id: string | number) => request<Command>(`/commands/${encodeURIComponent(String(id))}`),

  sendCommand: (body: CommandRequest) =>
    request<CommandAck>('/commands', { method: 'POST', body: JSON.stringify(body) }),
};
