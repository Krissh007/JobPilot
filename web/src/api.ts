// Typed client for the JobPilot FastAPI backend (proxied at /api by Vite).

export const STAGES = ["found", "shortlisted", "docs_ready", "applied", "interview", "offer", "rejected"] as const;
export type Stage = (typeof STAGES)[number];
export const STAGE_LABEL: Record<Stage, string> = {
  found: "Found", shortlisted: "Shortlisted", docs_ready: "Docs ready", applied: "Applied",
  interview: "Interview", offer: "Offer", rejected: "Rejected",
};
export const STAGE_COLOR: Record<Stage, string> = {
  found: "var(--muted)", shortlisted: "var(--accent)", docs_ready: "var(--violet)", applied: "var(--warn)",
  interview: "var(--good)", offer: "var(--good)", rejected: "var(--bad)",
};

export interface Meta {
  db: string; total_jobs: number; example_jobs: number; unscored: number;
  stages: Record<Stage, number>; applied_today: number; daily_apply_cap: number; threshold: number;
  pending_review: number; blocked: number; running: { id: number; agent: string; started_at: string }[]; today: string;
}

export interface JobSummary {
  id: number; title: string; company: string; source: string; location: string;
  exp_min: number | null; exp_max: number | null; score: number | null; score_skills: number | null;
  score_experience: number | null; score_domain: number | null; legacy_score: number | null;
  status: Stage; found_at: string; applied_at: string | null; is_example: boolean; skills: string[]; has_docs: boolean;
  match_group: "best" | "maybe" | "eliminate" | "pending" | "below" | ""; match_reason: string;
}

export interface HistoryRow { from_status: Stage | null; to_status: Stage; changed_at: string; actor: "user" | "agent" }

export interface JobDetail extends JobSummary {
  url: string; jd_text: string; score_reason: string; notes: string; resume_version: string;
  docs: { resume: boolean; cover: boolean; report: boolean; folder: string | null };
  applications: { id: number; state: string; created_at: string; decided_at: string | null; reason: string }[];
  history: HistoryRow[];
}

export interface AppField { id: number; step: number; field_name: string; value_used: string; source: "resume" | "answer_bank" | "llm"; needs_review: boolean }
export interface Application {
  id: number; job_id: number; state: "filling" | "pending_review" | "approved" | "submitted" | "skipped" | "blocked";
  screenshot_path: string; has_screenshot: boolean; created_at: string; decided_at: string | null; is_example: boolean; reason: string;
  job: { id: number; title: string; company: string; score: number | null; source: string; url: string } | null;
  fields: AppField[]; needs_review: number;
}

export type AgentName = "discovery" | "scoring" | "documents" | "apply" | "search" | "score" | "docs"; // last three: retired, history only
export interface Run {
  id: number; agent: AgentName; started_at: string; finished_at: string | null; found: number; processed: number;
  status: "running" | "ok" | "error"; error: string; is_example: boolean; log_lines: number; log?: string;
}

export interface Answer { id: number; question: string; answer: string; tags: string; times_used: number; created_at: string; updated_at: string; is_example: boolean }

export interface DiscoveryFilters {
  keywords: string[]; use_variants: boolean; strict_titles: boolean; related_titles: string[]; variants: string[]; locations: string[]; sources: string[];
  max_posted_days: number; max_cards_per_query: number; max_pages_per_query: number; enrich_per_query: number;
  backlog_budget: number; naukri_sort_param: string;
}

export interface Settings {
  threshold: number; daily_apply_cap: number; search_queries: string[];
  schedule: { every_hours: number; start: string; end: string; timezone: string; days: string[]; times: string[] };
  discovery: DiscoveryFilters;
  scoring: { exp_min: number; exp_max: number };
  auto_submit: boolean; park_incomplete: boolean; review_wait_minutes: number; linkedin_easy_apply_only: boolean;
  weekly_review: { day: string; time: string }; answers_reviewed_at: string | null;
}

export interface WeeklyReview {
  due: boolean; due_at: string; next_due_at: string; reviewed_at: string | null;
  open_questions: { id: number; question: string; created_at: string }[];
  new_this_week: { id: number; question: string; answered: boolean }[];
  total_answers: number;
}

export interface SystemInfo {
  gemini_model: string; gemini_fallback_models: string; gemini_key_set: boolean; gemini_rpm: number;
  resume_path: string;
}

export interface ScheduledTask {
  name: string; registered: boolean; state?: string; next_run?: string | null; last_run?: string | null;
  last_result?: number; last_result_text: string;
}

export interface ScheduleStatus {
  tasks: ScheduledTask[]; error: string; log_tail: string[]; command: string; checked_at: string;
  config: { tasks: { pipeline: string; weekly: string; catchup: string }; start: string; every_hours: number; window_minutes: number; weekly_day: string; weekly_time: string };
}

