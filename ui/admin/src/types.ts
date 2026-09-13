export type Screen =
  | 'overview'
  | 'memory'
  | 'explainability'
  | 'prompts'
  | 'project-memory'
  | 'dreaming'
  | 'policy'
  | 'tenant'
  | 'integration';

export type Scope = {
  key: string;
  kind: string;
  scope_id: string;
  relationship_count?: number;
};

export type TenantConfig = {
  tenant: {
    tenant_id: string;
    agent_ids: string[];
    example_agent_id: string;
    default_scope: string;
    warnings?: string[];
  };
  operator: {
    graph_path: string;
    platform_api_url: string;
    ui_url: string;
    mcp_url: string;
    mcp_configured?: boolean;
    integration_contract_url: string;
  };
  llm: LlmStatus;
  integration: {
    contract_url: string;
    labels: string[];
  };
  snippets: {
    sdk: string;
    mcp: string;
    codex_mcp_config: string;
  };
};

export type LlmStatus = {
  tenant_id: string;
  has_api_key: boolean;
  provider?: string;
  model?: string;
  base_url?: string;
  created_at?: string;
  updated_at?: string;
  /** "sealed" (memotron llm configure / Tenant Setup form) or "environment"
   * (LITELLM_API_KEY / OPENAI_API_KEY already in the server process env).
   * Absent when neither is configured. */
  source?: 'sealed' | 'environment';
  /** Set only when source is "environment": which env var is active. */
  environment_key_env?: string;
};

export type FilterOptions = {
  scope: Scope;
  options: {
    types: string[];
    statuses: string[];
    subjects: string[];
    predicates: string[];
    objects: string[];
  };
};

export type TenantPrompts = {
  tenant_id: string;
  active_pack: string;
  current: PromptVersion;
  versions: PromptVersion[];
  library: PromptVersion[];
  motives: MotiveOption[];
  resolved_motive: ResolvedMotive;
  motive_evidence: MotiveEvidence;
};

export type ProjectMemoryConfig = {
  tenant_id: string;
  version: string;
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
  active: boolean;
  created_at: string;
  updated_at: string;
};

export type ProjectMemoryStatus = {
  tenant_id: string;
  project_scope: Scope;
  configured: boolean;
  config: ProjectMemoryConfig | null;
  versions: ProjectMemoryConfig[];
  candidate_count: number;
  pending_candidate_count: number;
};

export type PromptVersion = {
  key: string;
  kind: 'tenant' | 'library' | 'legacy' | string;
  label: string;
  version?: string;
  profile: string;
  profile_version: string;
  prompt_text: string;
  motive_name: string;
  created_at: string;
  updated_at: string;
  active: boolean;
};

export type MotiveOption = {
  name: string;
  goal: string;
  allowed_memory_types: string[];
  prompt_profile: string;
  prompt_profile_version: string;
  dedup_threshold?: number | null;
  retrieval_budget_share?: number | null;
  salience_rubric?: unknown;
};

export type ResolvedMotive = {
  name: string;
  source: string;
  source_label: string;
  scope?: string;
};

export type ObservedMotive = {
  motive_name: string;
  motive_version_digest: string;
  fact_count: number;
};

export type MotiveEvidence = {
  scope: string;
  fact_count: number;
  observed: ObservedMotive[];
  divergent: boolean;
};

export type LegacyPromptProfile = {
    name: string;
    version: string;
    key: string;
    goal: string;
    include: string[];
    exclude: string[];
    rules: string[];
    examples: string[];
    rendered_prompt: string;
};

export type TenantPromptRun = {
  scope: Scope;
  agent_id: string | null;
  result: {
    ran_at: string;
    job_runs: Array<{
      job_name: string;
      processed_episodes: number;
      created_relationships: number;
      reinforced_relationships: number;
      superseded_relationships: number;
      pruned_relationships?: number;
    }>;
  };
};

export type TenantPurgeResult = {
  tenant_id: string;
  purged: {
    tenant_id: string;
    agent_ids: string[];
    scope_keys: string[];
    raw_episodes_preserved: number;
    credentials_preserved: boolean;
    [key: string]: string | number | boolean | string[];
  };
  config: TenantConfig;
  prompts: TenantPrompts;
};

