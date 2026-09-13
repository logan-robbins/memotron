import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { MAX_ZOOM, MIN_ZOOM, labelColor } from './graphView';
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
  ProjectMemoryStatus,
  Scope,
  TenantConfig,
  TenantPrompts,
  TimelineEntry
} from './types';

const scope: Scope = {
  key: 'tenant:local-platform',
  kind: 'tenant',
  scope_id: 'local-platform',
  relationship_count: 1
};

const graph: GraphView = {
  scope,
  as_of: null,
  relationship_count: 1,
  memory_type_distribution: { requirement: 1 },
  status_distribution: { active: 1 },
  context_visible_relationship_count: 1,
  demoted_relationship_count: 0,
  rollup_relationship_count: 0,
  nodes: [
    {
      id: 'entity:subject',
      label: 'Consumer App',
      node_type: 'entity',
      properties: {}
    },
    {
      id: 'memory:fact-1',
      label: 'Consumer App requires memory_start before prior context',
      node_type: 'memory',
      relationship_uuid: 'fact-1',
      relationship_type: 'REQUIRES',
      memory_type: 'requirement',
      status: 'active',
      active_in_context: true,
      confidence: 0.93,
      observed_count: 2,
      properties: {
        subject: 'Consumer App',
        predicate: 'requires',
        object: 'memory_start before prior context'
      }
    },
    {
      id: 'entity:object',
      label: 'memory_start before prior context',
      node_type: 'entity',
      properties: {}
    }
  ],
  edges: [
    {
      id: 'edge:fact-1:subject',
      source_id: 'entity:subject',
      target_id: 'memory:fact-1',
      edge_type: 'subject',
      label: 'subject',
      relationship_uuid: 'fact-1'
    },
    {
      id: 'edge:fact-1:object',
      source_id: 'memory:fact-1',
      target_id: 'entity:object',
      edge_type: 'object',
      label: 'requires',
      relationship_uuid: 'fact-1'
    }
  ]
};

// Two components for the ego-network focus tests: fact-1's component, plus a
// second fact chained off entity:object with its own far-away entity.  From
// memory:fact-1 -- 1 hop: subject + object entities; 2 hops: adds fact-2;
// entity:island stays 3 hops out and must never appear while focused.
/**
 * A synthetic graph of `factCount` facts in the server's real shape:
 * entity -> fact -> entity, one subject edge and one object edge each, plus a
 * hub entity every fact also hangs off so the result is one connected component
 * with a realistic degree spread rather than a pile of isolated triples.
 */
function syntheticGraph(factCount: number): GraphView {
  const nodes: GraphView['nodes'] = [
    { id: 'entity:hub', label: 'Hub Entity', node_type: 'entity', labels: ['Entity', 'Customer'], properties: { name: 'Hub Entity' } }
  ];
  const edges: GraphView['edges'] = [];
  const types = ['requirement', 'decision', 'preference', 'incident', 'state', 'rollup', 'anchor', 'directive'];
  for (let index = 0; index < factCount; index += 1) {
    const memoryType = types[index % types.length];
    nodes.push({
      id: `entity:object-${index}`,
      label: `Object entity number ${index}`,
      node_type: 'entity',
      labels: ['Entity'],
      properties: { name: `Object entity number ${index}` }
    });
    nodes.push({
      id: `memory:fact-${index}`,
      label: `Hub Entity requires object entity number ${index} for compliance reasons`,
      node_type: 'memory',
      relationship_uuid: `fact-${index}`,
      relationship_type: 'REQUIRES',
      memory_type: memoryType,
      status: 'active',
      active_in_context: true,
      confidence: 0.8,
      observed_count: 1,
      properties: { subject: 'Hub Entity', predicate: 'requires', object: `Object entity number ${index}` }
    });
    edges.push({
      id: `edge:fact-${index}:subject`,
      source_id: 'entity:hub',
      target_id: `memory:fact-${index}`,
      edge_type: 'subject',
      label: 'subject',
      relationship_uuid: `fact-${index}`
    });
    edges.push({
      id: `edge:fact-${index}:object`,
      source_id: `memory:fact-${index}`,
      target_id: `entity:object-${index}`,
      edge_type: 'object',
      label: 'requires',
      relationship_uuid: `fact-${index}`
    });
  }
  return {
    ...graph,
    relationship_count: factCount,
    context_visible_relationship_count: factCount,
    nodes,
    edges
  };
}

const islandGraph: GraphView = {
  ...graph,
  relationship_count: 2,
  nodes: [
    ...graph.nodes,
    {
      id: 'memory:fact-2',
      label: 'Island fact',
      node_type: 'memory',
      relationship_uuid: 'fact-2',
      relationship_type: 'RELATES_TO',
      memory_type: 'state',
      status: 'active',
      active_in_context: true,
      confidence: 0.7,
      observed_count: 1,
      properties: {
        subject: 'memory_start before prior context',
        predicate: 'relates to',
        object: 'Lonely entity'
      }
    },
    {
      id: 'entity:island',
      label: 'Lonely entity',
      node_type: 'entity',
      properties: {}
    }
  ],
  edges: [
    ...graph.edges,
    {
      id: 'edge:fact-2:subject',
      source_id: 'entity:object',
      target_id: 'memory:fact-2',
      edge_type: 'subject',
      label: 'subject',
      relationship_uuid: 'fact-2'
    },
    {
      id: 'edge:fact-2:object',
      source_id: 'memory:fact-2',
      target_id: 'entity:island',
      edge_type: 'object',
      label: 'relates to',
      relationship_uuid: 'fact-2'
    }
  ]
};

const evidence: MemoryEvidence = {
  relationship_uuid: 'fact-1',
  relationship_type: 'REQUIRES',
  fact: 'Consumer App requires memory_start before prior context',
  scope,
  subject: 'Consumer App',
  predicate: 'requires',
  object: 'memory_start before prior context',
  confidence: 0.93,
  status: 'active',
  episode_uuids: ['episode-1'],
  observed_count: 2,
  created_by: 'test',
  source_text: 'Memory: Consumer App requires memory_start before prior context',
  metadata: {},
  episodes: [
    {
      uuid: 'episode-1',
      name: 'session',
      body: 'Memory: Consumer App requires memory_start before prior context',
      source: 'json',
      source_description: 'test',
      scope,
      reference_time: '2026-06-01T00:00:00+00:00',
      created_at: '2026-06-01T00:00:00+00:00',
      metadata: {},
      instruction_set: 'default'
    }
  ]
};

const timeline: TimelineEntry[] = [
  {
    relationship_uuid: 'fact-1',
    relationship_type: 'REQUIRES',
    fact: 'Consumer App requires memory_start before prior context',
    subject: 'Consumer App',
    predicate: 'requires',
    object: 'memory_start before prior context',
    confidence: 0.93,
    status: 'active',
    is_current: true,
    valid_from: '2026-06-01T00:00:00+00:00'
  }
];

const evolution: EvolutionProof = {
  scope,
  as_of: '2026-06-01T00:00:00+00:00',
  episode_count: 2,
  processed_episode_count: 2,
  pending_episode_count: 0,
  dream_run_count: 1,
  decision_count: 1,
  active_relationship_count: 1,
  inactive_relationship_count: 0,
  context_visible_relationship_count: 1,
  rollup_relationship_count: 0,
  demoted_relationship_count: 0,
  compression_ratio: 2,
  semantic_dedup_rate: 0.5,
  per_type_active_counts: { requirement: 1 },
  tokens_raw_episodes: 1200,
  tokens_unbudgeted_facts: 320,
  tokens_rendered_profile: 180,
  tokens_saved_vs_raw: 1020,
  tokens_saved_by_demotion: 64,
  // null = never measured; the sibling 0 below is a real observation.
  repeat_search_rate: null,
  answered_from_profile_rate: 0,
  active_facts: [],
  inactive_facts: [],
  signals: [{ name: 'semantic_dedup', observed: true, count: 50, evidence: 'dedup' }]
};