export interface Funnel { stages: { stage: Stage; count: number }[]; rejected: number; total: number }
export interface ScoreHistogram { bins: { from: number; to: number; count: number }[]; unscored: number; threshold: number }
export interface PerDay { days: { date: string; count: number }[] }

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "string" ? detail : (detail as { message?: string })?.message ?? `HTTP ${status}`);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json", ...init.headers } : init?.headers,
  });
  if (!response.ok) {
    let detail: unknown = response.statusText;
    try { detail = (await response.json()).detail; } catch { /* not JSON */ }
    throw new ApiError(response.status, detail);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

export interface JobQuery { match?: "best" | "maybe" | "eliminate"; status?: Stage[]; source?: string; min_score?: number; q?: string; include_examples?: boolean; sort?: string; order?: "asc" | "desc" }

export function jobQueryString(q: JobQuery): string {
  const params = new URLSearchParams();
  q.status?.forEach((s) => params.append("status", s));
  if (q.source) params.set("source", q.source);
  if (q.match) params.set("match", q.match);
  if (q.min_score != null) params.set("min_score", String(q.min_score));
  if (q.q?.trim()) params.set("q", q.q.trim());
  if (q.include_examples === false) params.set("include_examples", "false");
  params.set("sort", q.sort ?? "score");
  params.set("order", q.order ?? "desc");
  params.set("limit", "2000");
  return params.toString();
}

const json = (body: unknown) => ({ body: JSON.stringify(body) });

export const api = {
  meta: () => request<Meta>("/meta"),
  jobs: (q: JobQuery) => request<{ total: number; items: JobSummary[] }>(`/jobs?${jobQueryString(q)}`),
  job: (id: number) => request<JobDetail>(`/jobs/${id}`),
  createJob: (body: Partial<JobDetail>) => request<JobSummary>("/jobs", { method: "POST", ...json(body) }),
  patchJob: (id: number, body: { status?: Stage; notes?: string }) => request<JobSummary>(`/jobs/${id}`, { method: "PATCH", ...json(body) }),
  deleteJob: (id: number) => request<void>(`/jobs/${id}`, { method: "DELETE" }),
  openFolder: (id: number) => request<{ opened: string }>(`/jobs/${id}/open-folder`, { method: "POST" }),
  applications: (states?: string[]) => request<Application[]>(`/applications${states?.length ? "?" + states.map((s) => `state=${s}`).join("&") : ""}`),
  decide: (id: number, decision: "approve" | "skip") => request<Application>(`/applications/${id}/${decision}`, { method: "POST" }),
  markSubmitted: (id: number) => request<Application>(`/applications/${id}/mark-submitted`, { method: "POST" }),
  runs: (limit = 50) => request<Run[]>(`/runs?limit=${limit}`),
  run: (id: number) => request<Run>(`/runs/${id}`),
  // auto_submit is the Settings switch; the server also checks it, and anything not explicitly true is review-only
  runNow: (autoSubmit = false) => request<{ started: AgentName[]; log: string; auto_submit: boolean; mode: string }>("/runs/pipeline", { method: "POST", ...json({ auto_submit: autoSubmit }) }),
  startRun: (agent: AgentName, options: Record<string, unknown> = {}) => request<Run>(`/runs/${agent}`, { method: "POST", ...json(options) }),
  answers: () => request<Answer[]>("/answers"),
  createAnswer: (body: Pick<Answer, "question" | "answer" | "tags">) => request<Answer>("/answers", { method: "POST", ...json(body) }),
  updateAnswer: (id: number, body: Partial<Pick<Answer, "question" | "answer" | "tags">>) => request<Answer>(`/answers/${id}`, { method: "PUT", ...json(body) }),
  deleteAnswer: (id: number) => request<void>(`/answers/${id}`, { method: "DELETE" }),
  settings: () => request<Settings>("/settings"),
  saveSettings: (body: Partial<Settings>) => request<Settings>("/settings", { method: "PUT", ...json(body) }),
  weekly: () => request<WeeklyReview>("/review/weekly"),
  weeklyDone: () => request<WeeklyReview>("/review/weekly/done", { method: "POST" }),
  tableCounts: () => request<Record<string, number>>("/stats/tables"),
  system: () => request<SystemInfo>("/system"),
  schedule: () => request<ScheduleStatus>("/schedule"),
  funnel: (examples: boolean) => request<Funnel>(`/analytics/funnel?include_examples=${examples}`),
  scoreHistogram: (examples: boolean) => request<ScoreHistogram>(`/analytics/score-histogram?include_examples=${examples}`),
  perDay: (days: number, examples: boolean) => request<PerDay>(`/analytics/applications-per-day?days=${days}&include_examples=${examples}`),
  registerSchedule: (on: boolean) => request<ScheduleStatus & { output: string[] }>(`/schedule/${on ? "register" : "unregister"}`, { method: "POST" }),
  seedExamples: () => request<{ added_jobs: number }>("/examples/seed", { method: "POST" }),
  purgeExamples: () => request<Record<string, number>>("/examples", { method: "DELETE" }),
};
