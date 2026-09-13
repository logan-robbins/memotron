import {
  Activity,
  Braces,
  CheckCircle2,
  Circle,
  Clipboard,
  Database,
  FileText,
  GitBranch,
  KeyRound,
  Lock,
  Network,
  Play,
  RefreshCw,
  Search,
  SplitSquareHorizontal,
  Table2,
  Trash2,
  XCircle
} from 'lucide-react';
import {
  FormEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from 'react';
import {
  DEFAULT_FORCE_PARAMS,
  IDENTITY_VIEW,
  MAX_ZOOM,
  MIN_ZOOM,
  type Point,
  type SimLink,
  type SimNode,
  type Size,
  type Tally,
  type ViewTransform,
  buildAdjacency,
  contentBounds,
  coolAlpha,
  degreeMap,
  edgeGeometry,
  edgeSiblings,
  fitToBounds,
  forceTick,
  hubIds,
  isInspectableProperty,
  isSettled,
  labelColor,
  neighborhood,
  nodeCaption,
  nodeColor,
  nodeLabels,
  nodeRadius,
  primaryLabelCounts,
  propertyKeyCounts,
  relationshipTypeCounts,
  seedPositions,
  toGraphPoint,
  wheelZoomFactor,
  zoomAround
} from './graphView';
import {
  buildTriple,
  collapseFactNodes,
  endpointLabels,
  isRedundantFactSentence,
  resolveFactEndpoints,
  type CollapsedEdge,
  type RelationshipTriple
} from './relationshipView';
import {
  type GraphFilters,
  clearTenantLlm,
  getFilterOptions,
  getArchive,
  getControlPlane,
  getDreamRuns,
  getEvidence,
  getEvolution,
  getGraph,
  getOverview,
  getPolicyRollout,
  getProjectMemoryConfig,
  getScopes,
  getTenantConfig,
  getTenantPrompts,
  getTimeline,
  getDreamSequenceStatus,
  purgeTenantState,
  saveTenantLlm,
  saveTenantPrompts,
  saveProjectMemoryConfig,
  startDreamSequence
} from './api';
import type {
  ArchiveResponse,
  ControlPlaneResponse,
  DreamSequenceStatus,
  DreamRunsResponse,
  EvolutionProof,
  GraphEdge,
  GraphNode,
  GraphView,
  FilterOptions,
  MemoryEvidence,
  Overview,
  PolicyRolloutStatus,
  ProjectMemoryConfig,
  ProjectMemoryStatus,
  Scope,
  Screen,
  TenantConfig,
  TenantPurgeResult,
  TenantPrompts,
  TimelineEntry
} from './types';

type LoadState = 'idle' | 'loading' | 'ready';
type ViewMode = 'graph' | 'table';
type AdminFilters = GraphFilters;

const screens: Array<{ id: Screen; label: string; icon: typeof Activity }> = [
  { id: 'overview', label: 'Overview', icon: Activity },
  { id: 'memory', label: 'Memory Explorer', icon: Network },
  { id: 'explainability', label: 'Explainability', icon: GitBranch },
  { id: 'prompts', label: 'Prompts', icon: FileText },
  { id: 'project-memory', label: 'Project Memory', icon: Database },
  { id: 'dreaming', label: 'Dreaming', icon: SplitSquareHorizontal },
  { id: 'policy', label: 'Policy Rollout', icon: CheckCircle2 },
  { id: 'tenant', label: 'Tenant Setup', icon: KeyRound },
  { id: 'integration', label: 'Integration', icon: Braces }
];

function todayDate(): string {
  const now = new Date();
  const localDate = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return localDate.toISOString().slice(0, 10);
}

function preferredExplorerScope(defaultScope: string | undefined, scopes: Scope[]): string {
  const agentWithFacts = scopes.find((scope) => scope.kind === 'agent' && (scope.relationship_count ?? 0) > 0);
  // An agent scope holding nothing must never hide a populated default scope:
  // landing there shows an empty graph AND that agent's Motive rather than the
  // Motive governing the scope the operator actually has memory in.
  const populatedDefault = scopes.find(
    (scope) => scope.key === defaultScope && (scope.relationship_count ?? 0) > 0
  );
  const anyAgent = scopes.find((scope) => scope.kind === 'agent');
  return agentWithFacts?.key ?? populatedDefault?.key ?? anyAgent?.key ?? defaultScope ?? scopes[0]?.key ?? '';
}

function isFactNode(node: GraphNode | null): node is GraphNode {
  return Boolean(node?.relationship_uuid);
}

// (The old hardcoded memoryPalette lived here. It keyed on `identity` and
// `theme`, which the vocabulary renamed to `anchor` and `rollup` -- so every
// fact of those two types silently fell through to the grey default. Node
// colour now comes from `labelColor`, a hash of the label, which cannot go
// stale when the vocabulary moves.)

const projectMemoryTypes = [
  'anchor',
  'requirement',
  'preference',
  'directive',
  'state',
  'decision',
  'incident',
  'rollup'
] as const;

function lines(value: string): string[] {
  return [...new Set(value.split('\n').map((item) => item.trim()).filter(Boolean))];
}

function formatNumber(value: number | undefined, digits = 0): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '0';
  return value.toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits
  });
}

/** Proof-derived counts render as an em dash until the proof lands -- never as 0. */
function formatTokens(value: number | undefined): string {
  return typeof value === 'number' && !Number.isNaN(value) ? formatNumber(value) : '—';
}

/**
 * `repeat_search_rate` and `answered_from_profile_rate` are `null` when the
 * denominator is empty: nothing was measured.  A measured 0.0 is a real
 * observation and must render as "0%".  Never collapse the two.
 */
function formatRate(value: number | null | undefined, loaded: boolean): {
  text: string;
  unmeasured: boolean;
} {
  if (!loaded) return { text: '—', unmeasured: true };
  if (typeof value !== 'number' || Number.isNaN(value)) {
    return { text: 'not yet measured', unmeasured: true };
  }
  return { text: `${formatNumber(value * 100)}%`, unmeasured: false };
}

/**
 * Episodes-to-facts, stated in whichever direction the workload actually runs.
 * Conversational memory collapses many episodes into few durable facts
 * (compression); a knowledge base expands few document sections into many
 * extracted facts (expansion).  Both are healthy, so neither is reported as a
 * sub-1.0 ratio that reads like failure.
 */
function episodeFactShape(episodes: number | undefined, visible: number | undefined): string {
  if (!episodes || !visible) return 'not enough signal yet';
  if (visible > episodes) return `${(visible / episodes).toFixed(1)} facts per episode (expansion)`;
  if (episodes > visible) return `${(episodes / visible).toFixed(1)} episodes per fact (compression)`;
  return '1 fact per episode (even)';
}

/** Dominant memory type share -- the context a bare dedup percentage needs. */
function dominantTypeShare(counts: Record<string, number> | undefined): string {
  const entries = Object.entries(counts ?? {});
  if (!entries.length) return 'no type mix yet';
  const total = entries.reduce((sum, [, count]) => sum + count, 0);
  if (!total) return 'no type mix yet';
  const [name, count] = entries.reduce((top, entry) => (entry[1] > top[1] ? entry : top));
  return `${name} is ${Math.round((count / total) * 100)}% of visible facts`;
}

function propertyText(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === 'string' ? value : '';
}

function factNodes(graph: GraphView | null): GraphNode[] {
  return (graph?.nodes ?? []).filter((node) => node.node_type === 'memory');
}

function scopeLabel(scope: Scope | undefined): string {
  return scope ? `${scope.kind}:${scope.scope_id}` : '';
}

function useInterval(callback: () => void, delay: number | null): void {
  useEffect(() => {
    if (delay === null) return undefined;
    const id = window.setInterval(callback, delay);
    return () => window.clearInterval(id);
  }, [callback, delay]);
}

