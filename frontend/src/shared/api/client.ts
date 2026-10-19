export type HealthResponse = {
  database_ready: boolean;
  redis_ready: boolean;
  worker_ready: boolean;
  grobid_ready: boolean;
  details: Record<string, unknown>;
};

export type StageInfo = {
  name: string;
  label: string | null;
  description: string;
};

export type StageRunSummary = {
  id: number;
  stage: string;
  status: string;
  attempt_count: number;
  task_id: string | null;
  error: string | null;
  metrics: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type PipelineRunSummary = {
  id: number;
  run_key: string;
  query: string | null;
  status: string;
  config: Record<string, unknown>;
  celery_task_id: string | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
  finished_at: string | null;
  stages: StageRunSummary[];
};

export type PipelineRunEvent = {
  id: number;
  pipeline_run_id: number;
  stage: string | null;
  level: string;
  message: string;
  payload: Record<string, unknown>;
  created_at: string | null;
};

export type RunCreateRequest = {
  preview_id: string;
  processing_limit?: number | null;
  excluded_candidate_ids?: string[];
  overwrite: boolean;
  um_datasets_path?: string | null;
  output_path: string;
  strategy?: "standard" | "high_throughput";
  render_profile?: "full_body" | "pruned";
};

export type RunCreateResponse = {
  pipeline_run_id: number;
  task_id: string | null;
  status: string;
};

export type InsightsResponse = {
  columns: string[];
  rows: Record<string, unknown>[];
};

export type PaginatedInsightsResponse = InsightsResponse & {
  total: number;
  offset: number;
  limit: number;
};

export type CsvDownload = {
  blob: Blob;
  filename: string;
};

export type DiscoveryPhaseId = "direct" | "exact" | "related" | "focused" | "random" | "manual";

export type UMProfilePhase = {
  id: DiscoveryPhaseId;
  label: string;
  description: string;
  coverage_count: number;
  coverage_percent: number;
  estimated_calls: number;
  estimated_cost_usd: number;
};

export type UMDiscoveryProfile = {
  dataset_count: number;
  catalog_fingerprint: string;
  coverage: Record<string, number>;
  counts: Record<string, number>;
  topic_resolution: Record<string, unknown>;
  phases: UMProfilePhase[];
  top_topics: string[];
  top_keywords: string[];
  warnings: string[];
};

export type OpenAlexStatus = {
  status: "ready" | "missing" | "invalid" | "unavailable";
  available: boolean;
  remaining: number | null;
  limit: number | null;
  reset_seconds: number | null;
  reset_at: string | null;
  message: string;
};

export type DiscoveryPreviewRequest = {
  strategy_version: 2;
  mode: "catalog_funnel" | "random" | "manual";
  focus_query: string;
  manual_query: string | null;
  random_seed: number | null;
  from_year: number | null;
  to_year: number | null;
  publication_types: string[];
  language: string | null;
  discovery_limit: number;
  processing_limit: number;
  max_cost_usd: number;
};

export type DiscoveryCandidate = {
  paper_id: string;
  title: string | null;
  doi: string | null;
  year: number | null;
  source_url: string | null;
  open_access_url: string | null;
  oa_status: string | null;
  cited_by_count: number | null;
  primary_source_name: string | null;
  candidate_strength: number;
  evidence_tier: "direct" | "exact" | "expanded";
  evidence_reasons: string[];
  matched_um_dataset_ids: string[];
  pipeline_ready: boolean;
  included: boolean;
  exclusion_reason: string | null;
};

export type DiscoveryCandidateListResponse = {
  items: DiscoveryCandidate[];
  total: number;
  offset: number;
  limit: number;
};

export type DiscoveryPreview = {
  preview_id: string;
  strategy_version: 2;
  strategy_fingerprint: string;
  catalog_fingerprint: string;
  language: string | null;
  code_version: string;
  provider: "openalex";
  provider_snapshot_at: string;
  request: DiscoveryPreviewRequest;
  executed_queries: Array<Record<string, unknown>>;
  expires_at: string;
  candidate_count: number;
  included_count: number;
  ready_count: number;
  watchlist_count: number;
  estimated_cost_usd: number;
  actual_cost_usd: number;
  actual_calls: number;
  max_cost_usd: number;
  random_seed: number | null;
  partial: boolean;
  rate_limit: Record<string, string>;
  stop_reason: "ready_target_met" | "cost_ceiling" | "phases_exhausted" | "provider_failure";
  completed_phases: string[];
  phase_results: Record<string, Record<string, unknown>>;
  warnings: string[];
  metrics: Record<string, unknown>;
  profile: UMDiscoveryProfile;
  candidates: DiscoveryCandidate[];
};

export type UMDatasetSummary = {
  um_dataset_id: string;
  title: string;
  aliases: string[];
  creators: string[];
  doi: string | null;
  url: string | null;
  year: number | null;
  repository: string | null;
  keywords: string[];
  created_at: string | null;
  updated_at: string | null;
};

export type UMDatasetDetail = UMDatasetSummary & {
  raw: Record<string, unknown>;
};

export type UMDatasetListResponse = {
  items: UMDatasetSummary[];
  total: number;
  offset: number;
  limit: number;
  repositories: string[];
  years: number[];
};

export type UMDatasetVerificationIssue = {
  um_dataset_id: string;
  title: string;
  status: "missing" | "unexpected" | "changed";
  changed_fields: string[];
};

export type UMDatasetVerificationResponse = {
  status: "verified" | "mismatch" | "not_imported" | "unavailable";
  source_path: string;
  checked_at: string;
  source_count: number | null;
  stored_count: number;
  verified_count: number;
  issues: UMDatasetVerificationIssue[];
  warnings: string[];
  metrics: Record<string, unknown>;
  message: string | null;
};

export type ImportUMDatasetsResponse = {
  status: string;
  count: number;
  deleted: number;
  path: string;
  warnings: string[];
  metrics: Record<string, unknown>;
};

export type UMDatasetListParams = {
  q?: string;
  repository?: string;
  year?: number;
  offset?: number;
  limit?: number;
};

export type ResetRequest = {
  confirm: string;
  force: boolean;
};

export type ResetResponse = {
  status: string;
  active_runs: number;
  truncated_tables: string[];
  deleted_paths: string[];
  recreated_directories: string[];
};

type RequestOptions = {
  method?: "GET" | "POST";
  body?: unknown;
  signal?: AbortSignal;
};

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : `API request failed with status ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await fetch(path, {
    method: options.method ?? "GET",
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
  });

  if (!response.ok) {
    let detail: unknown = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      detail = response.statusText;
    }
    throw new ApiError(response.status, detail);
  }

  return response.json() as Promise<T>;
}

async function requestBlob(path: string): Promise<CsvDownload> {
  const response = await fetch(path, { method: "GET" });
  if (!response.ok) {
    let detail: unknown = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      detail = response.statusText;
    }
    throw new ApiError(response.status, detail);
  }

  const disposition = response.headers.get("Content-Disposition") ?? "";
  const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] ?? "datasight-insights.csv";
  return { blob: await response.blob(), filename };
}

export const api = {
  health: () => request<HealthResponse>("/api/v1/health"),
  stages: async () => {
    const response = await request<{ stages: StageInfo[] }>("/api/v1/stages");
    return response.stages;
  },
  runs: async (limit = 25) => {
    const params = new URLSearchParams({ limit: String(limit) });
    const response = await request<{ runs: PipelineRunSummary[] }>(`/api/v1/runs?${params}`);
    return response.runs;
  },
  run: (runId: number) => request<PipelineRunSummary>(`/api/v1/runs/${runId}`),
  runEvents: async (runId: number, limit = 200) => {
    const params = new URLSearchParams({ limit: String(limit) });
    const response = await request<{ events: PipelineRunEvent[] }>(
      `/api/v1/runs/${runId}/events?${params}`,
    );
    return response.events;
  },
  createRun: (body: RunCreateRequest, signal?: AbortSignal) =>
    request<RunCreateResponse>("/api/v1/runs", { method: "POST", body, signal }),
  umDiscoveryProfile: () =>
    request<UMDiscoveryProfile>("/api/v1/discovery/um-profile"),
  openAlexStatus: () => request<OpenAlexStatus>("/api/v1/openalex/status"),
  discoveryPreview: (body: DiscoveryPreviewRequest, signal?: AbortSignal) =>
    request<DiscoveryPreview>("/api/v1/discovery/preview", { method: "POST", body, signal }),
  discoveryCandidates: (runId: number, offset = 0, limit = 100, selectedOnly = false) => {
    const params = new URLSearchParams({
      offset: String(offset),
      limit: String(limit),
      selected_only: String(selectedOnly),
    });
    return request<DiscoveryCandidateListResponse>(
      `/api/v1/runs/${runId}/discovery-candidates?${params}`,
    );
  },
  runResults: (runId: number, offset = 0, limit = 50) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    return request<PaginatedInsightsResponse>(`/api/v1/runs/${runId}/insights?${params}`);
  },
  runCandidates: (runId: number, offset = 0, limit = 50) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    return request<PaginatedInsightsResponse>(`/api/v1/runs/${runId}/candidates?${params}`);
  },
  downloadRunCandidateResultsCsv: (runId: number, columns: string[]) => {
    const params = new URLSearchParams();
    columns.forEach((column) => params.append("columns", column));
    return requestBlob(`/api/v1/runs/${runId}/candidates/export.csv?${params}`);
  },
  downloadRunInsightResultsCsv: (runId: number, columns: string[]) => {
    const params = new URLSearchParams();
    columns.forEach((column) => params.append("columns", column));
    return requestBlob(`/api/v1/runs/${runId}/insights/export.csv?${params}`);
  },
  insights: (limit = 100) => {
    const params = new URLSearchParams({ limit: String(limit) });
    return request<InsightsResponse>(`/api/v1/insights?${params}`);
  },
  downloadInsightsCsv: (columns: string[]) => {
    const params = new URLSearchParams();
    columns.forEach((column) => params.append("columns", column));
    return requestBlob(`/api/v1/insights/export.csv?${params}`);
  },
  umDatasets: (filters: UMDatasetListParams = {}) => {
    const params = new URLSearchParams();
    if (filters.q) params.set("q", filters.q);
    if (filters.repository) params.set("repository", filters.repository);
    if (filters.year !== undefined) params.set("year", String(filters.year));
    params.set("offset", String(filters.offset ?? 0));
    params.set("limit", String(filters.limit ?? 50));
    return request<UMDatasetListResponse>(`/api/v1/um-datasets?${params}`);
  },
  importUmDatasets: (path: string) =>
    request<ImportUMDatasetsResponse>("/api/v1/um-datasets/import", {
      method: "POST",
      body: { path },
    }),
  umDataset: (umDatasetId: string) =>
    request<UMDatasetDetail>(`/api/v1/um-datasets/${encodeURIComponent(umDatasetId)}`),
  verifyUmDatasets: () =>
    request<UMDatasetVerificationResponse>("/api/v1/um-datasets/verification"),
  reset: (body: ResetRequest) =>
    request<ResetResponse>("/api/v1/admin/reset", { method: "POST", body }),
};

export function isActiveRun(status: string | undefined) {
  return status === "queued" || status === "running" || status === "started";
}
