import type {
  ArchiveResponse,
  ControlPlaneResponse,
  DreamSequenceStatus,
  DreamRunsResponse,
  EvolutionProof,
  FilterOptions,
  GraphView,
  MemoryEvidence,
  Overview,
  PolicyRolloutStatus,
  ProjectMemoryConfig,
  ProjectMemoryStatus,
  Scope,
  TenantConfig,
  TenantPurgeResult,
  TenantPromptRun,
  TenantPrompts,
  TimelineEntry
} from './types';

export type GraphFilters = {
  scope: string;
  query: string;
  statuses: string[];
  types: string[];
  asOf: string;
  includeDemoted: boolean;
};

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers
    }
  });
  const payload: unknown = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message =
      typeof payload === 'object' &&
      payload !== null &&
      'error' in payload &&
      typeof (payload as { error: unknown }).error === 'string'
        ? (payload as { error: string }).error
        : response.statusText;
    throw new Error(message);
  }
  return payload as T;
}

function scopedParams(scope: string, asOf = ''): URLSearchParams {
  const params = new URLSearchParams();
  params.set('scope', scope);
  if (asOf.trim()) params.set('as_of', asOf.trim());
  return params;
}

export async function getOverview(scope?: string): Promise<Overview> {
  const params = new URLSearchParams();
  if (scope) params.set('scope', scope);
  const query = params.toString();
  const suffix = query ? `?${query}` : '';
  return fetchJson<Overview>(`/api/overview${suffix}`);
}

export async function getScopes(): Promise<Scope[]> {
  const response = await fetchJson<{ scopes: Scope[] }>('/api/scopes');
  return response.scopes;
}

export async function getTenantConfig(): Promise<TenantConfig> {
  return fetchJson<TenantConfig>('/api/tenant-config');
}

export async function getFilterOptions(scope: string): Promise<FilterOptions> {
  const params = scopedParams(scope);
  return fetchJson<FilterOptions>(`/api/filter-options?${params.toString()}`);
}

export async function getTenantPrompts(scope?: string): Promise<TenantPrompts> {
  const params = new URLSearchParams();
  if (scope) params.set('scope', scope);
  const query = params.toString();
  return fetchJson<TenantPrompts>(`/api/tenant-prompts${query ? `?${query}` : ''}`);
}

export async function getProjectMemoryConfig(): Promise<ProjectMemoryStatus> {
  return fetchJson<ProjectMemoryStatus>('/api/platform/project-memory/config');
}

export async function saveProjectMemoryConfig(payload: {
  project_goal: string;
  memory_goal: string;
  keep: string[];
  exclude: string[];
  rules: string[];
  allowed_memory_types: string[];
  protected_memory_types: string[];
  min_salience: number;
  max_memories_per_candidate: number;
  dedup_threshold: number;
  configured_by: string;
}): Promise<ProjectMemoryConfig> {
  return fetchJson<ProjectMemoryConfig>('/api/platform/project-memory/config', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export async function saveTenantPrompts(payload: {
  prompt_text: string;
  motive_name?: string;
  source_profile: string;
  source_profile_version: string;
}): Promise<TenantPrompts> {
  return fetchJson<TenantPrompts>('/api/tenant-prompts', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export async function rerunTenantPrompts(payload: {
  scope: string;
  agent_id?: string;
  motive_name?: string;
}): Promise<TenantPromptRun> {
  return fetchJson<TenantPromptRun>('/api/tenant-prompts/rerun', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export async function startDreamSequence(payload: {
  scope: string;
  agent_id?: string;
  motive_name?: string;
}): Promise<DreamSequenceStatus> {
  return fetchJson<DreamSequenceStatus>('/api/dream-sequence/run', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export async function getDreamSequenceStatus(runId?: string): Promise<DreamSequenceStatus | { runs: DreamSequenceStatus[] }> {
  const params = new URLSearchParams();
  if (runId) params.set('run_id', runId);
  const suffix = params.toString() ? `?${params.toString()}` : '';
  return fetchJson<DreamSequenceStatus | { runs: DreamSequenceStatus[] }>(`/api/dream-sequence/status${suffix}`);
}

export async function saveTenantLlm(payload: {
  provider: string;
  api_key: string;
  model: string;
  base_url: string;
}): Promise<TenantConfig> {
  return fetchJson<TenantConfig>('/api/tenant-config/llm', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export async function clearTenantLlm(): Promise<{ cleared: boolean; config: TenantConfig }> {
  return fetchJson<{ cleared: boolean; config: TenantConfig }>('/api/tenant-config/llm/clear', {
    method: 'POST',
    body: JSON.stringify({})
  });
}

export async function purgeTenantState(): Promise<TenantPurgeResult> {
  return fetchJson<TenantPurgeResult>('/api/tenant-config/purge', {
    method: 'POST',
    body: JSON.stringify({})
  });
}

export async function getGraph(filters: GraphFilters): Promise<GraphView> {
  const params = new URLSearchParams();
  params.set('scope', filters.scope);
  params.set('limit', '300');
  params.set('include_demoted', filters.includeDemoted ? 'true' : 'false');
  if (filters.query.trim()) params.set('query', filters.query.trim());
  if (filters.statuses.length) params.set('statuses', filters.statuses.join(','));
  if (filters.types.length) params.set('types', filters.types.join(','));
  if (filters.asOf.trim()) params.set('as_of', filters.asOf.trim());
  return fetchJson<GraphView>(`/api/graph?${params.toString()}`);
}

export async function getEvidence(scope: string, relationshipUuid: string): Promise<MemoryEvidence> {
  const params = scopedParams(scope);
  params.set('relationship_uuid', relationshipUuid);
  return fetchJson<MemoryEvidence>(`/api/evidence?${params.toString()}`);
}

export async function getTimeline(
  scope: string,
  subject: string,
  predicate: string,
  relationshipType: string
): Promise<TimelineEntry[]> {
  const params = scopedParams(scope);
  params.set('subject', subject);
  params.set('predicate', predicate);
  params.set('relationship_type', relationshipType);
  const response = await fetchJson<{ entries: TimelineEntry[] }>(`/api/timeline?${params.toString()}`);
  return response.entries;
}

export async function getEvolution(scope: string, asOf = ''): Promise<EvolutionProof> {
  const params = scopedParams(scope, asOf);
  const response = await fetchJson<{ proof: EvolutionProof }>(`/api/evolution?${params.toString()}`);
  return response.proof;
}

export async function getDreamRuns(): Promise<DreamRunsResponse> {
  return fetchJson<DreamRunsResponse>('/api/dream-runs?limit=30');
}

export async function getArchive(scope: string, asOf = ''): Promise<ArchiveResponse> {
  const params = scopedParams(scope, asOf);
  return fetchJson<ArchiveResponse>(`/api/archive?${params.toString()}`);
}

export async function getControlPlane(scope: string): Promise<ControlPlaneResponse> {
  const params = scopedParams(scope);
  return fetchJson<ControlPlaneResponse>(`/api/control-plane?${params.toString()}`);
}

export async function getPolicyRollout(scope: string, alias = 'production'): Promise<PolicyRolloutStatus> {
  const params = scopedParams(scope);
  params.set('alias', alias);
  return fetchJson<PolicyRolloutStatus>(`/api/policy-rollout?${params.toString()}`);
}