const dreams: DreamRunsResponse = {
  runs: [
    {
      uuid: 'run-1',
      ran_at: '2026-06-01T00:00:00+00:00',
      job_name: 'formation',
      job_kind: 'formation',
      processed_episodes: 1,
      created_relationships: 1,
      reinforced_relationships: 1,
      superseded_relationships: 0,
      pruned_relationships: 0,
      decision_count: 1
    }
  ],
  decisions: [
    {
      uuid: 'decision-1',
      ran_at: '2026-06-01T00:00:00+00:00',
      job_name: 'formation',
      job_kind: 'formation',
      decision_type: 'approved',
      summary: 'accepted'
    }
  ],
  statuses: []
};

const archive: ArchiveResponse = { facts: [] };
const filterOptions: FilterOptions = {
  scope,
  options: {
    types: ['REQUIRES'],
    statuses: ['active'],
    subjects: ['Consumer App'],
    predicates: ['requires'],
    objects: ['memory_start before prior context']
  }
};
const tenantPrompts: TenantPrompts = {
  tenant_id: 'local-platform',
  active_pack: 'tenant-active',
  resolved_motive: {
    name: 'agent-memory',
    source: 'tenant',
    source_label: 'tenant default'
  },
  current: {
    key: 'library:support-memory@v1',
    kind: 'library',
    label: 'support-memory@v1 (default)',
    profile: 'support-memory',
    profile_version: 'v1',
    prompt_text: 'Dream prompt profile: support-memory@v1',
    motive_name: '',
    created_at: '',
    updated_at: '',
    active: true
  },
  versions: [],
  library: [
    {
      key: 'library:support-memory@v1',
      kind: 'library',
      label: 'support-memory@v1 (library)',
      profile: 'support-memory',
      profile_version: 'v1',
      prompt_text: 'Dream prompt profile: support-memory@v1',
      motive_name: '',
      created_at: '',
      updated_at: '',
      active: false
    }
  ],
  motives: [
    {
      name: 'agent-memory',
      goal: 'Curate durable memory for autonomous software agents.',
      allowed_memory_types: ['requirement', 'directive', 'decision'],
      prompt_profile: '',
      prompt_profile_version: '',
      dedup_threshold: 0.86,
      retrieval_budget_share: 1,
      salience_rubric: null
    },
    {
      name: 'project-memory-policy',
      goal: "Advance project goal 'Deliver the local platform safely.' by curating shared memory.",
      allowed_memory_types: ['decision', 'requirement', 'directive'],
      prompt_profile: 'support-memory',
      prompt_profile_version: 'v2',
      dedup_threshold: 0.92,
      retrieval_budget_share: null,
      salience_rubric: null
    }
  ],
  motive_evidence: {
    scope: scope.key,
    fact_count: 1,
    observed: [
      { motive_name: 'agent-memory', motive_version_digest: 'a'.repeat(64), fact_count: 1 }
    ],
    divergent: false
  }
};

// A project scope whose active project-memory policy governs formation, over facts
// that were formed under that same Motive plus one un-stamped derived rollup.
const projectScopeTenantPrompts: TenantPrompts = {
  ...tenantPrompts,
  resolved_motive: {
    name: 'project-memory-policy',
    source: 'scope',
    source_label: 'scope policy',
    scope: scope.key
  },
  motive_evidence: {
    scope: scope.key,
    fact_count: 7,
    observed: [
      {
        motive_name: 'project-memory-policy',
        motive_version_digest: '95122f6bcb7bbbdfc025b0b2d95ee323fc3d09ccdaf110b416326752ad3dbd48',
        fact_count: 6
      },
      { motive_name: '', motive_version_digest: '', fact_count: 1 }
    ],
    divergent: false
  }
};

// Policy changed after the facts formed: the console must show both, not one.
const divergentTenantPrompts: TenantPrompts = {
  ...tenantPrompts,
  resolved_motive: {
    name: 'project-memory-policy',
    source: 'scope',
    source_label: 'scope policy',
    scope: scope.key
  },
  motive_evidence: {
    scope: scope.key,
    fact_count: 3,
    observed: [
      { motive_name: 'agent-memory', motive_version_digest: 'b'.repeat(64), fact_count: 3 }
    ],
    divergent: true
  }
};
const savedTenantPrompts: TenantPrompts = {
  ...tenantPrompts,
  current: {
    key: 'tenant:tenant-v1',
    kind: 'tenant',
    label: 'tenant-v1 (active)',
    version: 'tenant-v1',
    profile: 'support-memory',
    profile_version: 'v1',
    prompt_text: 'Dream prompt profile: support-memory@v1\nRemember JedAI Platform operating decisions.',
    motive_name: '',
    created_at: '2026-06-01T00:00:00+00:00',
    updated_at: '2026-06-01T00:00:00+00:00',
    active: true
  },
  versions: [
    {
      key: 'tenant:tenant-v1',
      kind: 'tenant',
      label: 'tenant-v1 (saved)',
      version: 'tenant-v1',
      profile: 'support-memory',
      profile_version: 'v1',
      prompt_text: 'Dream prompt profile: support-memory@v1\nRemember JedAI Platform operating decisions.',
      motive_name: '',
      created_at: '2026-06-01T00:00:00+00:00',
      updated_at: '2026-06-01T00:00:00+00:00',
      active: true
    }
  ]
};
const projectMemoryStatus: ProjectMemoryStatus = {
  tenant_id: 'local-platform',
  project_scope: scope,
  configured: true,
  config: {
    tenant_id: 'local-platform',
    version: 'project-v1',
    project_goal: 'Deliver the local platform safely.',
    memory_goal: 'Keep durable cross-agent decisions and incidents.',
    keep: ['Decisions that affect multiple project agents.'],
    exclude: ['Personal scratch work.'],
    rules: ['Preserve source attribution.'],
    allowed_memory_types: ['requirement', 'decision', 'incident'],
    protected_memory_types: ['requirement', 'decision', 'incident'],
    min_salience: 0.2,
    max_memories_per_candidate: 8,
    dedup_threshold: 0.9,
    configured_by: 'project-owner',
    active: true,
    created_at: '2026-06-01T00:00:00+00:00',
    updated_at: '2026-06-01T00:00:00+00:00'
  },
  versions: [],
  candidate_count: 4,
  pending_candidate_count: 1
};
projectMemoryStatus.versions = [projectMemoryStatus.config!];
const savedProjectMemoryConfig = {
  ...projectMemoryStatus.config!,
  version: 'project-v2',
  project_goal: 'Deliver the local platform with governed shared memory.',
  configured_by: 'admin-ui'
};
const dreamStatus: DreamSequenceStatus = {
  run_id: 'dream-sequence-1',
  status: 'completed',
  running: false,
  scope,
  agent_id: 'consumer-app',
  motive_name: 'agent-memory',
  motive_source: 'tenant',
  motive_source_label: 'tenant default',
  started_at: '2026-06-01T00:00:00+00:00',
  completed_at: '2026-06-01T00:00:01+00:00',
  capability_signals: [
    {
      name: 'Motive policy',
      description: 'A named Motive chooses the memory-making goal.'
    }
  ],
  events: [
    {
      at: '2026-06-01T00:00:00+00:00',
      phase: 'policy',
      message: 'Resolving tenant policy, active prompt pack, and Motive.',
      details: {}
    }
  ],
  result: {
    ran_at: '2026-06-01T00:00:00+00:00',
    job_runs: [
      {
        job_name: 'formation',
        processed_episodes: 1,
        created_relationships: 1,
        reinforced_relationships: 0,
        superseded_relationships: 0
      }
    ]
  }
};
const controlPlane: ControlPlaneResponse = {
  principal: {},
  policy: {
    source_trace: {
      tenant: 'tenant:local-platform',
      scope: 'principal_allowed_scope'
    }
  }
};

