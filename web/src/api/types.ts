export interface Task {
  id: number;
  short_id: string;
  project_id: number | null;
  parent_task_id: number | null;
  phase_number: number | null;
  phase_slug: string | null;
  title: string;
  description: string;
  kind: string;
  status: string;
  risk: string;
  priority: string;
  tags: string[];
  acceptance_criteria: string[];
  definition_of_done: string[];
  frozen_contracts: string[];
  out_of_scope: string[];
  branch_name: string | null;
  assigned_provider_profile: string | null;
  assigned_harness_adapter: string | null;
  handoff_status: string | null;
  review_status: string | null;
  verification_status: string | null;
  merge_status: string | null;
  source: string;
  created_at: string;
  updated_at: string;
}

export interface Run {
  id: number;
  short_id: string;
  task_id: number;
  status: string;
  provider_profile: string | null;
  harness_adapter: string | null;
  command_preview: string | null;
  worktree_path: string | null;
  branch_name: string | null;
  commit_sha: string | null;
  exit_code: number | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  error: string | null;
  artifacts_path: string | null;
  created_at: string;
}

export interface Review {
  id: number;
  run_id: number | null;
  reviewer_model: string | null;
  verdict: string;
  findings: { file_or_area: string; issue: string; violates?: string }[];
  reasoning: string | null;
  created_at: string;
}

export interface Verification {
  id: number;
  run_id: number | null;
  status: string;
  checks_json: { name: string; status: string; command?: string; evidence?: string; notes?: string }[];
  contradicts_worker: boolean;
  created_at: string;
}

export interface MemoryEntry {
  id: number;
  category: string;
  scope: string;
  project_slug: string | null;
  role: string | null;
  content: string;
  source: string;
  tags_json: string[];
  approved: boolean;
  rejected: boolean;
  updated_at: string;
}

export interface TaskDetail extends Task {
  runs: Run[];
  reviews: Review[];
  verifications: Verification[];
  memory_suggestions: MemoryEntry[];
  merge_command?: string;
}

export interface Project {
  id: number;
  name: string;
  slug: string;
  path: string;
  base_branch: string;
  test_command: string | null;
  lint_command: string | null;
  build_command: string | null;
  allowed_commands: string[];
  notes: string | null;
}

export interface ProviderStatus {
  id: number;
  name: string;
  kind: string;
  enabled: boolean;
  base_url: string | null;
  api_key_env: string | null;
  api_key_set: boolean;
  configured: boolean;
  problems: string[];
  notes: string | null;
}

export interface ModelProfile {
  id: number;
  name: string;
  role: string;
  provider_id: number;
  model_name: string;
  temperature: number | null;
  max_tokens: number;
  effort: string | null;
  enabled: boolean;
  notes: string | null;
}

export interface Adapter {
  id: number;
  name: string;
  kind: string;
  command: string;
  args_template: string[];
  env_vars: string[];
  timeout_seconds: number;
  enabled: boolean;
  experimental: boolean;
  detected: boolean;
  binary: string | null;
  verify_hint: string;
  notes: string | null;
}

export interface Schedule {
  id: number;
  name: string;
  cron_expression: string;
  task_template_json: Record<string, unknown>;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
}

export interface BuildLog {
  id: number;
  timestamp: string;
  phase: string | null;
  task_id: number | null;
  run_id: number | null;
  role: string;
  event: string;
  details: string;
  commit_sha: string | null;
}

export interface PreviewSection {
  index: number;
  name: string;
  static: boolean;
  cache_breakpoint: boolean;
  tokens: number;
  empty: boolean;
  excerpt: string;
}

export interface PreviewReport {
  sections: PreviewSection[];
  static_tokens: number;
  dynamic_tokens: number;
  total_tokens: number;
  cacheable_fraction: number;
  warnings: string[];
  ok: boolean;
}