export type DreamSequenceStatus = {
  run_id: string;
  status: 'queued' | 'running' | 'completed' | 'error' | string;
  running: boolean;
  scope: Scope;
  agent_id: string;
  motive_name: string;
  motive_source?: string;
  motive_source_label?: string;
  started_at: string;
  completed_at?: string | null;
  result?: TenantPromptRun['result'] | null;
  error?: string;
  capability_signals: Array<{
    name: string;
    description: string;
  }>;
  events: Array<{
    at: string;
    phase: string;
    message: string;
    details: Record<string, unknown>;
  }>;
};

export type Overview = {
  tenant: TenantConfig['tenant'];
  operator: TenantConfig['operator'];
  llm: LlmStatus;
  readiness: {
    llm_configured: boolean;
    agents_observed: boolean;
    scopes_registered: boolean;
    platform_api_ready: boolean;
    mcp_ready: boolean;
  };
  scope: Scope;
  scopes: Scope[];
  memory: {
    visible_facts: number;
    active_facts: number;
    inactive_facts: number;
    episodes: number;
    pending_episodes: number;
    compression_ratio: number;
    semantic_dedup_rate: number;
    rollups: number;
    demoted: number;
    per_type_active_counts: Record<string, number>;
  };
  evolution: {
    dream_run_count: number;
    decision_count: number;
    latest_dream_run: DreamRun | null;
    latest_decision: DreamDecision | null;
    signals: EvolutionSignal[];
  };
};

export type PolicyRolloutStatus = {
  scope: Scope;
  alias: string;
  active_alias: {
    scope: Scope;
    alias: string;
    contract_digest: string;
    previous_contract_digest?: string | null;
    updated_at: string;
  } | null;
  compiled_effective_policy: {
    contract_digest: string;
    motive: {
      name: string;
      goal: string;
      allowed_memory_types: string[];
      [key: string]: unknown;
    };
    replay_options: {
      model_identifier: string;
      temperature: number;
      repetitions: number;
      minimum_stability_score: number;
      [key: string]: unknown;
    };
    source_trace: Record<string, string>;
    certification_passed: boolean;
    staged_at: string;
    [key: string]: unknown;
  } | null;
  expected_episode_routing: {
    production_scope: string;
    motive: string;
    allowed_memory_types: string[];
    shadow_visibility: string;
  } | null;
  certification: {
    passed: boolean;
    corpus_name: string;
    corpus_digest: string;
    policy_delta: number;
    replay_flip_rate: number;
    stability_score: number;
    failures: string[];
    protected_invariants: Array<{ name: string; passed: boolean; detail: string }>;
  } | null;
  shadow_stages: Array<{
    stage_id: string;
    candidate_contract_digest: string;
    corpus_digest: string;
    required_episode_count: number;
    observed_episode_count: number;
    allowed_disposition_delta: number;
    status: string;
    started_at: string;
    completed_at?: string | null;
    comparison?: {
      policy_delta: number;
      replay_flip_rate: number;
      stability_score: number;
      failures: string[];
    } | null;
  }>;
};

export type GraphView = {
  scope: Scope;
  as_of: string | null;
  nodes: GraphNode[];
  edges: GraphEdge[];
  relationship_count: number;
  memory_type_distribution: Record<string, number>;
  status_distribution: Record<string, number>;
  context_visible_relationship_count: number;
  demoted_relationship_count: number;
  rollup_relationship_count: number;
};

export type GraphNode = {
  id: string;
  label: string;
  node_type: 'entity' | 'memory' | string;
  scope?: Scope | null;
  graph_uuid?: string | null;
  relationship_uuid?: string | null;
  relationship_type?: string | null;
  memory_type?: string | null;
  status?: string | null;
  active_in_context?: boolean;
  confidence?: number | null;
  observed_count?: number;
  valid_from?: string | null;
  valid_to?: string | null;
  labels?: string[];
  properties: Record<string, unknown>;
};

export type GraphEdge = {
  id: string;
  source_id: string;
  target_id: string;
  edge_type: string;
  label: string;
  relationship_uuid?: string | null;
};

export type EvidenceEpisode = {
  uuid: string;
  name: string;
  body: string;
  source: string;
  source_description: string;
  scope: Scope;
  reference_time: string;
  created_at: string;
  metadata: Record<string, unknown>;
  instruction_set: string;
};