export function App() {
  const [active, setActive] = useState<Screen>('overview');
  const [loadState, setLoadState] = useState<LoadState>('idle');
  const [error, setError] = useState('');
  const [overview, setOverview] = useState<Overview | null>(null);
  const [scopes, setScopes] = useState<Scope[]>([]);
  const [tenant, setTenant] = useState<TenantConfig | null>(null);
  const [filterOptions, setFilterOptions] = useState<FilterOptions | null>(null);
  const [prompts, setPrompts] = useState<TenantPrompts | null>(null);
  const [projectMemory, setProjectMemory] = useState<ProjectMemoryStatus | null>(null);
  const [dreamStatus, setDreamStatus] = useState<DreamSequenceStatus | null>(null);
  const [graph, setGraph] = useState<GraphView | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>('graph');
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [evidence, setEvidence] = useState<MemoryEvidence | null>(null);
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [controlPlane, setControlPlane] = useState<ControlPlaneResponse | null>(null);
  const [evolution, setEvolution] = useState<EvolutionProof | null>(null);
  const [dreams, setDreams] = useState<DreamRunsResponse | null>(null);
  const [archive, setArchive] = useState<ArchiveResponse | null>(null);
  const [policyRollout, setPolicyRollout] = useState<PolicyRolloutStatus | null>(null);
  // The Explorer opens on the context-visible graph: demoted rows are the
  // members thematic consolidation absorbed into a ROLLUP, so showing them by
  // default renders both the rollup and everything it replaced -- and makes the
  // Explorer's fact count disagree with the Overview's VISIBLE FACTS.  The
  // toggle stays, as the way to see what a rollup absorbed.
  const [filters, setFilters] = useState<GraphFilters>({
    scope: '',
    query: '',
    statuses: ['active'],
    types: [],
    asOf: todayDate(),
    includeDemoted: false
  });

  const activeScope = filters.scope || overview?.tenant.default_scope || scopes[0]?.key || '';

  const handleError = useCallback((cause: unknown) => {
    setError(cause instanceof Error ? cause.message : String(cause));
  }, []);

  const refreshOverview = useCallback(async () => {
    try {
      const data = await getOverview(activeScope || undefined);
      setOverview(data);
      if (!filters.scope) {
        const candidateScopes = data.scopes.length ? data.scopes : scopes;
        setFilters((current) => ({
          ...current,
          scope: preferredExplorerScope(data.tenant.default_scope, candidateScopes)
        }));
      }
      setError('');
    } catch (cause) {
      handleError(cause);
    }
  }, [activeScope, filters.scope, handleError, scopes]);

  // The token economics the Overview leads with (raw vs. rendered, saved-by-
  // demotion, repeat-search / answered-from-profile) live only on the evolution
  // proof -- /api/overview.memory does not carry them.  The proof is a heavy
  // payload (it ships every active fact), so the Overview refetches it on its
  // own slower cadence instead of riding the 5s readiness poll.
  const refreshOverviewProof = useCallback(async () => {
    if (!activeScope) return;
    try {
      setEvolution(await getEvolution(activeScope));
      setError('');
    } catch (cause) {
      handleError(cause);
    }
  }, [activeScope, handleError]);

  const refreshTenant = useCallback(async () => {
    try {
      setTenant(await getTenantConfig());
      setError('');
    } catch (cause) {
      handleError(cause);
    }
  }, [handleError]);

  const refreshFilterOptions = useCallback(async () => {
    if (!activeScope) return;
    try {
      setFilterOptions(await getFilterOptions(activeScope));
      setError('');
    } catch (cause) {
      handleError(cause);
    }
  }, [activeScope, handleError]);

  const refreshPrompts = useCallback(async () => {
    try {
      setPrompts(await getTenantPrompts(activeScope || undefined));
      setError('');
    } catch (cause) {
      handleError(cause);
    }
  }, [activeScope, handleError]);

  const refreshProjectMemory = useCallback(async () => {
    try {
      setProjectMemory(await getProjectMemoryConfig());
      setError('');
    } catch (cause) {
      handleError(cause);
    }
  }, [handleError]);

  const refreshGraph = useCallback(async () => {
    if (!activeScope) return;
    try {
      const data = await getGraph({ ...filters, scope: activeScope });
      setGraph(data);
      setSelected((current) => {
        if (current && data.nodes.some((node) => node.id === current.id)) return current;
        return factNodes(data)[0] ?? null;
      });
      setError('');
    } catch (cause) {
      handleError(cause);
    }
  }, [activeScope, filters, handleError]);

  // Read `graph` through a ref here.  Listing `graph` as a dependency was the
  // Memory Explorer's infinite-refetch loop: every getGraph() resolves to a
  // fresh object, so setGraph(...) recreated this callback, which recreated
  // refreshActive, which refired the [active, refreshActive] effect, which
  // called getGraph() again -- an unbroken /api/graph + /api/filter-options
  // cycle that made the filters look dead.
  const graphRef = useRef<GraphView | null>(null);
  useEffect(() => {
    graphRef.current = graph;
  }, [graph]);

  const refreshExplainability = useCallback(async () => {
    if (!activeScope) return;
    try {
      if (!graphRef.current) {
        const nextGraph = await getGraph({ ...filters, scope: activeScope });
        setGraph(nextGraph);
        setSelected(factNodes(nextGraph)[0] ?? null);
      }
      setControlPlane(await getControlPlane(activeScope));
      setError('');
    } catch (cause) {
      handleError(cause);
    }
  }, [activeScope, filters, handleError]);

  const refreshEvolution = useCallback(async () => {
    if (!activeScope) return;
    try {
      const [proof, runs, archived] = await Promise.all([
        getEvolution(activeScope, filters.asOf),
        getDreamRuns(),
        getArchive(activeScope, filters.asOf)
      ]);
      setEvolution(proof);
      setDreams(runs);
      setArchive(archived);
      setError('');
    } catch (cause) {
      handleError(cause);
    }
  }, [activeScope, filters.asOf, handleError]);

  const refreshPolicyRollout = useCallback(async () => {
    if (!activeScope) return;
    try {
      setPolicyRollout(await getPolicyRollout(activeScope));
      setError('');
    } catch (cause) {
      handleError(cause);
    }
  }, [activeScope, handleError]);

  const handleTenantPurged = useCallback(
    async (result: TenantPurgeResult) => {
      const defaultScope = result.config.tenant.default_scope;
      const resetFilters: GraphFilters = {
        scope: defaultScope,
        query: '',
        statuses: ['active'],
        types: [],
        asOf: todayDate(),
        includeDemoted: false
      };
      setTenant(result.config);
      setPrompts(result.prompts);
      setProjectMemory(null);
      setDreamStatus(null);
      setGraph(null);
      setSelected(null);
      setEvidence(null);
      setTimeline([]);
      setControlPlane(null);
      setEvolution(null);
      setDreams(null);
      setArchive(null);
      setPolicyRollout(null);
      setFilterOptions(null);
      setFilters(resetFilters);
      try {
        const [nextOverview, nextScopes, nextFilterOptions, nextGraph] = await Promise.all([
          getOverview(defaultScope),
          getScopes(),
          getFilterOptions(defaultScope),
          getGraph(resetFilters)
        ]);
        setOverview(nextOverview);
        setScopes(nextScopes);
        setFilterOptions(nextFilterOptions);
        setGraph(nextGraph);
        setSelected(factNodes(nextGraph)[0] ?? null);
        setError('');
      } catch (cause) {
        handleError(cause);
      }
    },
    [handleError]
  );

  const refreshActive = useCallback(async () => {
    if (loadState === 'idle') return;
    if (active === 'overview') await Promise.all([refreshOverview(), refreshOverviewProof()]);
    if (active === 'memory') {
      await Promise.all([refreshGraph(), refreshFilterOptions()]);
    }
    if (active === 'explainability') await refreshExplainability();
    if (active === 'prompts') await refreshPrompts();
    if (active === 'project-memory') await refreshProjectMemory();
    if (active === 'dreaming') await Promise.all([refreshEvolution(), refreshPrompts(), refreshTenant()]);
    if (active === 'policy') await refreshPolicyRollout();
    if (active === 'tenant') await refreshTenant();
    if (active === 'integration') await refreshTenant();
  }, [
    active,
    loadState,
    refreshEvolution,
    refreshExplainability,
    refreshFilterOptions,
    refreshGraph,
    refreshOverview,
    refreshOverviewProof,
    refreshPolicyRollout,
    refreshProjectMemory,
    refreshPrompts,
    refreshTenant
  ]);

  useEffect(() => {
    let cancelled = false;
    async function bootstrap() {
      setLoadState('loading');
      try {
        const [overviewData, scopeData, tenantData] = await Promise.all([
          getOverview(),
          getScopes(),
          getTenantConfig()
        ]);
        if (cancelled) return;
        setOverview(overviewData);
        setScopes(scopeData);
        setTenant(tenantData);
        setFilters((current) => ({
          ...current,
          scope: preferredExplorerScope(overviewData.tenant.default_scope, scopeData.length ? scopeData : overviewData.scopes)
        }));
        setLoadState('ready');
      } catch (cause) {
        if (!cancelled) {
          handleError(cause);
          setLoadState('ready');
        }
      }
    }
    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, [handleError]);

  useEffect(() => {
    void refreshActive();
  }, [active, refreshActive]);

  useEffect(() => {
    if (!selected?.relationship_uuid || !activeScope) {
      setEvidence(null);
      setTimeline([]);
      return;
    }
    const selectedFact = selected;
    let cancelled = false;
    async function loadSelection() {
      try {
        const evidenceData = await getEvidence(activeScope, selectedFact.relationship_uuid ?? '');
        const timelineData = await getTimeline(
          activeScope,
          propertyText(selectedFact.properties, 'subject') || evidenceData.subject,
          propertyText(selectedFact.properties, 'predicate') || evidenceData.predicate,
          selectedFact.relationship_type || evidenceData.relationship_type
        );
        if (!cancelled) {
          setEvidence(evidenceData);
          setTimeline(timelineData);
        }
      } catch (cause) {
        if (!cancelled) handleError(cause);
      }
    }
    void loadSelection();
    return () => {
      cancelled = true;
    };
  }, [activeScope, handleError, selected]);

  const refreshDreamSequenceStatus = useCallback(async () => {
    try {
      const status = await getDreamSequenceStatus(dreamStatus?.run_id);
      if ('runs' in status) {
        setDreamStatus(status.runs[0] ?? null);
      } else {
        setDreamStatus(status);
      }
      setError('');
    } catch (cause) {
      handleError(cause);
    }
  }, [dreamStatus?.run_id, handleError]);

  useInterval(() => {
    void refreshOverview();
  }, active === 'overview' ? 5000 : null);
  useInterval(() => {
    void refreshOverviewProof();
  }, active === 'overview' ? 15000 : null);
  // The Memory Explorer deliberately does NOT poll: an interactive canvas that
  // refetches underneath a drag or a half-built filter feels broken.  Fetches
  // happen exactly once per deliberate change (scope, filter, as-of, Refresh).
  useInterval(() => {
    void refreshEvolution();
  }, active === 'dreaming' ? 5000 : null);
  useInterval(() => {
    void refreshDreamSequenceStatus();
  }, active === 'dreaming' || dreamStatus?.running ? 2000 : null);

  const selectedLineage = useMemo(() => lineageEdges(graph, selected), [graph, selected]);

  const setFilter = <K extends keyof AdminFilters>(key: K, value: AdminFilters[K]) => {
    setFilters((current) => ({ ...current, [key]: value }));
  };

  const openExplainability = (node: GraphNode) => {
    if (!isFactNode(node)) return;
    setSelected(node);
    setActive('explainability');
  };

  const runDreamSequence = async (agentId: string, motiveName: string) => {
    if (!activeScope) return;
    try {
      const status = await startDreamSequence({
        scope: activeScope,
        agent_id: agentId || undefined,
        motive_name: motiveName || undefined
      });
      setDreamStatus(status);
      await Promise.all([refreshGraph(), refreshEvolution(), refreshOverview()]);
      setError('');
    } catch (cause) {
      handleError(cause);
    }
  };

  return (
    <div className="shell">
      <aside className="sidebar" aria-label="Memotron admin navigation">
        <div className="brand">
          <div className="brand-mark">DW</div>
          <div>
            <strong>Memotron</strong>
            <span>Admin</span>
          </div>
        </div>
        <nav className="nav-list">
          {screens.map((screen) => {
            const Icon = screen.icon;
            return (
              <button
                key={screen.id}
                className={active === screen.id ? 'nav-item active' : 'nav-item'}
                type="button"
                onClick={() => setActive(screen.id)}
              >
                <Icon size={17} aria-hidden="true" />
                <span>{screen.label}</span>
              </button>
            );
          })}
        </nav>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <div>
            <div className="eyebrow">{overview?.tenant.tenant_id ?? 'tenant'}</div>
            <h1>{screens.find((screen) => screen.id === active)?.label}</h1>
          </div>
          <div className="status-strip">
            <span className={overview?.llm.has_api_key ? 'status good' : 'status warn'}>
              {overview?.llm.has_api_key ? <CheckCircle2 size={15} /> : <XCircle size={15} />}
              LLM {overview?.llm.has_api_key ? 'ready' : 'unset'}
            </span>
            <label className="scope-picker">
              <span>Scope</span>
              <select value={activeScope} onChange={(event) => setFilter('scope', event.target.value)}>
                {(scopes.length ? scopes : overview?.scopes ?? []).map((scope) => (
                  <option key={scope.key} value={scope.key}>
                    {scope.key}
                  </option>
                ))}
              </select>
            </label>
            <button className="icon-button" type="button" onClick={() => void refreshActive()} title="Refresh">
              <RefreshCw size={17} aria-hidden="true" />
              <span className="sr-only">Refresh</span>
            </button>
          </div>
        </header>

        {error ? <div className="error-banner">{error}</div> : null}
        {loadState === 'loading' ? <div className="loading">Loading admin data...</div> : null}

        <main className="screen">
          {active === 'overview' && <OverviewScreen overview={overview} proof={evolution} />}
          {active === 'memory' && (
            <MemoryExplorer
              filters={{ ...filters, scope: activeScope }}
              filterOptions={filterOptions}
              graph={graph}
              selected={selected}
              viewMode={viewMode}
              onFilter={setFilter}
              onOpenExplainability={openExplainability}
              onRefresh={() => void refreshGraph()}
              onSelect={setSelected}
              onViewMode={setViewMode}
            />
          )}
          {active === 'explainability' && (
            <ExplainabilityScreen
              graph={graph}
              selected={selected}
              evidence={evidence}
              timeline={timeline}
              lineage={selectedLineage}
              controlPlane={controlPlane}
              onBackToSearch={() => setActive('memory')}
            />
          )}
          {active === 'prompts' && (
            <PromptsScreen
              prompts={prompts}
              onPromptsChanged={setPrompts}
            />
          )}
          {active === 'project-memory' && (
            <ProjectMemoryScreen
              status={projectMemory}
              onChanged={setProjectMemory}
            />
          )}
          {active === 'dreaming' && (
            <DreamingScreen
              activeScope={activeScope}
              config={tenant}
              prompts={prompts}
              dreamStatus={dreamStatus}
              evolution={evolution}
              dreams={dreams}
              archive={archive}
              onRunDreamSequence={runDreamSequence}
            />
          )}
          {active === 'policy' && <PolicyRolloutScreen rollout={policyRollout} />}
          {active === 'tenant' && (
            <TenantSetupScreen
              config={tenant}
              onReload={refreshTenant}
              onTenantChanged={(config) => {
                setTenant(config);
                void refreshOverview();
              }}
              onTenantPurged={handleTenantPurged}
            />
          )}
          {active === 'integration' && <IntegrationScreen config={tenant} />}
        </main>
      </div>
    </div>
  );
}

function OverviewScreen({
  overview,
  proof
}: {
  overview: Overview | null;
  proof: EvolutionProof | null;
}) {
  const readiness = overview?.readiness;
  const memory = overview?.memory;
  const proofLoaded = proof !== null;
  const rawTokens = proof?.tokens_raw_episodes;
  const savedTokens = proof?.tokens_saved_vs_raw;
  // Three distinct states, none of which may be reported as one of the others:
  // proof not loaded yet, loaded with no raw episodes, loaded with a real share.
  const savedShare = !proofLoaded
    ? 'vs. re-reading raw episodes'
    : typeof rawTokens === 'number' && rawTokens > 0 && typeof savedTokens === 'number'
      ? `${Math.round((savedTokens / rawTokens) * 100)}% vs. re-reading raw`
      : 'no raw episodes yet';
  const demotionTokens = proof?.tokens_saved_by_demotion;
  const repeatSearch = formatRate(proof?.repeat_search_rate, proofLoaded);
  const answeredFromProfile = formatRate(proof?.answered_from_profile_rate, proofLoaded);
  const checks = [
    ['LLM credentials', readiness?.llm_configured],
    ['Agents observed', readiness?.agents_observed],
    ['Scopes', readiness?.scopes_registered],
    ['Platform API', readiness?.platform_api_ready],
    ['MCP', readiness?.mcp_ready]
  ] as const;
  const tenantWarnings = overview?.tenant.warnings ?? [];
  return (
    <div className="stack">
      {tenantWarnings.map((warning) => (
        <div className="tenant-warning" role="alert" key={warning}>
          <strong>Tenant configuration</strong>
          <span>{warning}</span>
        </div>
      ))}
      <section className="metric-grid" aria-label="Memory value cards">
        <Metric
          label="Context tokens"
          value={`${formatTokens(proof?.tokens_rendered_profile)} / ${formatTokens(rawTokens)}`}
          hint="rendered profile / raw episodes"
          wide
        />
        <Metric
          label="Tokens saved"
          value={formatTokens(savedTokens)}
          hint={savedShare}
        />
        <Metric
          label="Visible facts"
          value={formatNumber(memory?.visible_facts)}
          hint={`${formatNumber(memory?.active_facts)} active · ${formatNumber(memory?.inactive_facts)} inactive`}
        />
        <Metric
          label="Rollups"
          value={formatNumber(memory?.rollups)}
          hint="consolidated summaries"
        />
        <Metric
          label="Demoted"
          value={formatNumber(memory?.demoted)}
          hint={`${formatTokens(demotionTokens)} tokens saved by demotion`}
        />
      </section>
      <p className="metric-caption">
        Diagnostics — how this scope&apos;s working set is shaped.
      </p>
      <section className="metric-grid quiet" aria-label="Memory shape diagnostics">
        <Metric
          label="Episodes → facts"
          value={`${formatNumber(memory?.episodes)} → ${formatNumber(memory?.visible_facts)}`}
          hint={episodeFactShape(memory?.episodes, memory?.visible_facts)}
        />
        <Metric
          label="Semantic dedup"
          value={`${formatNumber((memory?.semantic_dedup_rate ?? 0) * 100)}%`}
          hint={dominantTypeShare(memory?.per_type_active_counts)}
        />
        <Metric
          label="Whole working set"
          value={formatTokens(proof?.tokens_unbudgeted_facts)}
          hint="tokens if every visible fact were injected"
        />
        <Metric
          label="Repeat search"
          value={repeatSearch.text}
          hint="same query re-searched across sessions"
          unmeasured={repeatSearch.unmeasured}
        />
        <Metric
          label="Answered from profile"
          value={answeredFromProfile.text}
          hint="citations served without a search"
          unmeasured={answeredFromProfile.unmeasured}
        />
      </section>
      <section className="panel-grid">
        <div className="panel">
          <h2>Tenant</h2>
          <KeyValue label="Tenant" value={overview?.tenant.tenant_id} mono />
          <KeyValue label="Observed agents" value={overview?.tenant.agent_ids.join(', ') || 'None yet'} mono />
          <KeyValue label="Default scope" value={overview?.tenant.default_scope} mono />
          <KeyValue label="Platform API" value={overview?.operator.platform_api_url} mono />
          <KeyValue label="MCP" value={overview?.operator.mcp_url || 'not configured'} mono />
        </div>
        <div className="panel">
          <h2>Readiness</h2>
          <div className="readiness-list">
            {checks.map(([label, ready]) => (
              <div className="readiness-row" key={label}>
                {ready ? <CheckCircle2 size={16} /> : <Circle size={16} />}
                <span>{label}</span>
                <strong>{ready ? 'ready' : 'pending'}</strong>
              </div>
            ))}
          </div>
        </div>
        <div className="panel">
          <h2>Latest dream run</h2>
          <KeyValue label="Ran at" value={overview?.evolution.latest_dream_run?.ran_at ?? 'none'} mono />
          <KeyValue label="Job" value={overview?.evolution.latest_dream_run?.job_name ?? 'none'} />
          <KeyValue label="Kind" value={overview?.evolution.latest_dream_run?.job_kind ?? 'none'} />
          <KeyValue label="Decisions" value={formatNumber(overview?.evolution.decision_count)} />
        </div>
      </section>
    </div>
  );
}

function MemoryExplorer({
  filters,
  filterOptions,
  graph,
  selected,
  viewMode,
  onFilter,
  onOpenExplainability,
  onRefresh,
  onSelect,
  onViewMode
}: {
  filters: AdminFilters;
  filterOptions: FilterOptions | null;
  graph: GraphView | null;
  selected: GraphNode | null;
  viewMode: ViewMode;
  onFilter: <K extends keyof AdminFilters>(key: K, value: AdminFilters[K]) => void;
  onOpenExplainability: (node: GraphNode) => void;
  onRefresh: () => void;
  onSelect: (node: GraphNode | null) => void;
  onViewMode: (mode: ViewMode) => void;
}) {
  const options = filterOptions?.options;
  return (
    <div className="stack">
      <section className="toolbar">
        <label>
          <span>Smart search</span>
          <SmartSearch
            value={filters.query}
            options={options ?? { subjects: [], predicates: [], objects: [], statuses: [], types: [] }}
            onChange={(value) => onFilter('query', value)}
            onSubmit={onRefresh}
          />
        </label>
        <label>
          <span>Status</span>
          <MultiCheckDropdown
            label="Status"
            values={filters.statuses}
            options={options?.statuses.length ? options.statuses : ['active']}
            onChange={(values) => onFilter('statuses', values)}
          />
        </label>
        <label>
          <span>Types</span>
          <MultiCheckDropdown
            label="Types"
            values={filters.types}
            options={options?.types ?? []}
            emptyLabel="All types"
            onChange={(values) => onFilter('types', values)}
          />
        </label>
        <label>
          <span>As of</span>
          <input
            type="date"
            value={filters.asOf}
            onChange={(event) => onFilter('asOf', event.target.value)}
          />
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={filters.includeDemoted}
            onChange={(event) => onFilter('includeDemoted', event.target.checked)}
          />
          <span title="Reveal the facts a ROLLUP absorbed. They are active but deliberately outside the default context.">
            Show demoted
          </span>
        </label>
        <button className="primary-button" type="button" onClick={onRefresh}>
          <RefreshCw size={15} />
          Refresh
        </button>
        <div className="segmented" role="group" aria-label="Memory view mode">
          <button className={viewMode === 'graph' ? 'active' : ''} type="button" onClick={() => onViewMode('graph')}>
            <Network size={15} />
            Graph
          </button>
          <button className={viewMode === 'table' ? 'active' : ''} type="button" onClick={() => onViewMode('table')}>
            <Table2 size={15} />
            Table
          </button>
        </div>
      </section>

      <section className="split-layout">
        <div className="panel graph-panel">
          <div className="panel-head">
            <h2>Scope graph</h2>
            <span>{formatNumber(graph?.relationship_count)} facts</span>
          </div>
          {viewMode === 'graph' ? (
            <GraphCanvas graph={graph} selected={selected} onSelect={onSelect} />
          ) : (
            <FactTable graph={graph} selected={selected} onSelect={onSelect} onOpenExplainability={onOpenExplainability} />
          )}
        </div>
        <Inspector graph={graph} selected={selected} evidence={null} timeline={[]} onOpenExplainability={onOpenExplainability} />
      </section>
    </div>
  );
}

function MultiCheckDropdown({
  label,
  values,
  options,
  emptyLabel = 'None',
  onChange
}: {
  label: string;
  values: string[];
  options: string[];
  emptyLabel?: string;
  onChange: (values: string[]) => void;
}) {
  const uniqueOptions = Array.from(new Set(options.filter(Boolean)));
  const summary = values.length ? values.join(', ') : emptyLabel;
  const [open, setOpen] = useState(false);
  const detailsRef = useRef<HTMLDetailsElement | null>(null);
  useEffect(() => {
    if (!open) return;
    function closeFromOutside(event: MouseEvent | PointerEvent) {
      if (!detailsRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function closeFromKeyboard(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false);
    }
    document.addEventListener('pointerdown', closeFromOutside);
    document.addEventListener('keydown', closeFromKeyboard);
    return () => {
      document.removeEventListener('pointerdown', closeFromOutside);
      document.removeEventListener('keydown', closeFromKeyboard);
    };
  }, [open]);
  function toggle(value: string) {
    onChange(values.includes(value) ? values.filter((item) => item !== value) : [...values, value]);
  }
  return (
    <details ref={detailsRef} className="multi-select" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary title={summary}>
        <span>{summary}</span>
      </summary>
      <div className="multi-menu" role="group" aria-label={label}>
        {uniqueOptions.map((option) => (
          <label className="check menu-check" key={option}>
            <input
              type="checkbox"
              checked={values.includes(option)}
              onChange={() => toggle(option)}
            />
            <span>{option}</span>
          </label>
        ))}
        {!uniqueOptions.length ? <div className="empty-state compact">No values yet.</div> : null}
        {values.length ? (
          <button
            className="text-button"
            type="button"
            onClick={() => {
              onChange([]);
              setOpen(false);
            }}
          >
            Clear
          </button>
        ) : null}
      </div>
    </details>
  );
}

function SmartSearch({
  value,
  options,
  onChange,
  onSubmit
}: {
  value: string;
  options: FilterOptions['options'];
  onChange: (value: string) => void;
  onSubmit: () => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const suggestions = useMemo(() => {
    const query = value.trim().toLowerCase();
    const groups = [
      ['Subject', options.subjects],
      ['Predicate', options.predicates],
      ['Object', options.objects]
    ] as const;
    return groups.flatMap(([label, values]) =>
      values
        .filter((candidate) => !query || candidate.toLowerCase().includes(query))
        .slice(0, 4)
        .map((candidate) => ({ label, value: candidate }))
    ).slice(0, 10);
  }, [options, value]);
  useEffect(() => {
    if (!open) return;
    function closeFromOutside(event: MouseEvent | PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function closeFromKeyboard(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false);
    }
    document.addEventListener('pointerdown', closeFromOutside);
    document.addEventListener('keydown', closeFromKeyboard);
    return () => {
      document.removeEventListener('pointerdown', closeFromOutside);
      document.removeEventListener('keydown', closeFromKeyboard);
    };
  }, [open]);
  function submitSearch() {
    setOpen(false);
    onSubmit();
  }
  const showSuggestions = open && suggestions.length > 0;
  return (
    <div className="smart-search" ref={rootRef}>
      <div className={showSuggestions ? 'input-with-icon has-close' : 'input-with-icon'}>
        <Search size={15} />
        <input
          value={value}
          onClick={() => setOpen(true)}
          onFocus={() => setOpen(true)}
          onChange={(event) => {
            onChange(event.target.value);
            setOpen(true);
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              submitSearch();
            }
            if (event.key === 'Escape') setOpen(false);
          }}
          placeholder="subject, predicate, object"
        />
        {showSuggestions ? (
          <button className="smart-search-close" type="button" title="Close suggestions" onClick={() => setOpen(false)}>
            <XCircle size={14} />
            <span className="sr-only">Close search suggestions</span>
          </button>
        ) : null}
      </div>
      {showSuggestions ? (
        <div className="suggestions" role="listbox" aria-label="Smart search suggestions">
          {suggestions.map((suggestion) => (
            <button
              type="button"
              key={`${suggestion.label}:${suggestion.value}`}
              onClick={() => {
                onChange(suggestion.value);
                submitSearch();
              }}
            >
              <span>{suggestion.label}</span>
              <strong>{suggestion.value}</strong>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// Pointer position in the SVG's (untransformed) viewBox coordinates.  Guards
// exist because jsdom implements neither getScreenCTM nor createSVGPoint.
function viewBoxPoint(svg: SVGSVGElement, event: { clientX: number; clientY: number }): Point | null {
  if (typeof svg.getScreenCTM !== 'function' || typeof svg.createSVGPoint !== 'function') return null;
  const matrix = svg.getScreenCTM();
  if (!matrix) return null;
  const point = svg.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  const transformed = point.matrixTransform(matrix.inverse());
  return { x: transformed.x, y: transformed.y };
}

// The viewport does not scale with the node count -- the force layout decides
// how much space it needs and the view transform frames it. (The old canvas grew
// with sqrt(nodeCount), which is the layout's job, not the viewport's.)
//
// It is MEASURED rather than fixed: a constant viewBox against a fluid element
// makes preserveAspectRatio letterbox the difference. At 608x560 CSS px against
// a 960x600 viewBox that was ~180px of dead vertical space, and every "fit" was
// fitting to a box the graph could not actually use. Measuring makes one viewBox
// unit exactly one CSS pixel.
const GRAPH_VIEWPORT_FALLBACK = { width: 960, height: 600 };

/**
 * Live CSS-pixel size of an element, via ResizeObserver.
 *
 * Takes the ELEMENT, not a ref object, on purpose. Given a ref, this effect runs
 * once on mount and reads `ref.current` — but the canvas is behind an early
 * return for the empty graph, so on the first render (before any data arrives)
 * there is no svg, `ref.current` is null, and the effect bails. Nothing in its
 * dependency list ever changes again, so the observer is never attached and the
 * size stays stuck at the fallback forever. A callback ref puts the element in
 * state, which makes mounting a dependency change.
 */
function useElementSize(element: Element | null, fallback: Size): Size {
  const [size, setSize] = useState<Size>(fallback);
  useEffect(() => {
    if (!element || typeof ResizeObserver === 'undefined') return undefined;
    function measure(width: number, height: number) {
      if (width < 1 || height < 1) return;
      setSize((current) =>
        Math.abs(current.width - width) < 1 && Math.abs(current.height - height) < 1
          ? current
          : { width: Math.round(width), height: Math.round(height) }
      );
    }
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) measure(entry.contentRect.width, entry.contentRect.height);
    });
    observer.observe(element);
    const box = element.getBoundingClientRect();
    measure(box.width, box.height);
    return () => observer.disconnect();
  }, [element, fallback.width, fallback.height]);
  return size;
}
// How far to trim an edge short of the target rim so the ARROWHEAD tip, rather
// than the line, touches the node.
const ARROW_LENGTH = 9;
// Full cooling for a graph a person can read; a shorter run for a big one, so
// the first paint is never held up by a second of physics.
const SETTLE_TICKS_SMALL = 300;
const SETTLE_TICKS_LARGE = 120;
const LARGE_GRAPH_NODES = 250;
const CANVAS_CAPTION_LENGTH = 24;
// Captions are only DRAWN below the density threshold, so only then does the fit
// need to leave room for them. ~0.51em per glyph at the 11px caption size.
const CAPTION_DENSITY_LIMIT = 45;
function captionAllowanceFor(nodeCount: number): number {
  return nodeCount <= CAPTION_DENSITY_LIMIT ? CANVAS_CAPTION_LENGTH * 5.6 : 0;
}
// The fit on arrival frames the graph; this only stops a two-node result being
// magnified to the 8x rail.
const AUTO_FIT_MAX_SCALE = 2.2;

type NodeMenu = { nodeId: string; x: number; y: number };

/**
 * The graph explorer, built to Neo4j Browser's interaction grammar.
 *
 * Matched behaviours, and where each one lives:
 *   * force-directed layout, live while you drag        -- forceTick + rAF loop
 *   * drag a node to PIN it where you drop it           -- `fx`/`fy`, node menu unpins
 *   * colour by label, stable across renders            -- nodeColor (hashed)
 *   * legend of every label with its count, click to filter
 *   * relationship-type captions ON the edges, curved multi-edges, arrowheads
 *   * double-click a node to expand its neighbours, again to undo
 *   * right-click a node: expand / dismiss / unpin / focus
 *   * database-information panel: labels, relationship types, property keys
 *   * zoom in / out / fit / reset
 */
function GraphCanvas({
  graph,
  selected,
  onSelect
}: {
  graph: GraphView | null;
  selected: GraphNode | null;
  onSelect: (node: GraphNode | null) => void;
}) {
  const rawNodes = graph?.nodes ?? [];
  const rawEdges = graph?.edges ?? [];
  // `Memotron.knowledge_graph` reifies every relationship as
  // entity--subject-->fact--object-->entity so evidence, the truth timeline
  // and supersession can all attach to the fact. Drawn literally that puts a
  // sentence-captioned circle on the canvas for every fact and leaves
  // "subject"/"object" as the only edge labels -- a graph of facts pretending
  // to be a graph of entities. `collapseFactNodes` undoes that for display:
  // one node per entity, one edge per fact, captioned with its predicate. The
  // fact stays fully inspectable -- it rides along on the edge as `factNode`.
  const collapsedGraph = useMemo(() => collapseFactNodes(rawNodes, rawEdges), [rawNodes, rawEdges]);
  const nodes = collapsedGraph.nodes;
  const edges = collapsedGraph.edges;
  const svgRef = useRef<SVGSVGElement | null>(null);
  // The element in state (for measuring) AND in a ref (for the imperative
  // pointer/CTM math, which must not wait for a render).
  const [svgElement, setSvgElement] = useState<SVGSVGElement | null>(null);
  const attachSvg = useCallback((element: SVGSVGElement | null) => {
    svgRef.current = element;
    setSvgElement(element);
  }, []);
  const viewport = useElementSize(svgElement, GRAPH_VIEWPORT_FALLBACK);

  // ---- structure derived from the data ------------------------------------
  const adjacency = useMemo(() => buildAdjacency(edges), [edges]);
  const degrees = useMemo(() => degreeMap(adjacency), [adjacency]);
  const maxDegree = useMemo(() => (degrees.size ? Math.max(...degrees.values()) : 0), [degrees]);
  const radiusById = useMemo(() => {
    const radii = new Map<string, number>();
    for (const node of nodes) radii.set(node.id, nodeRadius(degrees.get(node.id) ?? 0, maxDegree));
    return radii;
  }, [nodes, degrees, maxDegree]);

  // ---- the database-information panel / legend rows -----------------------
  // Counted over the whole FETCHED result set, not the visible scene. Neo4j's
  // legend counts the scene, but Neo4j's legend only highlights on click whereas
  // these rows FILTER: scene counts would collapse to (0) the moment you used
  // one, making the row you need to click next look like it matches nothing.
  // Stable counts keep the panel usable as a filter and still answer "what is in
  // this graph". Which rows are active is shown by their state instead.
  const labelRows = useMemo(() => primaryLabelCounts(nodes), [nodes]);
  const relationshipRows = useMemo(() => relationshipTypeCounts(edges), [edges]);
  const propertyRows = useMemo(() => propertyKeyCounts(nodes), [nodes]);

  // ---- what is on screen --------------------------------------------------
  // Empty filter sets mean "no filter", so the default view is the whole graph.
  const [activeLabels, setActiveLabels] = useState<ReadonlySet<string>>(() => new Set<string>());
  const [activeTypes, setActiveTypes] = useState<ReadonlySet<string>>(() => new Set<string>());
  // Neo4j's expand: nodes whose neighbours have been explicitly pulled in, even
  // when a filter or focus would otherwise exclude them.  Double-click toggles.
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set<string>());
  // Neo4j's "dismiss from the visualization".
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(() => new Set<string>());
  const [focus, setFocus] = useState<{ nodeId: string; depth: 1 | 2 } | null>(null);
  const [menu, setMenu] = useState<NodeMenu | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [pinnedCount, setPinnedCount] = useState(0);

  const labelsById = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const node of nodes) map.set(node.id, nodeLabels(node));
    return map;
  }, [nodes]);

  const visibleIds = useMemo(() => {
    const base = new Set<string>();
    const inFocus = focus ? neighborhood(adjacency, focus.nodeId, focus.depth) : null;
    for (const node of nodes) {
      if (inFocus && !inFocus.has(node.id)) continue;
      if (activeLabels.size) {
        const labels = labelsById.get(node.id) ?? [];
        if (!labels.some((label) => activeLabels.has(label))) continue;
      }
      base.add(node.id);
    }
    // Expansion pulls a node and its neighbours in regardless of the filter --
    // that is the whole point of expanding, and it is how Neo4j behaves.
    for (const id of expanded) {
      base.add(id);
      for (const neighbor of adjacency.get(id) ?? []) base.add(neighbor);
    }
    for (const id of dismissed) base.delete(id);
    return base;
  }, [nodes, adjacency, focus, activeLabels, labelsById, expanded, dismissed]);

  const visibleNodes = useMemo(() => nodes.filter((node) => visibleIds.has(node.id)), [nodes, visibleIds]);
  const visibleEdges = useMemo(
    () =>
      edges.filter(
        (edge) =>
          visibleIds.has(edge.source_id) &&
          visibleIds.has(edge.target_id) &&
          (activeTypes.size === 0 || activeTypes.has(edge.edge_type))
      ),
    [edges, visibleIds, activeTypes]
  );
  const siblings = useMemo(() => edgeSiblings(visibleEdges), [visibleEdges]);

  // ---- the simulation -----------------------------------------------------
  const forceParams = useMemo(
    () => ({
      ...DEFAULT_FORCE_PARAMS,
      centerX: viewport.width / 2,
      centerY: viewport.height / 2
    }),
    [viewport.width, viewport.height]
  );
  const simRef = useRef<SimNode[]>([]);
  const alphaRef = useRef(0);
  const linksRef = useRef<SimLink[]>([]);
  const frameRef = useRef<number | null>(null);
  const [simNodes, setSimNodes] = useState<SimNode[]>([]);

  const simLinks = useMemo<SimLink[]>(
    () => visibleEdges.map((edge) => ({ source: edge.source_id, target: edge.target_id })),
    [visibleEdges]
  );
  // The identity of the node set, not the array. The graph endpoint is polled,
  // so `nodes` is a fresh array on every refresh; reseeding on that would
  // restart the layout under the operator's hands a few times a minute.
  const simKey = useMemo(() => visibleNodes.map((node) => node.id).join(''), [visibleNodes]);
  const linkKey = useMemo(() => simLinks.map((link) => `${link.source}>${link.target}`).join(''), [simLinks]);

  const [view, setView] = useState<ViewTransform>(IDENTITY_VIEW);
  /**
   * Crowding is a property of the LAYOUT, not of the zoom -- captions sit inside
   * the zoom transform and scale with it, so a fit can magnify freely without
   * making anything collide (see DEFAULT_FORCE_PARAMS). `maxScale` exists only
   * to stop a two-node result from being blown up to the 8x rail.
   */
  const fitToNodes = useCallback(
    (targets: ReadonlyArray<SimNode>, captionAllowance: number, maxScale = MAX_ZOOM) => {
      // Two points per node -- its top-left and the far end of its caption --
      // because a caption is drawn to the RIGHT of the circle and is far wider
      // than any symmetric margin. Fitting node centres alone left the captions
      // on the right-hand nodes clipped off the edge of the frame.
      const bounds = contentBounds(
        targets.flatMap((node) => [
          { x: node.x - node.radius, y: node.y - node.radius },
          { x: node.x + node.radius + captionAllowance, y: node.y + node.radius }
        ]),
        12
      );
      if (bounds) setView(fitToBounds(bounds, viewport, 24, MIN_ZOOM, maxScale));
    },
    [viewport]
  );

  const runFrame = useCallback(() => {
    frameRef.current = null;
    // A big graph advances several ticks a frame so it still reaches rest
    // promptly; a small one animates one tick a frame and reads as motion.
    const ticksPerFrame = simRef.current.length > LARGE_GRAPH_NODES ? 3 : 1;
    for (let tick = 0; tick < ticksPerFrame && !isSettled(alphaRef.current); tick += 1) {
      simRef.current = forceTick(simRef.current, linksRef.current, alphaRef.current, forceParams);
      alphaRef.current = coolAlpha(alphaRef.current);
    }
    setSimNodes(simRef.current);
    if (!isSettled(alphaRef.current)) frameRef.current = requestAnimationFrame(runFrame);
  }, [forceParams]);

  // Re-energise the layout after an interaction (a drag, an expand, an unpin) --
  // d3's `alphaTarget`/`restart`, and the reason dragging a node visibly drags
  // its neighbours along.
  const reheat = useCallback(
    (alpha = 0.45) => {
      alphaRef.current = Math.max(alphaRef.current, alpha);
      if (frameRef.current === null) frameRef.current = requestAnimationFrame(runFrame);
    },
    [runFrame]
  );

  useEffect(() => {
    linksRef.current = simLinks;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [linkKey]);

  // Build (or rebuild) the simulation when the visible node set changes.
  // Positions and pins of nodes that survive are carried over, so filtering or
  // expanding nudges the layout instead of scattering it.
  useEffect(() => {
    const previous = new Map(simRef.current.map((node) => [node.id, node]));
    const seeded = seedPositions(
      visibleNodes.map((node) => ({ id: node.id, radius: radiusById.get(node.id) ?? 8 })),
      viewport.width / 2,
      viewport.height / 2,
      30
    );
    let alpha = 1;
    let working = seeded.map((node) => {
      const carried = previous.get(node.id);
      return carried ? { ...node, x: carried.x, y: carried.y, fx: carried.fx, fy: carried.fy } : node;
    });
    // Settle synchronously so the graph arrives laid out and framed rather than
    // detonating on first paint. One canonical path: no dependence on
    // requestAnimationFrame existing, which also keeps this testable.
    const maxTicks = working.length > LARGE_GRAPH_NODES ? SETTLE_TICKS_LARGE : SETTLE_TICKS_SMALL;
    for (let tick = 0; tick < maxTicks && !isSettled(alpha); tick += 1) {
      working = forceTick(working, simLinks, alpha, forceParams);
      alpha = coolAlpha(alpha);
    }
    simRef.current = working;
    alphaRef.current = 0;
    linksRef.current = simLinks;
    setSimNodes(working);
    setPinnedCount(working.filter((node) => node.fx !== null).length);
    if (working.length) fitToNodes(working, captionAllowanceFor(working.length), AUTO_FIT_MAX_SCALE);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [simKey]);

  // Keep the graph framed when the panel resizes. Safe against the feedback loop
  // that a self-referential dependency would create: the svg's size does not
  // depend on the view transform (the transform is on an inner <g>), and
  // ResizeObserver only fires on a real size change.
  useEffect(() => {
    if (simRef.current.length)
      fitToNodes(simRef.current, captionAllowanceFor(simRef.current.length), AUTO_FIT_MAX_SCALE);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewport.width, viewport.height]);

  useEffect(
    () => () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    },
    []
  );

  const positionById = useMemo(() => {
    const map = new Map<string, SimNode>();
    for (const node of simNodes) map.set(node.id, node);
    return map;
  }, [simNodes]);

  // ---- pointer plumbing ---------------------------------------------------
  function svgPointFromEvent(event: { clientX: number; clientY: number }): Point | null {
    const svg = svgRef.current;
    return svg ? viewBoxPoint(svg, event) : null;
  }
  function graphPointFromEvent(event: { clientX: number; clientY: number }): Point | null {
    const point = svgPointFromEvent(event);
    return point ? toGraphPoint(view, point) : null;
  }

  const [dragging, setDragging] = useState<{ nodeId: string; offsetX: number; offsetY: number } | null>(null);
  const [panning, setPanning] = useState<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);
  const didPanRef = useRef(false);

  function setPin(nodeId: string, x: number | null, y: number | null) {
    simRef.current = simRef.current.map((node) =>
      node.id === nodeId ? { ...node, fx: x, fy: y, ...(x !== null && y !== null ? { x, y, vx: 0, vy: 0 } : {}) } : node
    );
    setSimNodes(simRef.current);
    setPinnedCount(simRef.current.filter((node) => node.fx !== null).length);
  }

  function startDrag(nodeId: string, event: ReactPointerEvent<SVGGElement>) {
    if (event.button !== 0) return;
    const node = positionById.get(nodeId);
    const point = graphPointFromEvent(event);
    if (!node || !point) return;
    // NOT preventDefault: calling it on `pointerdown` suppresses the
    // compatibility mouse events, which kills `click` AND `dblclick` on the
    // node -- i.e. selection and expand both silently stop working. Text
    // selection and touch scrolling are already handled by `user-select: none`
    // and `touch-action: none` in CSS, so there is nothing left to prevent.
    // Nodes still swallow their own pointerdown so the background pan never
    // engages underneath them.
    event.stopPropagation();
    try {
      event.currentTarget.setPointerCapture?.(event.pointerId);
    } catch {
      /* synthetic events carry inactive pointer ids; capture is best-effort */
    }
    setDragging({ nodeId, offsetX: point.x - node.x, offsetY: point.y - node.y });
    setPin(nodeId, node.x, node.y);
    // Capturing the pointer means the nodes the drag passes over never receive
    // pointerleave, so whichever one was hovered when the drag began would keep
    // the spotlight -- leaving most of the graph dimmed until something else was
    // hovered. The drag owns the interaction; drop the hover.
    setHovered(null);
    reheat(0.3);
  }
  function moveDrag(nodeId: string, event: ReactPointerEvent<SVGGElement>) {
    if (dragging?.nodeId !== nodeId) return;
    const point = graphPointFromEvent(event);
    if (!point) return;
    event.preventDefault();
    setPin(nodeId, point.x - dragging.offsetX, point.y - dragging.offsetY);
    reheat(0.3);
  }
  function endDrag(event: ReactPointerEvent<SVGGElement>) {
    try {
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    } catch {
      /* best-effort */
    }
    // The pin is left in place: in Neo4j a node stays where you drop it, and
    // the node menu is how you release it.
    setDragging(null);
    setHovered(null);
  }

  function startPan(event: ReactPointerEvent<SVGSVGElement>) {
    if (event.button !== 0) return;
    setMenu(null);
    didPanRef.current = false;
    const point = svgPointFromEvent(event);
    if (!point) return;
    try {
      event.currentTarget.setPointerCapture?.(event.pointerId);
    } catch {
      /* best-effort */
    }
    setPanning({
      pointerId: event.pointerId,
      startX: point.x,
      startY: point.y,
      originX: view.x,
      originY: view.y
    });
  }
  function movePan(event: ReactPointerEvent<SVGSVGElement>) {
    if (!panning || event.pointerId !== panning.pointerId) return;
    const point = svgPointFromEvent(event);
    if (!point) return;
    const dx = point.x - panning.startX;
    const dy = point.y - panning.startY;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) didPanRef.current = true;
    // 1:1 with the cursor in viewBox units, at every zoom level.
    setView((current) => ({ ...current, x: panning.originX + dx, y: panning.originY + dy }));
  }
  function endPan(event: ReactPointerEvent<SVGSVGElement>) {
    if (panning && event.pointerId === panning.pointerId) {
      try {
        event.currentTarget.releasePointerCapture?.(event.pointerId);
      } catch {
        /* best-effort */
      }
      setPanning(null);
    }
  }
  function backgroundClick(event: ReactMouseEvent<SVGSVGElement>) {
    setMenu(null);
    if (didPanRef.current) {
      didPanRef.current = false;
      return;
    }
    if (event.target === event.currentTarget) onSelect(null);
  }
  // Background double-click = zoom in about the pointer (d3-zoom's default).
  // Distinct from a double-click on a NODE, which expands it -- nodes stop
  // propagation, so the two never collide.
  function backgroundDoubleClick(event: ReactMouseEvent<SVGSVGElement>) {
    if (event.target !== event.currentTarget) return;
    const anchor =
      svgPointFromEvent(event) ?? { x: viewport.width / 2, y: viewport.height / 2 };
    setView((current) => zoomAround(current, 2, anchor));
  }

  // Wheel = zoom about the cursor.  Attached natively and non-passive: React
  // registers onWheel passively at the root, where preventDefault is ignored
  // (the page scrolls under the graph) and logs a console violation.
  // Keyed on the ELEMENT rather than on a proxy for "has it mounted yet": the
  // canvas is behind an early return, so a mount-time-only effect reading
  // `svgRef.current` would attach nothing and the wheel would silently do nothing.
  useEffect(() => {
    if (!svgElement) return undefined;
    function handleWheel(event: WheelEvent) {
      event.preventDefault();
      const anchor =
        viewBoxPoint(svgElement as SVGSVGElement, event) ?? {
          x: viewport.width / 2,
          y: viewport.height / 2
        };
      setView((current) => zoomAround(current, wheelZoomFactor(event), anchor));
    }
    svgElement.addEventListener('wheel', handleWheel, { passive: false });
    return () => svgElement.removeEventListener('wheel', handleWheel);
  }, [svgElement, viewport.width, viewport.height]);

  // ---- commands -----------------------------------------------------------
  function zoomAtCenter(factor: number) {
    setView((current) =>
      zoomAround(current, factor, { x: viewport.width / 2, y: viewport.height / 2 })
    );
  }
  function toggleExpand(nodeId: string) {
    setExpanded((current) => {
      const next = new Set(current);
      // Double-clicking an expanded node undoes the expansion, as in Neo4j.
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      return next;
    });
    setDismissed((current) => {
      if (!current.size) return current;
      const next = new Set(current);
      // Expanding a node must be able to bring back something dismissed,
      // otherwise the two commands deadlock.
      for (const neighbor of adjacency.get(nodeId) ?? []) next.delete(neighbor);
      return next;
    });
    reheat();
  }
  function dismiss(nodeId: string) {
    setDismissed((current) => new Set(current).add(nodeId));
    setExpanded((current) => {
      if (!current.has(nodeId)) return current;
      const next = new Set(current);
      next.delete(nodeId);
      return next;
    });
    if (selected?.id === nodeId) onSelect(null);
    reheat();
  }
  function unpinAll() {
    simRef.current = simRef.current.map((node) => ({ ...node, fx: null, fy: null }));
    setSimNodes(simRef.current);
    setPinnedCount(0);
    reheat(0.6);
  }
  function resetView() {
    setActiveLabels(new Set());
    setActiveTypes(new Set());
    setExpanded(new Set());
    setDismissed(new Set());
    setFocus(null);
    unpinAll();
    setMenu(null);
    onSelect(null);
  }
  function toggleLabelFilter(label: string) {
    setActiveLabels((current) => {
      const next = new Set(current);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  }
  function toggleTypeFilter(type: string) {
    setActiveTypes((current) => {
      const next = new Set(current);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  }
  function enterFocus(nodeId: string) {
    setFocus({ nodeId, depth: 1 });
    setMenu(null);
  }
  function exitFocus() {
    setFocus(null);
  }

  // Escape leads back out of focus, and closes the node menu.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape') return;
      setMenu(null);
      setFocus(null);
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  // If the focused or selected node is filtered out of the fetched data
  // entirely, leave rather than strand the operator on an empty canvas.
  useEffect(() => {
    if (focus && !nodes.some((node) => node.id === focus.nodeId)) setFocus(null);
  }, [focus, nodes]);

  const hoverIds = useMemo(() => (hovered ? neighborhood(adjacency, hovered, 1) : null), [adjacency, hovered]);
  // Captions are what turn a dense graph into a smear. Past a threshold, show
  // them only for what is being looked at, until the zoom makes room -- plus the
  // busiest nodes regardless, because an unnamed hub is the one thing an
  // operator most needs named.
  const captionsVisible = visibleNodes.length <= CAPTION_DENSITY_LIMIT || view.k >= 1.7;
  const hubs = useMemo(() => hubIds(degrees, 12), [degrees]);
  const edgeCaptionsVisible = visibleEdges.length <= 60 || view.k >= 1.7;
  const menuNode = menu ? nodes.find((node) => node.id === menu.nodeId) ?? null : null;
  const menuPinned = menu ? positionById.get(menu.nodeId)?.fx !== null : false;
  const focusLabel = focus ? nodeCaption(nodes.find((node) => node.id === focus.nodeId) ?? { id: '', label: '', node_type: '' }, 40) : '';
  const filterActive = activeLabels.size > 0 || activeTypes.size > 0 || dismissed.size > 0 || Boolean(focus);

  if (!nodes.length) return <div className="empty-state">No matching graph facts.</div>;

  return (
    <div className="memory-graph-wrap">
      <div className="graph-stage">
      {/* Controls, the focus chip and the node menu are positioned against the
          CANVAS column, not the whole panel -- against the panel they landed on
          top of the information panel's first heading. */}
      <div className="graph-canvas-col">
      <div className="graph-controls">
        <button type="button" onClick={() => zoomAtCenter(1.4)} aria-label="Zoom in">
          +
        </button>
        <button type="button" onClick={() => zoomAtCenter(1 / 1.4)} aria-label="Zoom out">
          −
        </button>
        <button type="button" onClick={() => fitToNodes(simNodes, captionAllowanceFor(simNodes.length))} aria-label="Fit graph to view">
          Fit
        </button>
        <button type="button" onClick={resetView} aria-label="Reset graph view">
          Reset
        </button>
        {pinnedCount ? (
          <button type="button" onClick={unpinAll} aria-label="Unpin all nodes">
            Unpin {pinnedCount}
          </button>
        ) : null}
        <span className="graph-hint">
          {visibleNodes.length} nodes · {visibleEdges.length} rels · {Math.round(view.k * 100)}%
        </span>
      </div>

      {focus ? (
        <div className="graph-focus-chip" role="status">
          <span>
            Focused on <strong title={focusLabel}>{focusLabel}</strong>
          </span>
          <span className="depth-toggle" role="group" aria-label="Focus depth">
            <button
              type="button"
              className={focus.depth === 1 ? 'active' : ''}
              onClick={() => setFocus({ nodeId: focus.nodeId, depth: 1 })}
            >
              1 hop
            </button>
            <button
              type="button"
              className={focus.depth === 2 ? 'active' : ''}
              onClick={() => setFocus({ nodeId: focus.nodeId, depth: 2 })}
            >
              2 hops
            </button>
          </span>
          <button type="button" className="chip-exit" onClick={exitFocus}>
            Show full graph
          </button>
        </div>
      ) : null}

        <svg
          ref={attachSvg}
          className={panning ? 'memory-graph panning' : 'memory-graph'}
          viewBox={`0 0 ${viewport.width} ${viewport.height}`}
          role="img"
          aria-label="Memory graph"
          onPointerDown={startPan}
          onPointerMove={movePan}
          onPointerUp={endPan}
          onPointerCancel={endPan}
          onClick={backgroundClick}
          onDoubleClick={backgroundDoubleClick}
          onContextMenu={(event) => {
            // Right-clicking the background is not a command; suppress the
            // native menu only over nodes (handled on the node itself).
            if (event.target === event.currentTarget) setMenu(null);
          }}
        >
          <defs>
            {/* One marker per edge class: `orient="auto"` turns the head to the
                path's end tangent, so it reads correctly on a curve too. */}
            {[
              ['arrow-default', '#b9c3cb'],
              ['arrow-superseded_by', '#b56a13'],
              ['arrow-derived_from', '#7c52c4'],
              ['arrow-active', '#2764c4']
            ].map(([id, fill]) => (
              <marker
                key={id}
                id={id}
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth={ARROW_LENGTH}
                markerHeight={ARROW_LENGTH}
                markerUnits="userSpaceOnUse"
                orient="auto"
              >
                <path d="M 0 1 L 9 5 L 0 9 z" fill={fill} />
              </marker>
            ))}
          </defs>
          <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
            {visibleEdges.map((edge) => {
              const source = positionById.get(edge.source_id);
              const target = positionById.get(edge.target_id);
              const sibling = siblings.get(edge.id);
              if (!source || !target || !sibling) return null;
              const highlighted = hovered === edge.source_id || hovered === edge.target_id;
              const dimmed = hoverIds ? !highlighted : false;
              const geometry = edgeGeometry(
                { x: source.x, y: source.y, radius: source.radius },
                { x: target.x, y: target.y, radius: target.radius },
                sibling,
                ARROW_LENGTH
              );
              // These two edge_types no longer arise post-collapse (they were
              // ever only structural, never a predicate a fact would carry),
              // but the check is cheap insurance against a stray edge some
              // other payload shape introduces.
              const knownStyle =
                edge.edge_type === 'superseded_by' || edge.edge_type === 'derived_from' ? edge.edge_type : '';
              const marker = highlighted ? 'arrow-active' : knownStyle ? `arrow-${knownStyle}` : 'arrow-default';
              const isSelected = selected?.id === edge.factNode.id;
              return (
                <g key={edge.id} className={dimmed ? 'graph-edge-group dimmed' : 'graph-edge-group'}>
                  <path
                    className={['graph-edge', knownStyle, highlighted ? 'highlighted' : '', isSelected ? 'selected' : '']
                      .filter(Boolean)
                      .join(' ')}
                    d={geometry.path}
                    fill="none"
                    markerEnd={`url(#${marker})`}
                  />
                  {/* Collapsing the reification onto this edge means the fact
                      it represents is no longer a node of its own -- this is
                      the only remaining way to select it, so a wide invisible
                      stroke stands in for the (visually thin) path as the hit
                      target, exactly as `graph-node-hit` widens a node's
                      circle. Not `preventDefault` on pointerdown, only
                      `stopPropagation` -- same reason a node swallows its own
                      pointerdown: so the background pan never starts
                      underneath a click that was meant to select the fact. */}
                  <path
                    className="graph-edge-hit"
                    d={geometry.path}
                    fill="none"
                    stroke="transparent"
                    strokeWidth={14}
                    role="button"
                    tabIndex={0}
                    aria-label={`Select fact: ${edge.label}`}
                    onPointerDown={(event) => event.stopPropagation()}
                    onClick={(event) => {
                      event.stopPropagation();
                      onSelect(edge.factNode);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        onSelect(edge.factNode);
                      }
                    }}
                  />
                  {edgeCaptionsVisible || highlighted ? (
                    <text
                      className="edge-label"
                      textAnchor="middle"
                      transform={`translate(${geometry.labelX} ${geometry.labelY}) rotate(${geometry.labelAngle})`}
                      dy="-3"
                    >
                      {edge.label}
                    </text>
                  ) : null}
                </g>
              );
            })}
            {visibleNodes.map((node) => {
              const position = positionById.get(node.id);
              if (!position) return null;
              const labels = labelsById.get(node.id) ?? [];
              const fill = nodeColor(node);
              // Short on the canvas; the Inspector and the hover title carry the
              // whole thing. Fact captions are full sentences, and at their full
              // length they overrun every neighbouring node.
              const caption = nodeCaption(node, CANVAS_CAPTION_LENGTH);
              const dimmed = hoverIds ? !hoverIds.has(node.id) : false;
              const pinned = position.fx !== null;
              return (
                <g
                  key={node.id}
                  className={[
                    'graph-node',
                    selected?.id === node.id ? 'selected' : '',
                    dragging?.nodeId === node.id ? 'dragging' : '',
                    pinned ? 'pinned' : '',
                    dimmed ? 'dimmed' : ''
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  transform={`translate(${position.x} ${position.y})`}
                  onClick={(event) => {
                    event.stopPropagation();
                    onSelect(node);
                  }}
                  onDoubleClick={(event) => {
                    event.stopPropagation();
                    toggleExpand(node.id);
                  }}
                  onContextMenu={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    onSelect(node);
                    const point = svgPointFromEvent(event) ?? { x: 0, y: 0 };
                    setMenu({
                      nodeId: node.id,
                      x: (point.x / viewport.width) * 100,
                      y: (point.y / viewport.height) * 100
                    });
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      onSelect(node);
                    }
                  }}
                  onPointerDown={(event) => startDrag(node.id, event)}
                  onPointerMove={(event) => moveDrag(node.id, event)}
                  onPointerUp={endDrag}
                  onPointerCancel={endDrag}
                  onPointerEnter={() => setHovered(node.id)}
                  onPointerLeave={() => setHovered((current) => (current === node.id ? null : current))}
                  /* The accessible name carries the WHOLE caption; only the
                     drawn text is shortened to fit beside the circle. */
                  aria-label={`Select ${nodeCaption(node, 0)}`}
                  role="button"
                  tabIndex={0}
                >
                  {/* Neo4j shows an element's label and caption on hover; an
                      SVG <title> is the native way to do it. aria-label still
                      wins as the accessible name, so this is purely additive. */}
                  <title>{`${labels.join(', ')} · ${node.label}`}</title>
                  {/* Hit area = a small halo around the dot, NOT the caption
                      width.  Caption-sized invisible rects tiled the whole
                      canvas at a few hundred nodes, so every background drag
                      silently grabbed a node and panning was unreachable. */}
                  <circle className="graph-node-hit" r={position.radius + 5} />
                  <circle r={position.radius} fill={fill} />
                  {pinned ? <circle className="graph-node-pin" r={position.radius + 3.5} /> : null}
                  {captionsVisible || selected?.id === node.id || hoverIds?.has(node.id) || hubs.has(node.id) ? (
                    <text x={position.radius + 5} y="4">
                      {caption}
                    </text>
                  ) : null}
                </g>
              );
            })}
          </g>
        </svg>

        {menu && menuNode ? (
          <div className="graph-node-menu" style={{ left: `${menu.x}%`, top: `${menu.y}%` }} role="menu">
            <span className="graph-node-menu-head" title={nodeCaption(menuNode, 200)}>
              {nodeCaption(menuNode, 28)}
            </span>
            <button type="button" role="menuitem" onClick={() => { toggleExpand(menu.nodeId); setMenu(null); }}>
              {expanded.has(menu.nodeId) ? 'Collapse' : 'Expand'}
            </button>
            <button type="button" role="menuitem" onClick={() => enterFocus(menu.nodeId)}>
              Focus
            </button>
            <button
              type="button"
              role="menuitem"
              disabled={!menuPinned}
              onClick={() => {
                setPin(menu.nodeId, null, null);
                setMenu(null);
                reheat();
              }}
            >
              Unpin
            </button>
            <button type="button" role="menuitem" onClick={() => { dismiss(menu.nodeId); setMenu(null); }}>
              Dismiss
            </button>
          </div>
        ) : null}
      </div>

        {/* Neo4j's database-information drawer, doubling as the legend: every
            label with its colour and count, every relationship type, every
            property key.  One panel rather than a separate legend, because it
            is the same information and one filter state drives both. */}
        <aside className="graph-info" aria-label="Graph information">
          <div className="graph-info-section">
            <h3>Node labels</h3>
            {labelRows.length ? (
              <ul>
                {labelRows.map((row) => (
                  <LegendRow
                    key={row.name}
                    row={row}
                    color={labelColor(row.name)}
                    active={activeLabels.has(row.name)}
                    onToggle={() => toggleLabelFilter(row.name)}
                  />
                ))}
              </ul>
            ) : (
              <p className="graph-info-empty">None</p>
            )}
          </div>
          <div className="graph-info-section">
            <h3>Relationship types</h3>
            {relationshipRows.length ? (
              <ul>
                {relationshipRows.map((row) => (
                  <LegendRow
                    key={row.name}
                    row={row}
                    active={activeTypes.has(row.name)}
                    onToggle={() => toggleTypeFilter(row.name)}
                  />
                ))}
              </ul>
            ) : (
              <p className="graph-info-empty">None</p>
            )}
          </div>
          <div className="graph-info-section">
            <h3>Property keys</h3>
            {propertyRows.length ? (
              <ul className="graph-info-keys">
                {propertyRows.map((row) => (
                  <li key={row.name}>
                    <span className="graph-info-name" title={row.name}>
                      {row.name}
                    </span>
                    <span className="graph-info-count">({row.count})</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="graph-info-empty">None</p>
            )}
          </div>
          {filterActive ? (
            <button type="button" className="graph-info-clear" onClick={resetView}>
              Clear filters
            </button>
          ) : null}
        </aside>
      </div>
      {visibleNodes.length ? null : (
        <p className="graph-info-empty graph-empty-note">
          Every node is filtered out. Clear the filters to bring the graph back.
        </p>
      )}
    </div>
  );
}

function LegendRow({
  row,
  color,
  active,
  onToggle
}: {
  row: Tally;
  color?: string;
  active: boolean;
  onToggle: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        className={active ? 'graph-info-row active' : 'graph-info-row'}
        onClick={onToggle}
        aria-pressed={active}
        /* Explicit, because the name and count are separate inline elements:
           browsers join them with a space, jsdom does not, so the computed
           accessible name would otherwise differ between the two. */
        aria-label={`${row.name} (${row.count})`}
        title={`${row.name} (${row.count})`}
      >
        {color ? <span className="graph-info-swatch" style={{ background: color }} /> : null}
        <span className="graph-info-name">{row.name}</span>
        <span className="graph-info-count">({row.count})</span>
      </button>
    </li>
  );
}

/**
 * One endpoint of a relationship triple: its entity-type chip (the same
 * hashed colour the graph canvas uses, via `labelColor`) plus its name.
 * `labels` is null whenever the calling endpoint could not resolve the real
 * entity node (see `resolveFactEndpoints`) -- degrades to a plain name with
 * no chip rather than fabricating a type.
 */
function EntityChip({ name, labels }: { name: string; labels?: readonly string[] | null }) {
  if (!name) return <span className="fact-endpoint-name empty">unknown</span>;
  const type = labels && labels.length ? labels[0] : null;
  return (
    <span className="fact-endpoint">
      {type ? (
        <span className="label-chip" style={{ background: labelColor(type) }}>
          {type}
        </span>
      ) : null}
      <span className="fact-endpoint-name">{name}</span>
    </span>
  );
}

/**
 * A relationship rendered as what it is -- a labelled edge between two named
 * entities -- rather than its `fact` sentence standing in for a third entity
 * of its own (the defect this component exists to fix: a bare sentence like
 * "Jedai Knowledge Base ingests Special Offers" sitting in a list next to
 * real entity names is indistinguishable from one).
 *
 * The sentence is not deleted -- it is the embedded retrieval surface and
 * genuinely useful -- but it prints only when it carries a qualifier, number,
 * or condition the triple does not (`isRedundantFactSentence`); a pure
 * restatement of the triple is suppressed outright rather than shown twice.
 */
function FactTriple({
  triple,
  subjectLabels,
  objectLabels
}: {
  triple: RelationshipTriple;
  subjectLabels?: readonly string[] | null;
  objectLabels?: readonly string[] | null;
}) {
  const showSentence = triple.fact && !isRedundantFactSentence(triple);
  return (
    <div className="fact-triple">
      <div className="fact-triple-row">
        <EntityChip name={triple.subject} labels={subjectLabels} />
        <span className="fact-triple-arrow" aria-hidden="true">
          →
        </span>
        <span className="fact-triple-predicate">{triple.predicate || '—'}</span>
        <span className="fact-triple-arrow" aria-hidden="true">
          →
        </span>
        <EntityChip name={triple.object} labels={objectLabels} />
      </div>
      {showSentence ? <p className="fact-sentence">{triple.fact}</p> : null}
    </div>
  );
}

function FactTable({
  graph,
  selected,
  onSelect,
  onOpenExplainability
}: {
  graph: GraphView | null;
  selected: GraphNode | null;
  onSelect: (node: GraphNode) => void;
  onOpenExplainability: (node: GraphNode) => void;
}) {
  const nodes = factNodes(graph);
  const allNodes = graph?.nodes ?? [];
  const edges = graph?.edges ?? [];
  return (
    <table className="data-table fact-table">
      <thead>
        <tr>
          <th>Type</th>
          <th>Relationship</th>
          <th>Status</th>
          <th>Confidence</th>
          <th>Observed</th>
          <th>Explain</th>
        </tr>
      </thead>
      <tbody>
        {nodes.map((node) => {
          const triple = buildTriple(node.properties);
          const { subject, object } = resolveFactEndpoints(node.id, allNodes, edges);
          return (
            <tr
              key={node.id}
              className={selected?.id === node.id ? 'selected-row' : ''}
              onClick={() => onSelect(node)}
            >
              <td>{node.memory_type ?? node.relationship_type ?? 'unknown'}</td>
              <td>
                <FactTriple triple={triple} subjectLabels={endpointLabels(subject)} objectLabels={endpointLabels(object)} />
              </td>
              <td>{node.status ?? 'unknown'}</td>
              <td>{formatNumber(node.confidence ?? 0, 2)}</td>
              <td>{node.observed_count ?? 0}</td>
              <td>
                <button
                  className="text-button"
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();
                    onOpenExplainability(node);
                  }}
                >
                  Open
                </button>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function ExplainabilityScreen({
  graph,
  selected,
  evidence,
  timeline,
  lineage,
  controlPlane,
  onBackToSearch
}: {
  graph: GraphView | null;
  selected: GraphNode | null;
  evidence: MemoryEvidence | null;
  timeline: TimelineEntry[];
  lineage: GraphEdge[];
  controlPlane: ControlPlaneResponse | null;
  onBackToSearch: () => void;
}) {
  return (
    <section className="split-layout">
      <div className="panel">
        <div className="panel-head">
          <h2>Fact</h2>
          <button className="secondary-button" type="button" onClick={onBackToSearch}>
            <Search size={15} />
            Search facts
          </button>
        </div>
        <Inspector graph={graph} selected={selected} evidence={evidence} timeline={timeline} />
      </div>
      <div className="stack">
        <div className="panel">
          <h2>Evidence episodes</h2>
          {(evidence?.episodes ?? []).map((episode) => (
            <div className="list-row" key={episode.uuid}>
              <strong>{episode.name}</strong>
              <span>{episode.reference_time}</span>
              <pre>{episode.body}</pre>
            </div>
          ))}
          {!evidence?.episodes.length ? <div className="empty-state">No evidence loaded.</div> : null}
        </div>
        <div className="panel">
          <h2>Lineage</h2>
          {lineage.map((edge) => (
            <div className="list-row compact" key={edge.id}>
              <strong>{edge.edge_type}</strong>
              <span>{edge.label}</span>
            </div>
          ))}
          {!lineage.length ? <div className="empty-state">No lineage edges for this fact.</div> : null}
        </div>
        <div className="panel">
          <h2>Source trace</h2>
          <KeyValueTable values={controlPlane?.policy.source_trace ?? {}} />
        </div>
      </div>
    </section>
  );
}

function ProjectMemoryScreen({
  status,
  onChanged
}: {
  status: ProjectMemoryStatus | null;
  onChanged: (status: ProjectMemoryStatus) => void;
}) {
  const [projectGoal, setProjectGoal] = useState('');
  const [memoryGoal, setMemoryGoal] = useState('');
  const [keepText, setKeepText] = useState('');
  const [excludeText, setExcludeText] = useState('');
  const [rulesText, setRulesText] = useState('');
  const [allowedTypes, setAllowedTypes] = useState<string[]>([]);
  const [protectedTypes, setProtectedTypes] = useState<string[]>([]);
  const [minSalience, setMinSalience] = useState(0);
  const [maxMemories, setMaxMemories] = useState(12);
  const [dedupThreshold, setDedupThreshold] = useState(0.87);
  const [message, setMessage] = useState('');

  const loadConfig = useCallback((config: ProjectMemoryConfig) => {
    setProjectGoal(config.project_goal);
    setMemoryGoal(config.memory_goal);
    setKeepText(config.keep.join('\n'));
    setExcludeText(config.exclude.join('\n'));
    setRulesText(config.rules.join('\n'));
    setAllowedTypes(config.allowed_memory_types);
    setProtectedTypes(config.protected_memory_types);
    setMinSalience(config.min_salience);
    setMaxMemories(config.max_memories_per_candidate);
    setDedupThreshold(config.dedup_threshold);
  }, []);

  useEffect(() => {
    if (status?.config) {
      loadConfig(status.config);
      return;
    }
    if (!status) return;
    setProjectGoal('Deliver the project outcome safely, correctly, and with shared alignment.');
    setMemoryGoal('Preserve durable information that helps every project agent coordinate and make correct decisions.');
    setKeepText(
      [
        'Cross-team decisions, requirements, commitments, and their rationale.',
        'Incidents, blockers, and recovery lessons that affect delivery.',
        'Current shared state only when it changes another agent’s work.'
      ].join('\n')
    );
    setExcludeText(
      [
        'Personal preferences or private agent scratch work.',
        'Unverified speculation and transient conversational wording.'
      ].join('\n')
    );
    setRulesText('Attach a source and preserve valid_from or valid_to when the fact is temporary.');
    setAllowedTypes(['requirement', 'directive', 'state', 'decision', 'incident', 'rollup']);
    setProtectedTypes(['requirement', 'decision', 'incident']);
  }, [loadConfig, status]);

  function toggleAllowed(memoryType: string) {
    setAllowedTypes((current) => {
      if (current.includes(memoryType)) {
        setProtectedTypes((protectedCurrent) =>
          protectedCurrent.filter((item) => item !== memoryType)
        );
        return current.filter((item) => item !== memoryType);
      }
      return [...current, memoryType];
    });
  }

  function toggleProtected(memoryType: string) {
    setProtectedTypes((current) =>
      current.includes(memoryType)
        ? current.filter((item) => item !== memoryType)
        : [...current, memoryType]
    );
  }

  async function savePolicy(event: FormEvent) {
    event.preventDefault();
    const saved = await saveProjectMemoryConfig({
      project_goal: projectGoal,
      memory_goal: memoryGoal,
      keep: lines(keepText),
      exclude: lines(excludeText),
      rules: lines(rulesText),
      allowed_memory_types: allowedTypes,
      protected_memory_types: protectedTypes,
      min_salience: minSalience,
      max_memories_per_candidate: maxMemories,
      dedup_threshold: dedupThreshold,
      configured_by: 'admin-ui'
    });
    onChanged(await getProjectMemoryConfig());
    setMessage(`Saved ${saved.version}`);
  }

  return (
    <section className="project-memory-grid">
      <div className="panel project-memory-editor">
        <div className="panel-head">
          <div>
            <h2>Project-memory policy</h2>
            <span className="subtle">
              Every save creates an immutable version used by the project-scoped dream.
            </span>
          </div>
          <span className="message">{message}</span>
        </div>
        <form className="project-memory-form" onSubmit={(event) => void savePolicy(event)}>
          <label>
            <span>Overall project goal</span>
            <textarea
              aria-label="Overall project goal"
              rows={3}
              value={projectGoal}
              onChange={(event) => setProjectGoal(event.target.value)}
            />
          </label>
          <label>
            <span>Project memory goal</span>
            <textarea
              aria-label="Project memory goal"
              rows={3}
              value={memoryGoal}
              onChange={(event) => setMemoryGoal(event.target.value)}
            />
          </label>
          <label>
            <span>Keep — one criterion per line</span>
            <textarea
              aria-label="Project memory keep criteria"
              rows={5}
              value={keepText}
              onChange={(event) => setKeepText(event.target.value)}
            />
          </label>
          <label>
            <span>Exclude — one criterion per line</span>
            <textarea
              aria-label="Project memory exclusion criteria"
              rows={4}
              value={excludeText}
              onChange={(event) => setExcludeText(event.target.value)}
            />
          </label>
          <label>
            <span>Additional rules — one per line</span>
            <textarea
              aria-label="Project memory rules"
              rows={4}
              value={rulesText}
              onChange={(event) => setRulesText(event.target.value)}
            />
          </label>
          <fieldset>
            <legend>Allowed memory types</legend>
            <div className="type-toggle-grid">
              {projectMemoryTypes.map((memoryType) => (
                <label key={memoryType} className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={allowedTypes.includes(memoryType)}
                    onChange={() => toggleAllowed(memoryType)}
                  />
                  <span>{memoryType}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <fieldset>
            <legend>Protected from generic pruning</legend>
            <div className="type-toggle-grid">
              {projectMemoryTypes.map((memoryType) => (
                <label key={memoryType} className="checkbox-row">
                  <input
                    type="checkbox"
                    disabled={!allowedTypes.includes(memoryType)}
                    checked={protectedTypes.includes(memoryType)}
                    onChange={() => toggleProtected(memoryType)}
                  />
                  <span>{memoryType}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <div className="policy-number-grid">
            <label>
              <span>Minimum salience</span>
              <input
                aria-label="Minimum salience"
                type="number"
                min="0"
                max="1"
                step="0.01"
                value={minSalience}
                onChange={(event) => setMinSalience(Number(event.target.value))}
              />
            </label>
            <label>
              <span>Max memories per candidate</span>
              <input
                aria-label="Max memories per candidate"
                type="number"
                min="1"
                max="100"
                value={maxMemories}
                onChange={(event) => setMaxMemories(Number(event.target.value))}
              />
            </label>
            <label>
              <span>Semantic dedup threshold</span>
              <input
                aria-label="Semantic dedup threshold"
                type="number"
                min="0"
                max="1"
                step="0.01"
                value={dedupThreshold}
                onChange={(event) => setDedupThreshold(Number(event.target.value))}
              />
            </label>
          </div>
          <button className="primary-button" type="submit">
            Save project-memory version
          </button>
        </form>
      </div>
      <div className="stack">
        <div className="panel">
          <h2>Active policy</h2>
          <KeyValue label="Configured" value={status?.configured ? 'yes' : 'no'} />
          <KeyValue label="Version" value={status?.config?.version ?? 'not configured'} mono />
          <KeyValue label="Project scope" value={status?.project_scope.key ?? ''} mono />
          <KeyValue label="Configured by" value={status?.config?.configured_by ?? ''} />
          <KeyValue label="Candidates" value={String(status?.candidate_count ?? 0)} mono />
          <KeyValue label="Pending dream" value={String(status?.pending_candidate_count ?? 0)} mono />
        </div>
        <div className="panel">
          <h2>Version history</h2>
          <table className="data-table">
            <thead>
              <tr>
                <th>Version</th>
                <th>Editor</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {(status?.versions ?? []).map((version) => (
                <tr key={version.version}>
                  <td>
                    <button
                      className="link-button"
                      type="button"
                      onClick={() => loadConfig(version)}
                    >
                      {version.version}
                    </button>
                  </td>
                  <td>{version.configured_by}</td>
                  <td>{version.updated_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function PromptsScreen({
  prompts,
  onPromptsChanged
}: {
  prompts: TenantPrompts | null;
  onPromptsChanged: (prompts: TenantPrompts) => void;
}) {
  const [selectedKey, setSelectedKey] = useState('');
  const [promptText, setPromptText] = useState('');
  const [motiveName, setMotiveName] = useState('');
  const [sourceProfile, setSourceProfile] = useState('');
  const [sourceProfileVersion, setSourceProfileVersion] = useState('');
  const [message, setMessage] = useState('');

  useEffect(() => {
    if (!prompts) return;
    const current = prompts.current;
    setSelectedKey(current.key);
    setPromptText(current.prompt_text);
    setMotiveName(current.motive_name || '');
    setSourceProfile(current.profile || 'support-memory');
    setSourceProfileVersion(current.profile_version || 'v1');
  }, [prompts]);

  const loadVersion = (key: string) => {
    if (!prompts) return;
    const version = [...prompts.versions, ...prompts.library].find((candidate) => candidate.key === key);
    if (!version) return;
    setSelectedKey(version.key);
    setPromptText(version.prompt_text);
    setMotiveName(version.motive_name || '');
    setSourceProfile(version.profile || sourceProfile || 'support-memory');
    setSourceProfileVersion(version.profile_version || sourceProfileVersion || 'v1');
    setMessage('');
  };

  async function savePrompt(event: FormEvent) {
    event.preventDefault();
    const next = await saveTenantPrompts({
      prompt_text: promptText,
      motive_name: motiveName || undefined,
      source_profile: sourceProfile,
      source_profile_version: sourceProfileVersion
    });
    onPromptsChanged(next);
    setMessage(`Saved ${next.current.version || next.current.label}`);
  }

  const resolvedMotiveName = prompts?.resolved_motive.name ?? '';
  const resolvedMotive = prompts?.motives.find((motive) => motive.name === resolvedMotiveName);
  const selectedMotive = prompts?.motives.find((motive) => motive.name === motiveName);
  const displayedMotive = selectedMotive ?? resolvedMotive;
  const allVersions = [...(prompts?.versions ?? []), ...(prompts?.library ?? [])];

  return (
    <section className="prompt-workspace">
      <div className="panel prompt-editor-panel">
        <div className="panel-head">
          <div>
            <h2>Current prompt</h2>
            <span className="subtle">{prompts?.active_pack ?? 'tenant-active'} saves as a new tenant version</span>
          </div>
          <span className="message">{message}</span>
        </div>
        <form className="prompt-editor-form" onSubmit={(event) => void savePrompt(event)}>
          <div className="prompt-controls">
            <label>
              <span>Load version</span>
              <select value={selectedKey} onChange={(event) => loadVersion(event.target.value)}>
                {(prompts?.versions ?? []).map((version) => (
                  <option key={version.key} value={version.key}>
                    {version.label}
                  </option>
                ))}
                {(prompts?.library ?? []).map((version) => (
                  <option key={version.key} value={version.key}>
                    {version.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Motive override</span>
              <select value={motiveName} onChange={(event) => setMotiveName(event.target.value)}>
                <option value="">Automatic</option>
                {(prompts?.motives ?? []).map((motive) => (
                  <option key={motive.name} value={motive.name}>
                    {motive.name}
                  </option>
                ))}
              </select>
            </label>
            <button className="primary-button" type="submit">
              Save new version
            </button>
          </div>
          <textarea
            className="full-prompt-editor"
            aria-label="Current prompt"
            value={promptText}
            onChange={(event) => setPromptText(event.target.value)}
          />
        </form>
      </div>
      <div className="panel prompt-side-panel">
        <h2>Resolved run policy</h2>
        {displayedMotive ? (
          <>
            <KeyValue label="Resolved Motive" value={displayedMotive.name} mono />
            <KeyValue
              label="Source"
              value={motiveName ? 'prompt override' : prompts?.resolved_motive.source_label ?? 'automatic'}
            />
            <KeyValue label="Goal" value={displayedMotive.goal} />
            <KeyValue label="Allowed types" value={displayedMotive.allowed_memory_types.join(', ') || 'all'} />
            <KeyValue
              label="Dedup"
              value={displayedMotive.dedup_threshold == null ? 'policy default' : String(displayedMotive.dedup_threshold)}
              mono
            />
            <KeyValue
              label="Budget share"
              value={
                displayedMotive.retrieval_budget_share == null
                  ? 'policy default'
                  : String(displayedMotive.retrieval_budget_share)
              }
              mono
            />
          </>
        ) : (
          <div className="empty-state">No Motive catalog loaded.</div>
        )}
      </div>
      <div className="panel prompt-side-panel">
        <h2>Versions</h2>
        <table className="data-table">
          <thead>
            <tr>
              <th>Version</th>
              <th>Override</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {allVersions.map((version) => (
              <tr key={version.key}>
                <td>{version.label}</td>
                <td>{version.motive_name || 'Automatic'}</td>
                <td>{version.updated_at || version.created_at || 'library'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function DreamingScreen({
  activeScope,
  config,
  prompts,
  dreamStatus,
  evolution,
  dreams,
  archive,
  onRunDreamSequence
}: {
  activeScope: string;
  config: TenantConfig | null;
  prompts: TenantPrompts | null;
  dreamStatus: DreamSequenceStatus | null;
  evolution: EvolutionProof | null;
  dreams: DreamRunsResponse | null;
  archive: ArchiveResponse | null;
  onRunDreamSequence: (agentId: string, motiveName: string) => Promise<void>;
}) {
  const [agentId, setAgentId] = useState('');
  const [motiveOverride, setMotiveOverride] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const agentOptions = useMemo(() => {
    const observed = config?.tenant.agent_ids ?? [];
    const scopeAgent = activeScope.startsWith('agent:') ? activeScope.slice('agent:'.length) : '';
    return Array.from(new Set([scopeAgent, ...observed].filter(Boolean)));
  }, [activeScope, config?.tenant.agent_ids]);
  useEffect(() => {
    if (!agentId && agentOptions.length) setAgentId(agentOptions[0]);
  }, [agentId, agentOptions]);
  const resolvedMotiveName = prompts?.resolved_motive.name ?? '';
  const resolvedMotive = prompts?.motives.find((motive) => motive.name === resolvedMotiveName);
  const selectedMotive = prompts?.motives.find((motive) => motive.name === motiveOverride) ?? resolvedMotive;
  const governingMotiveName = motiveOverride || resolvedMotiveName;
  const governingSourceLabel = motiveOverride
    ? 'admin override'
    : prompts?.resolved_motive.source_label ?? 'automatic';
  const evidence = prompts?.motive_evidence;
  const running = submitting || Boolean(dreamStatus?.running);

  async function run() {
    if (!agentId || running) return;
    setSubmitting(true);
    try {
      await onRunDreamSequence(agentId, motiveOverride);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="stack">
      <section className="panel dreaming-run-panel">
        <div>
          <h2>Dream sequence</h2>
          <span className="subtle">Runs formation, consolidation, pruning, dream-agent audit, and before/after proof for the selected scope.</span>
        </div>
        <label>
          <span>Agent</span>
          <select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
            {agentOptions.map((agent) => (
              <option key={agent} value={agent}>
                {agent}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Motive override</span>
          <select value={motiveOverride} onChange={(event) => setMotiveOverride(event.target.value)}>
            <option value="">Automatic</option>
            {(prompts?.motives ?? []).map((motive) => (
              <option key={motive.name} value={motive.name}>
                {motive.name}
              </option>
            ))}
          </select>
        </label>
        <button className="primary-button" type="button" disabled={running || !agentOptions.length} onClick={() => void run()}>
          {running ? <Lock size={15} /> : <Play size={15} />}
          {running ? 'Dream Sequence Running' : 'Run Dream Sequence'}
        </button>
        <div className="subtle run-policy-note">
          Resolved Motive: {governingMotiveName || 'none'} ({governingSourceLabel}); Writes into: {activeScope}
        </div>
      </section>
      <section className="metric-grid">
        <Metric label="Episodes" value={formatNumber(evolution?.episode_count)} />
        <Metric label="Pending" value={formatNumber(evolution?.pending_episode_count)} />
        <Metric label="Dream runs" value={formatNumber(evolution?.dream_run_count)} />
        <Metric label="Decisions" value={formatNumber(evolution?.decision_count)} />
        <Metric label="Inactive" value={formatNumber(evolution?.inactive_relationship_count)} />
      </section>
      <section className="panel-grid two">
        <div className="panel">
          <h2>Status log</h2>
          {dreamStatus ? (
            <div className="status-log">
              <KeyValue label="Run" value={dreamStatus.run_id} mono />
              <KeyValue label="Status" value={dreamStatus.status} />
              <KeyValue label="Resolved Motive" value={dreamStatus.motive_name || 'none'} mono />
              <KeyValue label="Source" value={dreamStatus.motive_source_label ?? dreamStatus.motive_source ?? 'automatic'} />
              {(dreamStatus.events ?? []).map((event) => (
                <div className="log-entry" key={`${event.at}-${event.phase}-${event.message}`}>
                  <span>{event.at}</span>
                  <strong>{event.phase}</strong>
                  <p>{event.message}</p>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty-state">No dream sequence has run from this UI session.</div>
          )}
        </div>
        <div className="panel">
          <h2>Policy and proof</h2>
          <h3 className="panel-subhead">Governing Motive (policy now)</h3>
          <KeyValue label="Scope" value={prompts?.resolved_motive.scope || activeScope} mono />
          <KeyValue label="Motive" value={governingMotiveName || 'none'} mono />
          <KeyValue label="Source" value={governingSourceLabel} />
          {selectedMotive ? (
            <>
              <KeyValue label="Goal" value={selectedMotive.goal} />
              <KeyValue label="Allowed types" value={selectedMotive.allowed_memory_types.join(', ') || 'all'} />
            </>
          ) : (
            <div className="empty-state">
              No Motive is resolvable for this scope; formation would run without Motive gating.
            </div>
          )}
          <h3 className="panel-subhead">Formed under (evidence on these facts)</h3>
          {evidence && evidence.observed.length ? (
            <>
              {evidence.divergent ? (
                <div className="motive-divergence" role="status">
                  Policy has changed since these facts formed. The Motive above governs the NEXT
                  run; the rows below record what actually formed the current facts.
                </div>
              ) : null}
              {evidence.observed.map((item) => (
                <KeyValue
                  key={`${item.motive_name}:${item.motive_version_digest}`}
                  label={item.motive_name || 'no Motive stamped'}
                  value={`${item.fact_count} fact${item.fact_count === 1 ? '' : 's'}${
                    item.motive_version_digest ? ` · digest ${item.motive_version_digest.slice(0, 12)}` : ''
                  }`}
                  mono
                />
              ))}
            </>
          ) : (
            <div className="empty-state">No materialized facts in this scope yet.</div>
          )}
          <h3 className="panel-subhead">Capabilities</h3>
          {(dreamStatus?.capability_signals ?? []).map((signal) => (
            <div className="list-row compact" key={signal.name}>
              <strong>{signal.name}</strong>
              <span>{signal.description}</span>
            </div>
          ))}
          {!(dreamStatus?.capability_signals ?? []).length ? (
            <div className="empty-state">Run a dream sequence to capture capability signals.</div>
          ) : null}
        </div>
        <div className="panel">
          <h2>Dream runs</h2>
          <table className="data-table">
            <thead>
              <tr>
                <th>Ran at</th>
                <th>Job</th>
                <th>Motive</th>
                <th>Episodes</th>
                <th>Created</th>
                <th>Reinforced</th>
                <th>Superseded</th>
                <th>Pruned</th>
                <th>Decisions</th>
              </tr>
            </thead>
            <tbody>
              {(dreams?.runs ?? []).map((run) => (
                <tr key={run.uuid}>
                  <td>{run.ran_at}</td>
                  <td>{run.job_name}</td>
                  <td>{run.motive_name || 'unrecorded'}</td>
                  <td>{run.processed_episodes}</td>
                  <td>{run.created_relationships}</td>
                  <td>{run.reinforced_relationships}</td>
                  <td>{run.superseded_relationships}</td>
                  <td>{run.pruned_relationships}</td>
                  <td>{run.decision_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="panel">
          <h2>Dream-agent decisions</h2>
          <table className="data-table">
            <thead>
              <tr>
                <th>Ran at</th>
                <th>Decision</th>
                <th>Summary</th>
              </tr>
            </thead>
            <tbody>
              {(dreams?.decisions ?? []).map((decision) => (
                <tr key={decision.uuid}>
                  <td>{decision.ran_at}</td>
                  <td>{decision.decision_type}</td>
                  <td>{decision.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="panel-grid two">
        <div className="panel">
          <h2>Signals</h2>
          {(evolution?.signals ?? []).map((signal) => (
            <div className="readiness-row" key={signal.name}>
              {signal.observed ? <CheckCircle2 size={16} /> : <Circle size={16} />}
              <span>{signal.name}</span>
              <strong>{signal.count}</strong>
            </div>
          ))}
        </div>
        <div className="panel">
          <h2>Archive</h2>
          <table className="data-table archive-table">
            <thead>
              <tr>
                <th>Status</th>
                <th>Type</th>
                <th>Relationship</th>
              </tr>
            </thead>
            <tbody>
              {/* /api/archive returns only the memory nodes, no entity nodes
                  or edges -- so unlike the Memory Explorer's FactTable there
                  is no entity-type label to colour these chips with. The
                  triple still renders (subject/object are resolved entity
                  names off `fact.properties`, per `knowledge_graph`), just
                  without the chip colour. */}
              {(archive?.facts ?? []).map((fact) => (
                <tr key={fact.id}>
                  <td>{fact.status}</td>
                  <td>{fact.memory_type ?? fact.relationship_type}</td>
                  <td>
                    <FactTriple triple={buildTriple(fact.properties)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function PolicyRolloutScreen({ rollout }: { rollout: PolicyRolloutStatus | null }) {
  if (!rollout) {
    return <div className="loading">Loading policy rollout evidence...</div>;
  }
  const policy = rollout.compiled_effective_policy;
  const certification = rollout.certification;
  if (!policy || !rollout.active_alias || !certification) {
    return (
      <section className="panel">
        <h2>Policy rollout</h2>
        <div className="empty-state">
          No active <code>{rollout.alias}</code> alias is registered for <code>{scopeLabel(rollout.scope)}</code>.
          Stage and certify an immutable contract before registering the existing baseline or activating a replacement.
        </div>
      </section>
    );
  }
  return (
    <div className="stack">
      <section className="metric-grid">
        <Metric label="Certification" value={certification.passed ? 'passed' : 'blocked'} />
        <Metric label="Stability" value={formatNumber(certification.stability_score, 3)} />
        <Metric label="Policy delta" value={formatNumber(certification.policy_delta, 3)} />
        <Metric label="Replay noise" value={formatNumber(certification.replay_flip_rate, 3)} />
        <Metric label="Shadow stages" value={String(rollout.shadow_stages.length)} />
      </section>
      <section className="panel-grid two">
        <div className="panel">
          <h2>Compiled effective policy</h2>
          <KeyValue label="Live alias" value={rollout.active_alias.alias} mono />
          <KeyValue label="Contract digest" value={policy.contract_digest} mono />
          <KeyValue label="Prior digest" value={rollout.active_alias.previous_contract_digest ?? 'none'} mono />
          <KeyValue label="Motive" value={policy.motive.name} mono />
          <KeyValue label="Goal" value={policy.motive.goal} />
          <KeyValue label="Pinned model" value={policy.replay_options.model_identifier} mono />
          <KeyValue label="Temperature / repetitions" value={`${policy.replay_options.temperature} / ${policy.replay_options.repetitions}`} />
          <KeyValue label="Staged" value={policy.staged_at} mono />
        </div>
        <div className="panel">
          <h2>Source trace and routing</h2>
          {Object.entries(policy.source_trace).map(([key, value]) => (
            <KeyValue key={key} label={key} value={value} mono />
          ))}
          <KeyValue label="Production scope" value={rollout.expected_episode_routing?.production_scope ?? ''} mono />
          <KeyValue label="Allowed memory types" value={rollout.expected_episode_routing?.allowed_memory_types.join(', ') || 'none'} />
          <KeyValue label="Shadow routing" value={rollout.expected_episode_routing?.shadow_visibility ?? ''} />
        </div>
        <div className="panel">
          <h2>Certification evidence</h2>
          <KeyValue label="Fixture corpus" value={certification.corpus_name} />
          <KeyValue label="Corpus digest" value={certification.corpus_digest} mono />
          <KeyValue label="Status" value={certification.passed ? 'certified' : 'activation blocked'} />
          {certification.protected_invariants.map((invariant) => (
            <div className="readiness-row" key={invariant.name}>
              {invariant.passed ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
              <span>{invariant.name}</span>
              <strong>{invariant.passed ? 'pass' : 'fail'}</strong>
            </div>
          ))}
          {certification.failures.length ? (
            <div className="error-banner">{certification.failures.join(', ')}</div>
          ) : null}
        </div>
        <div className="panel">
          <h2>Shadow-stage divergence</h2>
          {rollout.shadow_stages.length ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Episodes</th>
                  <th>Delta / allowed</th>
                  <th>Candidate</th>
                </tr>
              </thead>
              <tbody>
                {rollout.shadow_stages.map((stage) => (
                  <tr key={stage.stage_id}>
                    <td>{stage.status}</td>
                    <td>{stage.observed_episode_count} / {stage.required_episode_count}</td>
                    <td>{formatNumber(stage.comparison?.policy_delta, 3)} / {formatNumber(stage.allowed_disposition_delta, 3)}</td>
                    <td><code>{stage.candidate_contract_digest.slice(0, 16)}…</code></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="empty-state">No shadow window has completed for this alias.</div>
          )}
        </div>
      </section>
    </div>
  );
}

function TenantSetupScreen({
  config,
  onReload,
  onTenantChanged,
  onTenantPurged
}: {
  config: TenantConfig | null;
  onReload: () => Promise<void>;
  onTenantChanged: (config: TenantConfig) => void;
  onTenantPurged: (result: TenantPurgeResult) => Promise<void>;
}) {
  const [provider, setProvider] = useState('litellm');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [message, setMessage] = useState('');
  const [purging, setPurging] = useState(false);

  useEffect(() => {
    if (!config?.llm) return;
    setProvider(config.llm.provider || 'litellm');
    setModel(config.llm.model || '');
    setBaseUrl(config.llm.base_url || '');
  }, [config]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const next = await saveTenantLlm({ provider, api_key: apiKey, model, base_url: baseUrl });
    setApiKey('');
    setMessage('Saved');
    onTenantChanged(next);
  }

  async function clear() {
    const result = await clearTenantLlm();
    setApiKey('');
    setMessage(result.cleared ? 'Cleared' : 'No stored credential');
    onTenantChanged(result.config);
  }

  async function purge() {
    const tenantId = config?.tenant.tenant_id ?? '';
    const confirmed = window.confirm(
      `Purge generated memory state for tenant "${tenantId}"? Raw episodes and LLM credentials are preserved; graph, dream history, prompt versions, processed markers, and agent registrations are reset.`
    );
    if (!confirmed) return;
    setPurging(true);
    try {
      const result = await purgeTenantState();
      setMessage(`Purged; preserved ${result.purged.raw_episodes_preserved} raw episodes`);
      await onTenantPurged(result);
    } finally {
      setPurging(false);
    }
  }

  return (
    <section className="panel-grid two">
      <div className="panel">
        <h2>Tenant LLM</h2>
        <form className="form-grid" onSubmit={(event) => void submit(event)}>
          <label>
            <span>Provider</span>
            <select value={provider} onChange={(event) => setProvider(event.target.value)}>
              <option value="litellm">JedAI Gateway (LiteLLM)</option>
              <option value="openai">OpenAI-compatible</option>
            </select>
          </label>
          <label>
            <span>Model</span>
            <input value={model} onChange={(event) => setModel(event.target.value)} />
          </label>
          <label className="wide">
            <span>API key</span>
            <input
              type="password"
              value={apiKey}
              autoComplete="off"
              onChange={(event) => setApiKey(event.target.value)}
            />
          </label>
          <label className="wide">
            <span>Base URL</span>
            <input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
          </label>
          <div className="button-row">
            <button className="primary-button" type="submit">
              <KeyRound size={15} />
              Save
            </button>
            <button className="danger-button" type="button" onClick={() => void clear()}>
              Clear
            </button>
            <button className="secondary-button" type="button" onClick={() => void onReload()}>
              <RefreshCw size={15} />
              Reload
            </button>
            <span className="message">{message}</span>
          </div>
        </form>
      </div>
      <div className="panel">
        <h2>Credential status</h2>
        <KeyValue label="Tenant" value={config?.llm.tenant_id} mono />
        <KeyValue
          label="Credential"
          value={
            !config?.llm.has_api_key
              ? 'not configured'
              : config.llm.source === 'environment'
                ? `active via ${config.llm.environment_key_env ?? 'environment'} (not sealed)`
                : 'stored'
          }
        />
        <KeyValue label="Provider" value={config?.llm.provider ?? ''} mono />
        <KeyValue label="Model" value={config?.llm.model ?? ''} mono />
        <KeyValue label="Base URL" value={config?.llm.base_url ?? ''} mono />
        <KeyValue label="Updated" value={config?.llm.updated_at ?? ''} mono />
      </div>
      <div className="panel">
        <h2>Tenant state</h2>
        <KeyValue label="Tenant" value={config?.tenant.tenant_id ?? ''} mono />
        <KeyValue label="Default scope" value={config?.tenant.default_scope ?? ''} mono />
        <KeyValue label="Agents" value={(config?.tenant.agent_ids ?? []).join(', ')} mono />
        <div className="button-row">
          <button className="danger-button" type="button" onClick={() => void purge()} disabled={purging}>
            <Trash2 size={15} />
            {purging ? 'Purging' : 'Purge generated state'}
          </button>
        </div>
      </div>
      <div className="panel">
        <Snippet title="SDK consumer" text={config?.snippets.sdk ?? ''} />
      </div>
      <div className="panel">
        <Snippet title="MCP consumer" text={config?.snippets.mcp ?? ''} />
      </div>
    </section>
  );
}

function IntegrationScreen({ config }: { config: TenantConfig | null }) {
  const sdkCommand = `uv run examples/sdk_consumer.py --base-url ${config?.operator.platform_api_url ?? ''} --agent-id ${
    config?.tenant.example_agent_id ?? ''
  }`;
  const mcpCommand = config?.operator.mcp_url
    ? `uv run examples/mcp_consumer.py --mcp-url ${config.operator.mcp_url} --agent-id ${
        config?.tenant.example_agent_id ?? ''
      }`
    : '# No MCP endpoint is configured for this admin server. Relaunch with --mcp-url.';
  return (
    <section className="panel-grid two">
      <div className="panel">
        <h2>Platform endpoints</h2>
        <KeyValue label="Admin UI" value={config?.operator.ui_url} mono />
        <KeyValue label="Platform API" value={config?.operator.platform_api_url} mono />
        <KeyValue label="Contract" value={config?.operator.integration_contract_url} mono />
        <KeyValue label="MCP" value={config?.operator.mcp_url || 'not configured'} mono />
        <KeyValue label="Graph" value={config?.operator.graph_path} mono />
        <div className="integration-labels">
          {(config?.integration.labels ?? []).map((label) => (
            <span key={label}>{label}</span>
          ))}
        </div>
      </div>
      <div className="panel">
        <h2>Runnable commands</h2>
        <Snippet title="SDK command" text={sdkCommand} />
        <Snippet title="MCP command" text={mcpCommand} />
      </div>
      <div className="panel">
        <Snippet title="SDK snippet" text={config?.snippets.sdk ?? ''} />
      </div>
      <div className="panel">
        <Snippet title="MCP snippet" text={config?.snippets.mcp ?? ''} />
      </div>
      <div className="panel">
        <Snippet title="Codex MCP config" text={config?.snippets.codex_mcp_config ?? ''} />
      </div>
    </section>
  );
}

function Inspector({
  graph,
  selected,
  evidence,
  timeline,
  onOpenExplainability
}: {
  graph?: GraphView | null;
  selected: GraphNode | null;
  evidence: MemoryEvidence | null;
  timeline: TimelineEntry[];
  onOpenExplainability?: (node: GraphNode) => void;
}) {
  const selectedIsFact = isFactNode(selected);
  // The two real entities a relationship connects, resolved the same way the
  // graph canvas does (walk its reified subject/object edges -- see
  // `resolveFactEndpoints`), so the Inspector's chips match the canvas's.
  const endpoints =
    selected && selectedIsFact
      ? resolveFactEndpoints(selected.id, graph?.nodes ?? [], graph?.edges ?? [])
      : { subject: null, object: null };
  return (
    <aside className="panel inspector">
      <h2>Inspector</h2>
      {selected ? (
        <>
          {/* Neo4j shows the clicked element's labels, then every property as a
              key/value row. The label chips carry the same colour they have on
              the canvas, so the selection is traceable back to the graph. */}
          <div className="inspector-labels">
            {nodeLabels(selected).map((label) => (
              <span key={label} className="label-chip" style={{ background: labelColor(label) }}>
                {label}
              </span>
            ))}
          </div>
          {selectedIsFact ? (
            <FactTriple
              triple={buildTriple(selected.properties)}
              subjectLabels={endpointLabels(endpoints.subject)}
              objectLabels={endpointLabels(endpoints.object)}
            />
          ) : (
            <KeyValue label="Name" value={nodeCaption(selected, 0)} />
          )}
          <KeyValue label="Status" value={selected.status ?? ''} />
          <KeyValue label="Confidence" value={formatNumber(selected.confidence ?? 0, 2)} />
          <KeyValue label="Observed" value={String(selected.observed_count ?? 0)} />
          <KeyValue label="Relationship" value={selected.relationship_uuid ?? selected.graph_uuid ?? selected.id} mono />
          <NodeProperties node={selected} />
          {onOpenExplainability ? (
            <button
              className="primary-button full-width"
              type="button"
              disabled={!isFactNode(selected)}
              onClick={() => onOpenExplainability(selected)}
            >
              <GitBranch size={15} />
              Open in Explainability
            </button>
          ) : null}
          {evidence ? (
            <>
              <h3>Evidence</h3>
              <KeyValue label="Episodes" value={String(evidence.episode_uuids.length)} />
              <KeyValue label="Created by" value={evidence.created_by} mono />
            </>
          ) : null}
          {timeline.length ? (
            <>
              <h3>Timeline</h3>
              {timeline.map((entry) => (
                <div className="list-row compact" key={entry.relationship_uuid}>
                  <strong>{entry.status}</strong>
                  {/* No entity-type labels on a timeline entry (the endpoint
                      is a name string, not a resolvable node) -- the triple
                      degrades to plain, uncoloured names rather than
                      fabricating a type. */}
                  <FactTriple triple={buildTriple(entry)} />
                </div>
              ))}
            </>
          ) : null}
        </>
      ) : (
        <div className="empty-state">No fact selected.</div>
      )}
    </aside>
  );
}

function Metric({
  label,
  value,
  hint,
  unmeasured = false,
  wide = false
}: {
  label: string;
  value: string;
  hint?: string;
  unmeasured?: boolean;
  /** Compound values ("842 / 13,311") need a step down to stay on one line. */
  wide?: boolean;
}) {
  const valueClass = [unmeasured ? 'unmeasured' : '', wide ? 'wide' : ''].filter(Boolean).join(' ');
  return (
    <div className="metric">
      <span>{label}</span>
      <strong className={valueClass}>{value}</strong>
      {hint ? <em className="metric-hint">{hint}</em> : null}
    </div>
  );
}

/**
 * Every property of the selected element as a key/value row.
 *
 * Neo4j collapses a large property set behind a disclosure rather than letting
 * it push the rest of the panel away -- these nodes carry ~40 keys, so it does
 * the same. Embedding vectors are filtered out entirely (see
 * `isInspectableProperty`): a 256-float array is a model artefact, not a
 * property anyone inspects.
 */
const INSPECTOR_PROPERTY_PREVIEW = 8;

function formatPropertyValue(value: unknown): string {
  if (value === null || value === undefined) return 'null';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return value.length ? value.map((item) => formatPropertyValue(item)).join(', ') : '[]';
  return JSON.stringify(value);
}

function NodeProperties({ node }: { node: GraphNode }) {
  const [expanded, setExpanded] = useState(false);
  const entries = useMemo(
    () =>
      Object.entries(node.properties ?? {})
        .filter(([key, value]) => isInspectableProperty(key, value))
        .sort(([a], [b]) => a.localeCompare(b)),
    [node]
  );
  // Collapse state belongs to the selection, not to whatever was open before.
  useEffect(() => setExpanded(false), [node.id]);
  if (!entries.length) return null;
  const shown = expanded ? entries : entries.slice(0, INSPECTOR_PROPERTY_PREVIEW);
  return (
    <>
      <h3>Properties</h3>
      <table className="data-table compact-table property-table">
        <tbody>
          {shown.map(([key, value]) => (
            <tr key={key}>
              <th>{key}</th>
              <td>{formatPropertyValue(value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {entries.length > INSPECTOR_PROPERTY_PREVIEW ? (
        <button type="button" className="property-toggle" onClick={() => setExpanded((current) => !current)}>
          {expanded ? 'Show fewer' : `Show all ${entries.length} properties`}
        </button>
      ) : null}
    </>
  );
}

function KeyValue({ label, value, mono = false }: { label: string; value?: string; mono?: boolean }) {
  return (
    <div className="kv">
      <span>{label}</span>
      <strong className={mono ? 'mono' : ''}>{value || 'none'}</strong>
    </div>
  );
}

function KeyValueTable({ values }: { values: Record<string, string> }) {
  const entries = Object.entries(values);
  if (!entries.length) return <div className="empty-state">No source trace loaded.</div>;
  return (
    <table className="data-table compact-table">
      <tbody>
        {entries.map(([key, value]) => (
          <tr key={key}>
            <th>{key}</th>
            <td>{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Snippet({ title, text }: { title: string; text: string }) {
  async function copy() {
    if (!text) return;
    await navigator.clipboard?.writeText(text);
  }
  return (
    <div className="snippet">
      <div className="snippet-head">
        <h3>{title}</h3>
        <button className="icon-button" type="button" onClick={() => void copy()} title={`Copy ${title}`}>
          <Clipboard size={15} />
          <span className="sr-only">Copy {title}</span>
        </button>
      </div>
      <pre>{text}</pre>
    </div>
  );
}

function lineageEdges(graph: GraphView | null, selected: GraphNode | null): GraphEdge[] {
  if (!graph || !selected) return [];
  return graph.edges.filter(
    (edge) =>
      edge.relationship_uuid === selected.relationship_uuid &&
      (edge.edge_type === 'superseded_by' || edge.edge_type === 'derived_from')
  );
}