// Launch-sensitivity switches: an admin server launched without --mcp-url, or
// against a graph whose tenants do not include the launch tenant id.
let launchMcpUrl = 'http://127.0.0.1:8010/mcp';
let launchTenantWarnings: string[] = [];

// Workload switches for the Overview cards: the same screen serves a
// conversational tenant (episodes collapse into facts) and a knowledge-base
// tenant (sections expand into facts), and must read correctly for both.
let memoryOverride: Partial<Overview['memory']> = {};
let proofOverride: Partial<EvolutionProof> = {};

function tenantConfig(hasKey: boolean): TenantConfig {
  return {
    tenant: {
      tenant_id: 'local-platform',
      agent_ids: ['consumer-app'],
      example_agent_id: 'consumer-app',
      default_scope: scope.key,
      warnings: launchTenantWarnings
    },
    operator: {
      graph_path: '/tmp/local.sqlite',
      platform_api_url: 'http://127.0.0.1:8765/',
      ui_url: 'http://127.0.0.1:8765/',
      mcp_url: launchMcpUrl,
      mcp_configured: Boolean(launchMcpUrl),
      integration_contract_url: 'http://127.0.0.1:8765/api/platform/integration-contract'
    },
    integration: {
      contract_url: 'http://127.0.0.1:8765/api/platform/integration-contract',
      labels: [
        'No seeding required',
        'Register before memory tools',
        'Agent IDs and names are tenant-unique',
        'Motive resolves automatically'
      ]
    },
    llm: hasKey
      ? {
          tenant_id: 'local-platform',
          has_api_key: true,
          provider: 'litellm',
          model: 'claude-sonnet-4-6',
          updated_at: '2026-06-01T00:00:00+00:00'
        }
      : { tenant_id: 'local-platform', has_api_key: false },
    snippets: {
      sdk: [
        'from memotron import MemotronPlatformClient',
        "memory = MemotronPlatformClient(base_url='http://127.0.0.1:8765/', agent_id='consumer-app', agent_name='Consumer Agent')",
        'registration = await memory.agent_register()',
        'start = await memory.memory_start()',
        'prior = await memory.memory_search(query="prior decisions or requirements")',
        'project_policy = await memory.project_memory_config()',
        'remembered = await memory.memory_remember()',
        'published = await memory.memory_publish()',
        'logged = await memory.memory_log(summary="Session completed with reusable learning.")',
        'refreshed = await memory.memory_refresh()',
        'Motive resolves automatically unless explicitly overridden.'
      ].join('\n'),
      mcp: [
        "async with Client('http://127.0.0.1:8010/mcp') as session:",
        'await session.call_tool("memory_contract", {})',
        'await session.call_tool("agent_register", {"agent_id": "consumer-app", "agent_name": "Consumer Agent"})',
        'await session.call_tool("memory_start", {"agent_id": "consumer-app"})',
        'await session.call_tool("memory_search", {"query": "prior decisions or requirements"})',
        'await session.call_tool("project_memory_config", {})',
        'await session.call_tool("memory_remember", {})',
        'await session.call_tool("memory_publish", {})',
        'await session.call_tool("memory_log", {"summary": "Session completed with reusable learning."})',
        'await session.call_tool("memory_refresh", {"agent_id": "consumer-app"})',
        'Motive resolves automatically unless dream policy is overridden.'
      ].join('\n'),
      codex_mcp_config: [
        '[mcp_servers.memotron_agent_memory]',
        "url = 'http://127.0.0.1:8010/mcp'",
        'required = true',
        'tool_timeout_sec = 120',
        'approve Memotron memory tools automatically'
      ].join('\n')
    }
  };
}

function overview(hasKey: boolean): Overview {
  const config = tenantConfig(hasKey);
  return {
    tenant: config.tenant,
    operator: config.operator,
    llm: config.llm,
    readiness: {
      llm_configured: hasKey,
      agents_observed: true,
      scopes_registered: true,
      platform_api_ready: true,
      mcp_ready: Boolean(launchMcpUrl)
    },
    scope,
    scopes: [scope],
    memory: {
      visible_facts: 1,
      active_facts: 1,
      inactive_facts: 0,
      episodes: 2,
      pending_episodes: 0,
      compression_ratio: 2,
      semantic_dedup_rate: 0.5,
      rollups: 0,
      demoted: 0,
      per_type_active_counts: { requirement: 1 },
      ...memoryOverride
    },
    evolution: {
      dream_run_count: 1,
      decision_count: 1,
      latest_dream_run: dreams.runs[0],
      latest_decision: dreams.decisions[0],
      signals: evolution.signals
    }
  };
}

const policyRollout: PolicyRolloutStatus = {
  scope,
  alias: 'production',
  active_alias: {
    scope,
    alias: 'production',
    contract_digest: 'a'.repeat(64),
    previous_contract_digest: 'b'.repeat(64),
    updated_at: '2026-06-01T00:00:00+00:00'
  },
  compiled_effective_policy: {
    contract_digest: 'a'.repeat(64),
    motive: {
      name: 'learn-compliance-requirements',
      goal: 'retain compliance requirements',
      allowed_memory_types: ['requirement']
    },
    replay_options: {
      model_identifier: 'rule-based@v1',
      temperature: 0,
      repetitions: 3,
      minimum_stability_score: 1
    },
    source_trace: { change: 'requirement policy candidate' },
    certification_passed: true,
    staged_at: '2026-06-01T00:00:00+00:00'
  },
  expected_episode_routing: {
    production_scope: scope.key,
    motive: 'learn-compliance-requirements',
    allowed_memory_types: ['requirement'],
    shadow_visibility: 'zero user visibility; isolated replay stores only'
  },
  certification: {
    passed: true,
    corpus_name: 'fixture-corpus',
    corpus_digest: 'c'.repeat(64),
    policy_delta: 0.5,
    replay_flip_rate: 0,
    stability_score: 1,
    failures: [],
    protected_invariants: [{ name: 'protected_retention', passed: true, detail: 'ok' }]
  },
  shadow_stages: [
    {
      stage_id: 'stage-1',
      candidate_contract_digest: 'd'.repeat(64),
      corpus_digest: 'e'.repeat(64),
      required_episode_count: 5,
      observed_episode_count: 5,
      allowed_disposition_delta: 0,
      status: 'passed',
      started_at: '2026-06-01T00:00:00+00:00',
      completed_at: '2026-06-01T00:01:00+00:00',
      comparison: { policy_delta: 0, replay_flip_rate: 0, stability_score: 1, failures: [] }
    }
  ]
};

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: init.status ?? 200,
    headers: { 'Content-Type': 'application/json' }
  });
}