export type MemoryEvidence = {
  relationship_uuid: string;
  relationship_type: string;
  fact: string;
  scope: Scope;
  subject: string;
  predicate: string;
  object: string;
  confidence: number;
  status: string;
  valid_from?: string | null;
  valid_to?: string | null;
  episode_uuids: string[];
  observed_count: number;
  created_by: string;
  source_text?: string | null;
  metadata: Record<string, unknown>;
  episodes: EvidenceEpisode[];
};

export type TimelineEntry = {
  relationship_uuid: string;
  relationship_type: string;
  fact: string;
  subject: string;
  predicate: string;
  object: string;
  confidence: number;
  status: string;
  is_current: boolean;
  valid_from?: string | null;
  valid_to?: string | null;
  superseded_by_relationship_uuid?: string | null;
  pruned_reason?: string | null;
};

export type EvolutionProof = {
  scope: Scope;
  as_of: string;
  episode_count: number;
  processed_episode_count: number;
  pending_episode_count: number;
  dream_run_count: number;
  decision_count: number;
  active_relationship_count: number;
  inactive_relationship_count: number;
  context_visible_relationship_count: number;
  rollup_relationship_count: number;
  demoted_relationship_count: number;
  compression_ratio: number;
  semantic_dedup_rate: number;
  per_type_active_counts: Record<string, number>;
  /** Estimator tokens over the scope's raw episode bodies -- the no-memory baseline. */
  tokens_raw_episodes: number;
  /** Estimator tokens if every context-visible fact were injected, unbudgeted. */
  tokens_unbudgeted_facts: number;
  /** Estimator tokens of the actual default profile render for this scope. */
  tokens_rendered_profile: number;
  /** max(0, tokens_raw_episodes - tokens_rendered_profile). */
  tokens_saved_vs_raw: number;
  /** Tokens the two-tier read saves by demoting rollup members / duplicates. */
  tokens_saved_by_demotion: number;
  /** null means "not measured" (no digest-stamped searches); 0 is a real observation. */
  repeat_search_rate: number | null;
  /** null means "not measured" (no resolvable citations); 0 is a real observation. */
  answered_from_profile_rate: number | null;
  active_facts: EvolutionFact[];
  inactive_facts: EvolutionFact[];
  signals: EvolutionSignal[];
};

export type EvolutionFact = {
  relationship_uuid: string;
  relationship_type: string;
  fact: string;
  status: string;
  memory_type?: string | null;
  valid_from?: string | null;
  valid_to?: string | null;
  observed_count: number;
  confidence: number;
};

export type EvolutionSignal = {
  name: string;
  observed: boolean;
  count: number;
  evidence: string;
};

export type DreamRun = {
  uuid: string;
  ran_at: string;
  job_name: string;
  job_kind: string;
  processed_episodes: number;
  created_relationships: number;
  reinforced_relationships: number;
  superseded_relationships: number;
  /** Rows this run PRUNED. The server has always sent this; the table only
   * rendered Created and Superseded, so a pruning run that removed 7 rows
   * displayed as all zeros -- a job that did real work looked idle. */
  pruned_relationships: number;
  /** Decisions the dream agent recorded. A run can legitimately create
   * nothing and still have decided something: a consolidation gated by
   * Motive (ROLLUP not in allowed_memory_types) writes
   * `consolidation_motive_rollup_gated` and creates zero rows. Without this
   * column, "gated on purpose" and "found nothing" are indistinguishable. */
  decision_count: number;
  /** The Motive that governed this run, joined from the receipt ledger at
   * read time (DreamJobRunRecord itself carries no Motive attribution).
   * Absent for runs that predate the receipt ledger or ran a non-formation
   * job kind -- render "unrecorded", never a guess from current policy. */
  motive_name?: string | null;
};

export type DreamDecision = {
  uuid: string;
  ran_at: string;
  job_name: string;
  job_kind: string;
  agent_id?: string | null;
  decision_type: string;
  summary: string;
};

export type DreamRunsResponse = {
  runs: DreamRun[];
  decisions: DreamDecision[];
  statuses: Array<Record<string, unknown>>;
};

export type ArchiveResponse = {
  facts: Array<GraphNode & { fact: string }>;
};

export type ControlPlaneResponse = {
  principal: Record<string, unknown>;
  policy: {
    source_trace: Record<string, string>;
    [key: string]: unknown;
  };
};