describe('Memotron admin UI', () => {
  let storedKey = false;
  let promptsPayload: TenantPrompts = tenantPrompts;
  let graphPayload: GraphView = graph;
  let archivePayload: ArchiveResponse = archive;
  let fetchMock: ReturnType<typeof vi.fn>;
  let clipboardWrite: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    storedKey = false;
    promptsPayload = tenantPrompts;
    graphPayload = graph;
    archivePayload = archive;
    launchMcpUrl = 'http://127.0.0.1:8010/mcp';
    launchTenantWarnings = [];
    memoryOverride = {};
    proofOverride = {};
    clipboardWrite = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: {
        writeText: clipboardWrite
      },
      configurable: true
    });
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://127.0.0.1:8765');
      if (url.pathname === '/api/overview') return jsonResponse(overview(storedKey));
      if (url.pathname === '/api/scopes') return jsonResponse({ scopes: [scope] });
      if (url.pathname === '/api/tenant-config' && !init?.method) return jsonResponse(tenantConfig(storedKey));
      if (url.pathname === '/api/filter-options') return jsonResponse(filterOptions);
      if (url.pathname === '/api/tenant-prompts' && !init?.method) return jsonResponse(promptsPayload);
      if (url.pathname === '/api/tenant-prompts') return jsonResponse(savedTenantPrompts);
      if (url.pathname === '/api/platform/project-memory/config' && !init?.method) {
        return jsonResponse(projectMemoryStatus);
      }
      if (url.pathname === '/api/platform/project-memory/config') {
        return jsonResponse(savedProjectMemoryConfig);
      }
      if (url.pathname === '/api/dream-sequence/run') return jsonResponse({ ...dreamStatus, running: true, status: 'running' });
      if (url.pathname === '/api/dream-sequence/status') return jsonResponse(dreamStatus);
      if (url.pathname === '/api/tenant-config/llm') {
        storedKey = true;
        return jsonResponse(tenantConfig(true));
      }
      if (url.pathname === '/api/tenant-config/llm/clear') {
        storedKey = false;
        return jsonResponse({ cleared: true, config: tenantConfig(false) });
      }
      if (url.pathname === '/api/tenant-config/purge') {
        return jsonResponse({
          tenant_id: 'local-platform',
          purged: {
            tenant_id: 'local-platform',
            agent_ids: ['consumer-app'],
            scope_keys: ['tenant:local-platform', 'agent:consumer-app'],
            raw_episodes_preserved: 2,
            credentials_preserved: storedKey,
            processed_episodes_reset: 2,
            relationships_deleted: 1,
            nodes_deleted: 3,
            dream_decisions_deleted: 1,
            memory_receipts_deleted: 1,
            run_checkpoints_deleted: 1,
            dream_job_runs_deleted: 1,
            job_state_deleted: 1,
            tenant_agents_deleted: 1,
            tenant_prompt_overrides_deleted: 0,
            tenant_prompt_versions_deleted: 1,
            project_memory_config_versions_deleted: 1
          },
          config: tenantConfig(storedKey),
          prompts: tenantPrompts
        });
      }
      if (url.pathname === '/api/graph') return jsonResponse(graphPayload);
      if (url.pathname === '/api/evidence') return jsonResponse(evidence);
      if (url.pathname === '/api/timeline') return jsonResponse({ entries: timeline });
      if (url.pathname === '/api/evolution') return jsonResponse({ proof: { ...evolution, ...proofOverride } });
      if (url.pathname === '/api/dream-runs') return jsonResponse(dreams);
      if (url.pathname === '/api/archive') return jsonResponse(archivePayload);
      if (url.pathname === '/api/control-plane') return jsonResponse(controlPlane);
      if (url.pathname === '/api/policy-rollout') return jsonResponse(policyRollout);
      return jsonResponse({ error: 'not found' }, { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders overview readiness cards', async () => {
    render(<App />);

    expect(await screen.findByText('Visible facts')).toBeInTheDocument();
    expect(screen.getByText('Rollups')).toBeInTheDocument();
    expect(screen.getAllByText('Platform API').length).toBeGreaterThan(0);
    expect(screen.getAllByText('local-platform').length).toBeGreaterThan(0);
  });

  it('leads the overview with token economics from the evolution proof', async () => {
    render(<App />);

    await screen.findByText('180 / 1,200');
    const tokenCard = screen.getByText('Context tokens').closest('.metric') as HTMLElement;
    expect(within(tokenCard).getByText('180 / 1,200')).toBeInTheDocument();
    expect(within(tokenCard).getByText('rendered profile / raw episodes')).toBeInTheDocument();

    const savedCard = screen.getByText('Tokens saved').closest('.metric') as HTMLElement;
    expect(within(savedCard).getByText('1,020')).toBeInTheDocument();
    expect(within(savedCard).getByText('85% vs. re-reading raw')).toBeInTheDocument();

    // Demotion is tied back to the tokens it buys, not left as a bare count.
    const demotedCard = screen.getByText('Demoted').closest('.metric') as HTMLElement;
    expect(within(demotedCard).getByText('64 tokens saved by demotion')).toBeInTheDocument();

    const wholeSetCard = screen.getByText('Whole working set').closest('.metric') as HTMLElement;
    expect(within(wholeSetCard).getByText('320')).toBeInTheDocument();
  });

  it('states a compression workload as episodes per fact', async () => {
    render(<App />);

    const shapeCard = (await screen.findByText('Episodes → facts')).closest('.metric') as HTMLElement;
    expect(within(shapeCard).getByText('2 → 1')).toBeInTheDocument();
    expect(within(shapeCard).getByText('2.0 episodes per fact (compression)')).toBeInTheDocument();
  });

  it('states an expansion workload as facts per episode, never a sub-1 ratio', async () => {
    // Knowledge-base tenant: 39 doc sections expand into 139 extracted facts.
    // compression_ratio is 0.28 here, which reads as failure -- it must not ship.
    memoryOverride = {
      visible_facts: 139,
      active_facts: 166,
      inactive_facts: 4,
      episodes: 39,
      compression_ratio: 0.2805755395683453,
      semantic_dedup_rate: 0.017341040462427744,
      rollups: 5,
      demoted: 27,
      per_type_active_counts: { anchor: 120, requirement: 9, state: 5, rollup: 5 }
    };
    proofOverride = {
      tokens_raw_episodes: 13311,
      tokens_unbudgeted_facts: 4261,
      tokens_rendered_profile: 842,
      tokens_saved_vs_raw: 12469,
      tokens_saved_by_demotion: 471,
      repeat_search_rate: null,
      answered_from_profile_rate: null
    };
    render(<App />);

    const shapeCard = (await screen.findByText('Episodes → facts')).closest('.metric') as HTMLElement;
    expect(within(shapeCard).getByText('39 → 139')).toBeInTheDocument();
    expect(within(shapeCard).getByText('3.6 facts per episode (expansion)')).toBeInTheDocument();
    expect(screen.queryByText('0.28x')).not.toBeInTheDocument();
    expect(screen.queryByText(/0\.28/)).not.toBeInTheDocument();

    await waitFor(() =>
      expect(
        within(screen.getByText('Context tokens').closest('.metric') as HTMLElement).getByText(
          '842 / 13,311'
        )
      ).toBeInTheDocument()
    );
    const savedCard = screen.getByText('Tokens saved').closest('.metric') as HTMLElement;
    expect(within(savedCard).getByText('12,469')).toBeInTheDocument();
    expect(within(savedCard).getByText('94% vs. re-reading raw')).toBeInTheDocument();
  });

  it('gives a low dedup rate the type-skew context that explains it', async () => {
    memoryOverride = {
      visible_facts: 139,
      episodes: 39,
      semantic_dedup_rate: 0.017341040462427744,
      per_type_active_counts: { anchor: 120, requirement: 9, state: 5, rollup: 5 }
    };
    render(<App />);

    const dedupCard = (await screen.findByText('Semantic dedup')).closest('.metric') as HTMLElement;
    expect(within(dedupCard).getByText('2%')).toBeInTheDocument();
    expect(within(dedupCard).getByText('anchor is 86% of visible facts')).toBeInTheDocument();
  });

  it('separates an unmeasured rate from a measured zero', async () => {
    render(<App />);

    // repeat_search_rate is null (no digest-stamped searches ever ran).
    await screen.findByText('not yet measured');
    const repeatCard = screen.getByText('Repeat search').closest('.metric') as HTMLElement;
    const repeatValue = within(repeatCard).getByText('not yet measured');
    expect(repeatValue).toBeInTheDocument();
    expect(repeatValue).toHaveClass('unmeasured');

    // answered_from_profile_rate is 0.0 -- a real observation, shown as 0%.
    const profileCard = screen.getByText('Answered from profile').closest('.metric') as HTMLElement;
    const profileValue = within(profileCard).getByText('0%');
    expect(profileValue).toBeInTheDocument();
    expect(profileValue).not.toHaveClass('unmeasured');
  });

  it('shows both telemetry rates as unmeasured when neither has a denominator', async () => {
    proofOverride = { repeat_search_rate: null, answered_from_profile_rate: null };
    render(<App />);

    await screen.findByText('Repeat search');
    await waitFor(() => expect(screen.getAllByText('not yet measured')).toHaveLength(2));
    expect(screen.queryByText('—')).not.toBeInTheDocument();
  });

  it('saves tenant credentials and keeps the raw key out of status', async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Tenant Setup' }));
    await user.type(screen.getByLabelText('API key'), 'secret-test-key');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(screen.getByText('stored')).toBeInTheDocument());
    expect(screen.queryByDisplayValue('secret-test-key')).not.toBeInTheDocument();
    expect(screen.queryByText('secret-test-key')).not.toBeInTheDocument();
  });

  it('purges tenant generated state and reloads tenant scope data', async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Tenant Setup' }));
    await screen.findByText('Tenant state');
    await user.click(screen.getByRole('button', { name: 'Purge generated state' }));

    await waitFor(() => expect(screen.getByText('Purged; preserved 2 raw episodes')).toBeInTheDocument());
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('Raw episodes and LLM credentials are preserved'));
    const purgeCall = fetchMock.mock.calls.find(([url]) => url === '/api/tenant-config/purge');
    expect(purgeCall).toBeDefined();
    expect(JSON.parse(String(purgeCall?.[1]?.body))).toEqual({});
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([url]) => String(url).startsWith('/api/graph'))).toBe(true);
    });
  });

  it('renders memory filters and table state', async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Memory Explorer' }));
    await screen.findByText('Scope graph');
    await user.type(screen.getByPlaceholderText('subject, predicate, object'), 'memory_start');
    expect(await screen.findByRole('listbox', { name: 'Smart search suggestions' })).toBeInTheDocument();
    expect(screen.getByText('Object')).toBeInTheDocument();
    expect(screen.getAllByText('memory_start before prior context').length).toBeGreaterThan(0);
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('listbox', { name: 'Smart search suggestions' })).not.toBeInTheDocument());
    await user.click(screen.getByPlaceholderText('subject, predicate, object'));
    expect(await screen.findByRole('listbox', { name: 'Smart search suggestions' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Close search suggestions' }));
    await waitFor(() => expect(screen.queryByRole('listbox', { name: 'Smart search suggestions' })).not.toBeInTheDocument());
    // The fact is no longer its own node -- the canvas collapses
    // entity--subject-->fact--object-->entity into one entity->entity edge
    // captioned with the predicate, so this is now an edge click.
    const graphEdge = screen.getByRole('button', { name: 'Select fact: requires' });
    await user.click(graphEdge);
    expect(screen.getByRole('button', { name: 'Open in Explainability' })).toBeEnabled();
    expect(screen.getAllByText('active').length).toBeGreaterThan(0);
    await user.click(screen.getByRole('button', { name: 'Table' }));

    // The table renders the relationship as a structured triple -- subject,
    // predicate, object each their own element -- not the bare `fact`
    // sentence standing in for a third entity. Scoped to the graph panel's
    // own table because the Inspector beside it (which also has a table, for
    // node properties) renders the same selected fact's triple too.
    const graphPanel = screen.getByText('Scope graph').closest('.graph-panel') as HTMLElement;
    const table = within(graphPanel).getByRole('table');
    expect(within(table).getByText('Consumer App')).toBeInTheDocument();
    expect(within(table).getByText('requires')).toBeInTheDocument();
    expect(within(table).getByText('memory_start before prior context')).toBeInTheDocument();
    // This fixture's `fact` is a pure restatement of subject+predicate+object
    // (no qualifier beyond them), so it is suppressed rather than printed a
    // second time next to the triple.
    expect(
      within(table).queryByText('Consumer App requires memory_start before prior context')
    ).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open' })).toBeInTheDocument();
  });

  it('colours a relationship\'s two entities by their graph label, and keeps a fact sentence that carries more than the triple', async () => {
    const kbScope: Scope = { ...scope, relationship_count: 1 };
    graphPayload = {
      ...graph,
      nodes: [
        {
          id: 'entity:kb',
          label: 'Jedai Knowledge Base',
          node_type: 'entity',
          labels: ['Component'],
          properties: { name: 'Jedai Knowledge Base' }
        },
        {
          id: 'memory:ingest-fact',
          label: 'Jedai Knowledge Base ingests Special Offers nightly at 02:00 UTC',
          node_type: 'memory',
          relationship_uuid: 'ingest-fact',
          relationship_type: 'INGESTS',
          memory_type: 'state',
          status: 'active',
          active_in_context: true,
          confidence: 0.9,
          observed_count: 4,
          properties: {
            subject: 'Jedai Knowledge Base',
            predicate: 'ingests',
            object: 'Special Offers',
            fact: 'Jedai Knowledge Base ingests Special Offers nightly at 02:00 UTC'
          }
        },
        {
          id: 'entity:offers',
          label: 'Special Offers',
          node_type: 'entity',
          labels: ['Resource'],
          properties: { name: 'Special Offers' }
        }
      ],
      edges: [
        {
          id: 'edge:ingest-fact:subject',
          source_id: 'entity:kb',
          target_id: 'memory:ingest-fact',
          edge_type: 'subject',
          label: 'subject',
          relationship_uuid: 'ingest-fact'
        },
        {
          id: 'edge:ingest-fact:object',
          source_id: 'memory:ingest-fact',
          target_id: 'entity:offers',
          edge_type: 'object',
          label: 'ingests',
          relationship_uuid: 'ingest-fact'
        }
      ],
      scope: kbScope
    };
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Memory Explorer' }));
    await screen.findByText('Scope graph');
    await user.click(screen.getByRole('button', { name: 'Table' }));

    const graphPanel = screen.getByText('Scope graph').closest('.graph-panel') as HTMLElement;
    const table = within(graphPanel).getByRole('table');

    // The triple is unmistakable from an entity row: each endpoint carries
    // its entity-TYPE chip, coloured the same way the graph canvas colours
    // that label (both call the same hashed `labelColor`).
    const componentChip = within(table).getByText('Component');
    const resourceChip = within(table).getByText('Resource');
    expect(componentChip).toHaveStyle({ background: labelColor('Component') });
    expect(resourceChip).toHaveStyle({ background: labelColor('Resource') });
    expect(within(table).getByText('Jedai Knowledge Base')).toBeInTheDocument();
    expect(within(table).getByText('Special Offers')).toBeInTheDocument();
    expect(within(table).getByText('ingests')).toBeInTheDocument();

    // This fact sentence carries a qualifier ("nightly at 02:00 UTC") the
    // triple does not, so -- unlike the redundant-restatement case above --
    // it still renders.
    expect(
      within(table).getByText('Jedai Knowledge Base ingests Special Offers nightly at 02:00 UTC')
    ).toBeInTheDocument();
  });

  it('renders the Memory Explorer table on an empty scope without crashing', async () => {
    graphPayload = { ...graph, relationship_count: 0, context_visible_relationship_count: 0, nodes: [], edges: [] };
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Memory Explorer' }));
    await screen.findByText('Scope graph');
    await user.click(screen.getByRole('button', { name: 'Table' }));

    const graphPanel = screen.getByText('Scope graph').closest('.graph-panel') as HTMLElement;
    expect(within(graphPanel).getByRole('table')).toBeInTheDocument();
    expect(within(graphPanel).queryAllByRole('row').length).toBe(1); // header row only
  });

  it('renders an archive relationship as a triple, degraded to plain names when the payload has no entity nodes', async () => {
    archivePayload = {
      facts: [
        {
          id: 'memory:superseded-fact',
          label: 'Special Offers was backed by kb_ds_source_dscribe_special_offers_wdw_en_us_v1',
          node_type: 'memory',
          relationship_uuid: 'superseded-fact',
          relationship_type: 'BACKED_BY',
          memory_type: 'state',
          status: 'superseded',
          active_in_context: false,
          confidence: 0.6,
          observed_count: 1,
          properties: {
            subject: 'Special Offers',
            predicate: 'backed by',
            object: 'kb_ds_source_dscribe_special_offers_wdw_en_us_v1'
          },
          fact: 'Special Offers was backed by kb_ds_source_dscribe_special_offers_wdw_en_us_v1'
        }
      ]
    };
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Dreaming' }));
    const archivePanel = (await screen.findByText('Archive')).closest('.panel') as HTMLElement;
    expect(within(archivePanel).getByText('Special Offers')).toBeInTheDocument();
    expect(within(archivePanel).getByText('backed by')).toBeInTheDocument();
    expect(
      within(archivePanel).getByText('kb_ds_source_dscribe_special_offers_wdw_en_us_v1')
    ).toBeInTheDocument();
    // No entity nodes/edges came back from /api/archive, so there is nothing
    // to colour the endpoints by -- no label-chip element, just plain text.
    expect(within(archivePanel).queryByText('Resource')).not.toBeInTheDocument();
  });

  it('memory explorer settles instead of refetching in a loop, then refetches once per deliberate change', async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Memory Explorer' }));
    await screen.findByText('Scope graph');
    const graphCalls = () =>
      fetchMock.mock.calls.filter(([url]) => String(url).startsWith('/api/graph')).length;
    const optionCalls = () =>
      fetchMock.mock.calls.filter(([url]) => String(url).startsWith('/api/filter-options')).length;
    await waitFor(() => expect(graphCalls()).toBeGreaterThan(0));

    // Let every microtask-chained effect fire; the old dependency cycle
    // (refreshExplainability -> graph -> refreshActive -> effect) refetched
    // continuously here.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 250));
    });
    const settledGraphCalls = graphCalls();
    const settledOptionCalls = optionCalls();
    expect(settledGraphCalls).toBeLessThanOrEqual(2);
    expect(settledOptionCalls).toBeLessThanOrEqual(2);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 250));
    });
    expect(graphCalls()).toBe(settledGraphCalls);
    expect(optionCalls()).toBe(settledOptionCalls);

    // A deliberate Refresh click issues exactly one more graph fetch and then
    // settles again.
    const toolbar = screen.getByText('Smart search').closest('.toolbar') as HTMLElement;
    await user.click(within(toolbar).getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(graphCalls()).toBe(settledGraphCalls + 1));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 250));
    });
    expect(graphCalls()).toBe(settledGraphCalls + 1);
  });

  it('propagates filter changes into the graph fetch params', async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Memory Explorer' }));
    await screen.findByText('Scope graph');
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => String(url).startsWith('/api/graph'))).toBe(true)
    );

    // Default statuses ride along on every graph fetch.
    expect(
      fetchMock.mock.calls.some(
        ([url]) => String(url).startsWith('/api/graph') && String(url).includes('statuses=active')
      )
    ).toBe(true);

    // Checking a type in the Types dropdown refetches with types=...
    // (fireEvent because jsdom does not implement summary-click activation,
    // so the closed <details> keeps the checkbox out of the a11y tree.)
    const typesMenu = screen.getByRole('group', { name: 'Types' });
    fireEvent.click(within(typesMenu).getByRole('checkbox', { name: /REQUIRES/, hidden: true }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([url]) => String(url).startsWith('/api/graph') && String(url).includes('types=REQUIRES')
        )
      ).toBe(true)
    );

    // Checking "Show demoted" refetches with include_demoted=true -- the
    // default is false, so this is the toggle that reveals rollup members.
    await user.click(screen.getByRole('checkbox', { name: 'Show demoted' }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([url]) =>
            String(url).startsWith('/api/graph') && String(url).includes('include_demoted=true')
        )
      ).toBe(true)
    );
  });

  it('opens the explorer on the context-visible graph, matching the overview fact count', async () => {
    graphPayload = { ...graph, relationship_count: 139, context_visible_relationship_count: 139 };
    memoryOverride = { visible_facts: 139 };
    const user = userEvent.setup();
    render(<App />);

    const visibleCard = (await screen.findByText('Visible facts')).closest('.metric') as HTMLElement;
    expect(within(visibleCard).getByText('139')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Memory Explorer' }));
    await screen.findByText('Scope graph');
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => String(url).startsWith('/api/graph'))).toBe(true)
    );

    // Every graph fetch since open leaves demoted rows out.
    const graphCalls = fetchMock.mock.calls.filter(([url]) => String(url).startsWith('/api/graph'));
    expect(graphCalls.every(([url]) => String(url).includes('include_demoted=false'))).toBe(true);
    expect(screen.getByRole('checkbox', { name: 'Show demoted' })).not.toBeChecked();

    // ...so the Explorer header agrees with the Overview's VISIBLE FACTS card.
    expect(await screen.findByText('139 facts')).toBeInTheDocument();
  });

  // Neo4j's node menu (right-click) is where Focus now lives, because
  // double-click was reassigned to Neo4j's expand. The canvas collapses each
  // fact onto an entity->entity edge, so the neighbourhood a right-click
  // focuses is now the ENTITY graph's -- Consumer App's 1-hop neighbour is
  // the entity the "requires" edge points at, not a fact node in between.
  it('the node menu focuses the local neighborhood; depth, Escape, and the chip control it', async () => {
    graphPayload = islandGraph;
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Memory Explorer' }));
    await screen.findByText('Scope graph');
    const consumerApp = await screen.findByRole('button', { name: 'Select Consumer App' });
    expect(screen.getByRole('button', { name: 'Select Lonely entity' })).toBeInTheDocument();
    const svg = screen.getByRole('img', { name: 'Memory graph' });
    const transform = () => svg.querySelector('g')?.getAttribute('transform') ?? '';
    const initialTransform = transform();

    await user.pointer({ keys: '[MouseRight]', target: consumerApp });
    await user.click(await screen.findByRole('menuitem', { name: 'Focus' }));

    // Focus chip announces the mode; focusing never leaves the explorer.
    const chip = await screen.findByRole('status');
    expect(chip).toHaveTextContent(/Focused on/);
    expect(screen.getByText('Scope graph')).toBeInTheDocument();
    expect(screen.queryByText('Evidence episodes')).not.toBeInTheDocument();

    // 1 hop: Consumer App plus the entity its "requires" edge points at;
    // everything else HIDDEN.
    expect(screen.getByRole('button', { name: 'Select Consumer App' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Select memory_start before prior context' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Select Lonely entity' })).not.toBeInTheDocument();

    // Entering focus re-fit the view to the focused subgraph.
    expect(transform()).not.toBe(initialTransform);

    // Depth 2 walks the chained "relates to" edge out to the far entity.
    await user.click(within(chip).getByRole('button', { name: '2 hops' }));
    expect(await screen.findByRole('button', { name: 'Select Lonely entity' })).toBeInTheDocument();

    // Escape exits, and the whole graph is framed again -- the view is always
    // re-fit to whatever is on screen rather than restoring a stale transform.
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Select Lonely entity' })).toBeInTheDocument();

    // The chip's exit button is an equivalent way out.
    await user.pointer({ keys: '[MouseRight]', target: screen.getByRole('button', { name: 'Select Consumer App' }) });
    await user.click(await screen.findByRole('menuitem', { name: 'Focus' }));
    await screen.findByRole('status');
    await user.click(screen.getByRole('button', { name: 'Show full graph' }));
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Select Lonely entity' })).toBeInTheDocument();
  });

  // The graph endpoint caps at 300 relationships, so ~900 nodes is the real
  // ceiling. These two cases bracket what the view has to survive; neither the
  // layout nor the caption policy may be tuned for one size.
  it('renders an empty graph as an empty state, with no controls to press', async () => {
    graphPayload = { ...graph, relationship_count: 0, context_visible_relationship_count: 0, nodes: [], edges: [] };
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Memory Explorer' }));
    expect(await screen.findByText('No matching graph facts.')).toBeInTheDocument();
    expect(screen.queryByRole('img', { name: 'Memory graph' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Fit graph to view' })).not.toBeInTheDocument();
    expect(screen.queryByRole('complementary', { name: 'Graph information' })).not.toBeInTheDocument();
  });

  it('lays out a 250-fact collapsed graph, and drops captions rather than smearing them', async () => {
    // 250 facts, each collapsed onto one entity->entity edge -> 250 object
    // entities + 1 hub = 251 nodes, 250 edges (never the 501 nodes / 500
    // structural edges the raw entity--subject-->fact--object-->entity
    // reification would draw).
    graphPayload = syntheticGraph(250);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Memory Explorer' }));
    await screen.findByText('Scope graph');
    const svg = await screen.findByRole('img', { name: 'Memory graph' });

    expect(svg.querySelectorAll('g.graph-node')).toHaveLength(251);
    expect(svg.querySelectorAll('path.graph-edge')).toHaveLength(250);
    // Past the density limit captions are suppressed, except for the hubs worth
    // naming -- so the view does not become a wall of overlapping text.
    expect(svg.querySelectorAll('g.graph-node text').length).toBeLessThan(60);
    // Every node has a finite position: no NaN escaped the simulation.
    for (const node of Array.from(svg.querySelectorAll('g.graph-node'))) {
      expect(node.getAttribute('transform')).toMatch(/^translate\(-?[\d.]+ -?[\d.]+\)$/);
    }
    // And the fit produced a usable scale rather than collapsing to a rail.
    const scale = Number(/scale\(([\d.]+)\)/.exec(svg.querySelector('g')?.getAttribute('transform') ?? '')?.[1]);
    expect(scale).toBeGreaterThan(MIN_ZOOM);
    expect(scale).toBeLessThanOrEqual(MAX_ZOOM);

    // The information panel counts the whole (collapsed) node/edge set. Node
    // Labels is entity KINDS only, one row per node's colour label -- the
    // hub's secondary "Customer" label and every fact's memory type
    // (requirement, decision, ...) are gone from this legend, not just
    // hidden: neither is a node on the canvas anymore, and "Customer" is the
    // hub's second declared label, exactly the slot the server puts a
    // scope-kind marker like "Tenant" in on real data -- the legend must
    // pick one row per node, not fan out across every label it carries.
    const info = screen.getByRole('complementary', { name: 'Graph information' });
    expect(within(info).getByRole('button', { name: 'Entity (251)' })).toBeInTheDocument();
    expect(within(info).queryByRole('button', { name: /^Customer/ })).not.toBeInTheDocument();
    expect(within(info).queryByRole('button', { name: /^requirement/ })).not.toBeInTheDocument();
    // Relationship Types is real predicates now, never the structural
    // "subject"/"object" edge_type the reification used.
    expect(within(info).getByRole('button', { name: 'requires (250)' })).toBeInTheDocument();
    expect(within(info).queryByRole('button', { name: /^subject/ })).not.toBeInTheDocument();
    expect(within(info).queryByRole('button', { name: /^object/ })).not.toBeInTheDocument();
  });

  it('double-click on the background zooms in about the pointer, d3-style', async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Memory Explorer' }));
    await screen.findByText('Scope graph');
    const svg = screen.getByRole('img', { name: 'Memory graph' });
    const transform = () => svg.querySelector('g')?.getAttribute('transform') ?? '';
    const scaleOf = () => Number(/scale\(([\d.]+)\)/.exec(transform())?.[1] ?? NaN);
    // The graph arrives fitted, not at k=1, so the invariant under test is that
    // a double-click DOUBLES the scale -- not that it lands on any absolute one.
    const before = scaleOf();
    expect(before).toBeGreaterThan(0);

    await user.dblClick(svg);
    // Doubles, up to the zoom rail -- this fixture's graph is small enough that
    // the fit already starts it well above 1.
    await waitFor(() => expect(scaleOf()).toBeCloseTo(Math.min(before * 2, MAX_ZOOM), 6));
    // No focus chip: background double-click zooms, it never focuses.
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('edits full prompts and triggers a locked dream sequence', async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Prompts' }));
    await screen.findByText('Current prompt');
    expect(await screen.findByText('Resolved run policy')).toBeInTheDocument();
    expect(screen.getAllByText('Automatic').length).toBeGreaterThan(0);
    await user.type(screen.getByLabelText('Current prompt'), '\nRemember JedAI Platform operating decisions.');
    await user.click(screen.getByRole('button', { name: 'Save new version' }));
    await waitFor(() => expect(screen.getByText(/Saved tenant-v1/)).toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: 'Dreaming' }));
    await screen.findByText('Dream sequence');
    expect(await screen.findByText(/Resolved Motive: agent-memory/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Run Dream Sequence' }));
    expect(await screen.findByText('Status log')).toBeInTheDocument();
    expect(await screen.findByText('Resolved Motive')).toBeInTheDocument();
    expect((await screen.findAllByText('tenant default')).length).toBeGreaterThan(0);
    expect(await screen.findByText('Policy and proof')).toBeInTheDocument();
    expect(await screen.findByText('Motive policy')).toBeInTheDocument();
    const dreamRunCall = fetchMock.mock.calls.find(([url]) => url === '/api/dream-sequence/run');
    expect(dreamRunCall).toBeDefined();
    expect(JSON.parse(String(dreamRunCall?.[1]?.body))).not.toHaveProperty('motive_name');
  });

  it('reports the Motive that governs the selected scope, with formation evidence', async () => {
    promptsPayload = projectScopeTenantPrompts;
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Dreaming' }));
    await screen.findByText('Dream sequence');

    // Strapline reports the governing Motive AND where it came from.
    expect(
      await screen.findByText(/Resolved Motive: project-memory-policy \(scope policy\)/)
    ).toBeInTheDocument();

    // "Policy and proof" reports the same Motive, its real goal and its real
    // allowed types -- never a bank default that could not have formed these rows.
    const policyPanel = (await screen.findByText('Policy and proof')).closest('.panel') as HTMLElement;
    // Once as the governing policy, once as the Motive these facts formed under.
    expect(within(policyPanel).getAllByText('project-memory-policy')).toHaveLength(2);
    expect(within(policyPanel).getByText('decision, requirement, directive')).toBeInTheDocument();
    expect(within(policyPanel).getByText(/Deliver the local platform safely/)).toBeInTheDocument();
    expect(within(policyPanel).queryByText('learn-compliance-requirements')).not.toBeInTheDocument();

    // Evidence: what actually formed the rows, with the Motive version digest.
    expect(within(policyPanel).getByText('Formed under (evidence on these facts)')).toBeInTheDocument();
    expect(within(policyPanel).getByText(/6 facts · digest 95122f6bcb7b/)).toBeInTheDocument();
    expect(within(policyPanel).getByText('no Motive stamped')).toBeInTheDocument();
    expect(within(policyPanel).queryByText(/Policy has changed/)).not.toBeInTheDocument();

    // The prompts read is scope-aware.
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).startsWith('/api/tenant-prompts?scope='))
    ).toBe(true);
  });

  it('surfaces a policy-vs-evidence divergence instead of hiding it', async () => {
    promptsPayload = divergentTenantPrompts;
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Dreaming' }));
    const policyPanel = (await screen.findByText('Policy and proof')).closest('.panel') as HTMLElement;
    expect(within(policyPanel).getByRole('status')).toHaveTextContent(/Policy has changed since these facts formed/);
    expect(within(policyPanel).getByText('project-memory-policy')).toBeInTheDocument();
    expect(within(policyPanel).getByText('agent-memory')).toBeInTheDocument();
  });

  it('reports an unconfigured MCP endpoint instead of a default that may be false', async () => {
    launchMcpUrl = '';
    render(<App />);

    await screen.findByText('Visible facts');
    const tenantPanel = (
      await screen.findByRole('heading', { name: 'Tenant' })
    ).closest('.panel') as HTMLElement;
    expect(within(tenantPanel).getByText('not configured')).toBeInTheDocument();
    expect(within(tenantPanel).queryByText('http://127.0.0.1:8010/mcp')).not.toBeInTheDocument();
  });

  it('does not land on an empty agent scope when the default scope holds the facts', async () => {
    const emptyAgentScope: Scope = {
      key: 'agent:claude-code',
      kind: 'agent',
      scope_id: 'claude-code',
      relationship_count: 0
    };
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://127.0.0.1:8765');
      if (url.pathname === '/api/scopes') return jsonResponse({ scopes: [emptyAgentScope, scope] });
      if (url.pathname === '/api/overview') {
        return jsonResponse({ ...overview(false), scopes: [emptyAgentScope, scope] });
      }
      if (url.pathname === '/api/tenant-config' && !init?.method) return jsonResponse(tenantConfig(false));
      if (url.pathname === '/api/tenant-prompts') return jsonResponse(promptsPayload);
      if (url.pathname === '/api/graph') return jsonResponse(graph);
      if (url.pathname === '/api/filter-options') return jsonResponse(filterOptions);
      if (url.pathname === '/api/evolution') return jsonResponse({ proof: evolution });
      if (url.pathname === '/api/dream-runs') return jsonResponse(dreams);
      if (url.pathname === '/api/archive') return jsonResponse(archive);
      return jsonResponse({ error: 'not found' }, { status: 404 });
    });
    render(<App />);

    await screen.findByText('Visible facts');
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes(`scope=${encodeURIComponent(scope.key)}`))
      ).toBe(true);
    });
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes('scope=agent%3Aclaude-code'))
    ).toBe(false);
  });

  it('warns when the launch tenant does not match the graph', async () => {
    launchTenantWarnings = [
      "tenant id 'wdpr-demo' matches no tenant in this graph (found: local-platform)"
    ];
    render(<App />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/matches no tenant in this graph/);
  });

  it('versions the governed project-memory policy', async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Project Memory' }));
    expect(await screen.findByText('Project-memory policy')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Deliver the local platform safely.')).toBeInTheDocument();
    expect(screen.getAllByText('project-v1').length).toBeGreaterThan(0);
    expect(screen.getByText('4')).toBeInTheDocument();

    const projectGoal = screen.getByLabelText('Overall project goal');
    await user.clear(projectGoal);
    await user.type(
      projectGoal,
      'Deliver the local platform with governed shared memory.'
    );
    await user.click(
      screen.getByRole('button', { name: 'Save project-memory version' })
    );

    expect(await screen.findByText('Saved project-v2')).toBeInTheDocument();
    const saveCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        url === '/api/platform/project-memory/config' &&
        init?.method === 'POST'
    );
    expect(saveCall).toBeDefined();
    const body = JSON.parse(String(saveCall?.[1]?.body));
    expect(body).toMatchObject({
      project_goal: 'Deliver the local platform with governed shared memory.',
      memory_goal: 'Keep durable cross-agent decisions and incidents.',
      keep: ['Decisions that affect multiple project agents.'],
      configured_by: 'admin-ui',
      max_memories_per_candidate: 8
    });
    expect(body.allowed_memory_types).toEqual([
      'requirement',
      'decision',
      'incident'
    ]);
  });

  it('renders compiled policy, certification evidence, and shadow divergence', async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Policy Rollout' }));
    expect(await screen.findByText('Compiled effective policy')).toBeInTheDocument();
    expect(screen.getByText('learn-compliance-requirements')).toBeInTheDocument();
    expect(screen.getByText('fixture-corpus')).toBeInTheDocument();
    expect(screen.getByText('Shadow-stage divergence')).toBeInTheDocument();
    expect(screen.getByText('5 / 5')).toBeInTheDocument();
  });

  it('renders integration snippets and copies text', async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, 'clipboard', {
      value: {
        writeText: clipboardWrite
      },
      configurable: true
    });
    render(<App />);

    await user.click(await screen.findByRole('button', { name: 'Integration' }));
    expect(await screen.findByText('No seeding required')).toBeInTheDocument();
    expect(screen.getByText('Register before memory tools')).toBeInTheDocument();
    expect(screen.getByText('Agent IDs and names are tenant-unique')).toBeInTheDocument();
    expect(screen.getByText('Motive resolves automatically')).toBeInTheDocument();

    const sdkPanel = screen.getByText('SDK snippet').closest('.snippet');
    expect(sdkPanel).not.toBeNull();
    expect(within(sdkPanel as HTMLElement).getByText(/MemotronPlatformClient/)).toBeInTheDocument();
    expect(within(sdkPanel as HTMLElement).getByText(/agent_register/)).toBeInTheDocument();
    expect(within(sdkPanel as HTMLElement).getByText(/memory_start/)).toBeInTheDocument();
    expect(within(sdkPanel as HTMLElement).getByText(/memory_search/)).toBeInTheDocument();
    expect(within(sdkPanel as HTMLElement).getByText(/memory_log/)).toBeInTheDocument();
    expect(within(sdkPanel as HTMLElement).getByText(/memory_refresh/)).toBeInTheDocument();
    expect(within(sdkPanel as HTMLElement).getByText(/project_memory_config/)).toBeInTheDocument();
    expect(within(sdkPanel as HTMLElement).getByText(/memory_publish/)).toBeInTheDocument();
    expect(within(sdkPanel as HTMLElement).getByText(/Motive resolves automatically/)).toBeInTheDocument();

    const mcpPanel = screen.getByText('MCP snippet').closest('.snippet');
    expect(mcpPanel).not.toBeNull();
    expect(within(mcpPanel as HTMLElement).getByText(/agent_register/)).toBeInTheDocument();
    expect(within(mcpPanel as HTMLElement).getByText(/memory_start/)).toBeInTheDocument();
    expect(within(mcpPanel as HTMLElement).getByText(/memory_search/)).toBeInTheDocument();
    expect(within(mcpPanel as HTMLElement).getByText(/memory_log/)).toBeInTheDocument();
    expect(within(mcpPanel as HTMLElement).getByText(/memory_refresh/)).toBeInTheDocument();
    expect(within(mcpPanel as HTMLElement).getByText(/project_memory_config/)).toBeInTheDocument();
    expect(within(mcpPanel as HTMLElement).getByText(/memory_publish/)).toBeInTheDocument();
    expect(within(mcpPanel as HTMLElement).getByText(/Motive resolves automatically/)).toBeInTheDocument();

    const codexPanel = screen.getByText('Codex MCP config').closest('.snippet');
    expect(codexPanel).not.toBeNull();
    expect(within(codexPanel as HTMLElement).getByText(/memotron_agent_memory/)).toBeInTheDocument();
    expect(within(codexPanel as HTMLElement).getByText(/required = true/)).toBeInTheDocument();
    expect(within(codexPanel as HTMLElement).getByText(/tool_timeout_sec = 120/)).toBeInTheDocument();
    expect(within(codexPanel as HTMLElement).getByText(/automatically/)).toBeInTheDocument();

    await user.click(within(sdkPanel as HTMLElement).getByRole('button', { name: 'Copy SDK snippet' }));

    await waitFor(() => {
      expect(clipboardWrite).toHaveBeenCalledWith(expect.stringContaining('MemotronPlatformClient'));
    });
  });
});
