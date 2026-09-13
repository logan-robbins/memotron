# `examples/caveman_demo.py` — captured output of one real run

**Run date:** 2026-09-11 (UTC)
**Chat model:** `claude-sonnet-4-6` (undated alias, via the JedAI Gateway)
**Embedding model:** `text-embedding-3` (3072 dimensions, selected explicitly)
**Commit:** `184280bd8797bf9287be5ea11fbf016a34c98c97`
**Command:** `uv run examples/caveman_demo.py` — exit 0

## This is evidence, not a golden

No test diffs this file, and none should. A live model's wording is not reproducible: run the demo
again and the claims will be phrased differently, the node names may differ, and the number of
dream calls will follow however many nodes reconcile created. What is reproducible is the
*contract* — the budgets, the rendering, the call counts per stage, and the **twenty-one
assertions** at the bottom — and those are asserted in code inside the demo, which exits 1 rather
than 0 if any of them fails.

The header above is what makes the file auditable rather than decorative. Without the commit sha
there is no way to tell which prompts produced this transcript, and the prompts are the thing the
demo exists to test.

## What this run shows

- **19 live gateway calls**, and **zero** for every read, walk, deep read and the replay proof.
  Retrieval and the proof are both deterministic by design. Some call timings below are under a
  second, which is the gateway serving a byte-identical prompt from cache -- the prompts this
  subsystem sends are a deterministic function of the stores, so a re-run of the same episode sends
  the same bytes.

```text
  EXTRACT       2 call(s),   0.73s total
  RECONCILE     2 call(s),   5.99s total
  DREAM-NODE   11 call(s),  45.03s total
  DREAM-GLOBAL  4 call(s),  26.60s total
  READ          0 call(s) — deterministic by design
```

- **Two episodes into one scope.** 12 claims from the first, and the second is a recap a
  day later: **6 records gained evidence rather than being duplicated**, and
  **10 facts and edges** now carry more than one ledger entry, rendered as `(x2)` and weighted
  higher by ranking. That is the whole difference between evidence and a growing log.
- **A value replaced across episodes.** The chat default moved to `claude-sonnet-4-6`. The node
  shows the new value; the old one is reachable only through `memory_explain`, dated, with its
  entry id. The ledger can only link a supersession *within* one extract response, so the witness
  here is the dreamer's own answer — see the design note on that deviation.
- **Typed edges, and the ids carried back.** Every block header ends in `[n-…]`, and the run walks
  it: `node`, `neighbors`, one hop further to each neighbour, then `explain`. All 15 renders
  and all 19 prompts were checked for the four characters amendment D removed from the
  format; none of them appears.
- **Both ceilings bite.** `M` set one below the scope's own vocabulary forced exactly
  1 `rename_edge_type`, with the doomed type decided by arithmetic and what it folds into
  decided by the model. `N=3` then forced the merges, stepped down pass by pass because a forced
  slate is `pressure` *disjoint* pairs.
- **The compression is proved.** 34 journalled `DreamEvent`s replayed onto an empty graph
  rebuild this scope to the same content digest `1e8a0616df85286f…` — no model call, no embedding. Every
  event is printed at the bottom with its op, its nodes and a one-line summary.

## The whole transcript, verbatim

Every rendered block below is the exact text an agent receives. Nothing is summarised.

```text

====================================================================================================
1. THE EPISODE, THE MOTIVE, THE BUDGETS
====================================================================================================
  episode  : ep-251-01   scope=repo:jedai/memotron   occurred_at=2026-09-08T16:40:00Z
  chat     : caveman-chat:claude-sonnet-4-6
  embedding: openai-embeddings:text-embedding-3@v1   (selected explicitly; no transport applies a default)

  TURNS — the exact text stage 1 reads:
    1. Logan: One rule before we touch anything: every real run goes through the JedAI Gateway. No hermetic mode, no local stub, not even for a quick check. If the platform is broken we fix the platform — we don't downgrade the run.
    2. Priya: Agreed. And for whoever reads this later: the JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models. It isn't a model itself, it's the thing that routes to them.
    3. agent: Confirmed against the deploy history. Before #245 the gateway refused our Host header outright, so the agent-memory MCP server couldn't reach it at all. #245 fixed the Host rewrite.
    4. Priya: Right, and the same PR fixed the other half — the chart was deploying agent-memory from the wrong image tag. The chart is where the agent-memory MCP deploy lives in the first place; that came in with #240.
    5. Logan: There's still something flaky underneath all that. Roughly one call in twenty loses session affinity and lands on a cold pod. I've never got it to reproduce on demand.
    6. agent: That's the failure the probe in #246 names. It asserts the intermittent affinity loss rather than pretending it's fixed, which is why the probe is committed.
    7. Priya: On capability — C4 is returning 24 tools right now, so I'd call the surface basically complete.
    8. agent: Two more worth recording. The chat default on the gateway is claude-haiku-4-5. The embedding alias is text-embedding-3 at 3072 dimensions, and it has to be selected explicitly — there's no default embedding model applied for you.
    9. Logan: Correction on Priya's number. I counted again after #248 landed: the C4 memory server returns 31 tools, not 24. 24 was the count before #248.
    10. Priya: Last one, and treat it as a rule: we only ever name undated aliases on the LiteLLM proxy — claude-haiku-4-5, never a dated pin. A dated alias goes stale and the gateway starts refusing it.

  motive   : engineering   digest=1616634672d7d1d2
  goal     : Know how this system actually behaves, so a later session does not re-derive it: the rules, the definitions, the measured facts with their identifiers, and what is still unreliable.

  BUDGETS
    N (max nodes per scope)   = 500
    L (max facts per node)    = 8
    T (paragraph guard/line)  = 40 tokens  (about 160 characters; the prompt states brevity, not a count)
    header                    = 16 tokens (name|type|L:key|as of|N entries)
    read budget               = 100 lines / 1500 tokens, k=8 seeds
    scope_budget(N, L, T)     = 168,000 tokens
    superseded facts          = keep=True, ttl=90 days

====================================================================================================
2. EXTRACT — one live call. The episode becomes claims in the ledger.
====================================================================================================
  LIVE CALLS: 2
    EXTRACT        0.36s  prompt  7,534 chars -> response 3,923 chars
    RECONCILE      0.37s  prompt  9,115 chars -> response 4,316 chars

  12 claim(s) extracted under motive 'engineering':

    e-fc9b2270fe5a47ec9e9297143a82e12f  rule           mode=directive    conf=0.99  turns=[1]
      claim      : Every real run goes through the JedAI Gateway; no hermetic mode, no local stub, not even for a quick check.
      subjects   : ['JedAI Gateway']

    e-ca478da7f99146d9bb88abd46a1cb388  is             mode=report       conf=0.99  turns=[2]
      claim      : JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself.
      subjects   : ['JedAI Gateway']
      objects    : ['JedAI models']

    e-9590178378db4b689615facabd2118a8  attribute      mode=report       conf=0.97  turns=[3]
      claim      : Before #245 the gateway refused the Host header outright, so the agent-memory MCP server could not reach it; #245 fixed the Host rewrite.
      subjects   : ['JedAI Gateway', 'agent-memory MCP server']
      identifiers: ['#245']

    e-1b9152680510459f8b49f69f7310b2ab  attribute      mode=report       conf=0.97  turns=[4]
      claim      : #245 also fixed the chart deploying agent-memory from the wrong image tag.
      subjects   : ['agent-memory']
      identifiers: ['#245']

    e-cdb046a300714229a19c3732313a9ce5  attribute      mode=report       conf=0.97  turns=[4]
      claim      : The chart is where the agent-memory MCP deploy lives; that came in with #240.
      subjects   : ['agent-memory MCP']
      objects    : ['chart']
      identifiers: ['#240']

    e-8695547b60be4a428f7fcda5e437405c  unsure         mode=report       conf=0.90  turns=[5]
      claim      : Roughly one call in twenty loses session affinity and lands on a cold pod; never reproduced on demand.
      subjects   : ['session affinity']

    e-e91daa0551b44aa988580c356131715d  attribute      mode=report       conf=0.97  turns=[6]
      claim      : The intermittent affinity loss is named and asserted (not claimed fixed) by the probe in #246.
      subjects   : ['session affinity']
      identifiers: ['#246']

    e-b74cfc963f39456b8167f5a6316fd605  attribute      mode=report       conf=0.85  turns=[7]
      claim      : C4 is returning 24 tools.
      subjects   : ['C4']
      identifiers: ['24']

    e-86cc728cfda342fda1385e2d60a4d3ff  attribute      mode=report       conf=0.99  turns=[8]
      claim      : Chat default on the gateway is claude-haiku-4-5.
      subjects   : ['JedAI Gateway']
      identifiers: ['claude-haiku-4-5']

    e-6300eb1412aa47acb7362a17d130c1f3  attribute      mode=report       conf=0.99  turns=[8]
      claim      : Embedding alias is text-embedding-3 at 3072 dimensions; must be selected explicitly, no default embedding model is applied automatically.
      subjects   : ['text-embedding-3']
      identifiers: ['text-embedding-3', '3072']

    e-5c26ce857234420da3b55e04bdd73a73  attribute      mode=correction   conf=0.97  turns=[9]
      claim      : C4 memory server returns 31 tools after #248, not 24; 24 was the count before #248.
      subjects   : ['C4']
      identifiers: ['31', '#248', '24']
      supersedes : e-b74cfc963f39456b8167f5a6316fd605   <-- the in-episode correction, resolved to its sibling

    e-0976177bcc1d4505a3209847885a4a15  rule           mode=directive    conf=0.99  turns=[10]
      claim      : Rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-haiku-4-5, never a dated pin); a dated alias goes stale and the gateway starts refusing it.
      subjects   : ['LiteLLM proxy']
      identifiers: ['claude-haiku-4-5']

  supersession within this one episode: 1 entry/entries
    e-5c26ce857234420da3b55e04bdd73a73 supersedes e-b74cfc963f39456b8167f5a6316fd605
      new: C4 memory server returns 31 tools after #248, not 24; 24 was the count before #248.
      old: C4 is returning 24 tools.   <-- stays in the ledger, leaves the node

====================================================================================================
3. RECONCILE — one live call for the WHOLE batch. Names become node ids.
====================================================================================================
  LIVE CALLS: 1
    RECONCILE      0.37s  prompt  9,115 chars -> response 4,316 chars

  CANDIDATES OFFERED per surface name (top-k kNN, rendered to the model as id | name | aliases | type | gloss — never a node's lines). The aliases column is
  what lets the router recognise a name it has routed before, on sight:
    'JedAI Gateway': (none — the scope was empty, so nothing could be offered)
    'JedAI models': (none — the scope was empty, so nothing could be offered)
    'agent-memory MCP server': (none — the scope was empty, so nothing could be offered)
    'agent-memory': (none — the scope was empty, so nothing could be offered)
    'agent-memory MCP': (none — the scope was empty, so nothing could be offered)
    'chart': (none — the scope was empty, so nothing could be offered)
    'session affinity': (none — the scope was empty, so nothing could be offered)
    'C4': (none — the scope was empty, so nothing could be offered)
    'text-embedding-3': (none — the scope was empty, so nothing could be offered)
    'LiteLLM proxy': (none — the scope was empty, so nothing could be offered)

  ADJUDICATIONS — 10 name(s):
    'JedAI Gateway'  ->  NEW  n-001  {name='JedAI Gateway', type='service'}
        gloss : LiteLLM proxy fronting the JedAI models; routes requests to them, is not a model itself
        reason: no candidates offered; this is the central proxy service
    'JedAI models'  ->  NEW  n-002  {name='JedAI models', type='service'}
        gloss : the underlying AI models that the JedAI Gateway routes requests to
        reason: no candidates offered; distinct from the gateway that fronts them
    'agent-memory MCP server'  ->  NEW  n-003  {name='agent-memory MCP server', type='service'}
        gloss : the agent-memory MCP server deployed via chart; was blocked from reaching the gateway before #245
        reason: no candidates offered; same component as agent-memory and agent-memory MCP, all three describe the same deployed MCP service
    'agent-memory'  ->  NEW  n-003  {name='agent-memory MCP server', type='service'}
        gloss : the agent-memory MCP server deployed via chart; was blocked from reaching the gateway before #245
        reason: same service as agent-memory MCP server and agent-memory MCP; #245 fixed its wrong image tag
    'agent-memory MCP'  ->  NEW  n-003  {name='agent-memory MCP server', type='service'}
        gloss : the agent-memory MCP server deployed via chart; was blocked from reaching the gateway before #245
        reason: same service as agent-memory and agent-memory MCP server; deploy lives in the chart, introduced in #240
    'chart'  ->  NEW  n-004  {name='chart', type='artifact'}
        gloss : the Helm chart that hosts the agent-memory MCP deployment; introduced in #240
        reason: no candidates offered; a separate artifact from the service it deploys
    'session affinity'  ->  NEW  n-005  {name='session affinity', type='attribute'}
        gloss : pod-level session stickiness; intermittently lost (~1 in 20 calls), landing requests on cold pods; noted in #246
        reason: no candidates offered; a distinct operational property with its own failure mode
    'C4'  ->  NEW  n-006  {name='C4', type='service'}
        gloss : memory server component; returned 24 tools before #248, 31 tools after #248
        reason: no candidates offered; a named memory server with a tracked tool count
    'text-embedding-3'  ->  NEW  n-007  {name='text-embedding-3', type='model'}
        gloss : embedding model alias at 3072 dimensions; must be selected explicitly, no default is applied automatically
        reason: no candidates offered; a specific embedding model alias with a fixed dimension
    'LiteLLM proxy'  ->  NEW  n-001  {name='JedAI Gateway', type='service'}
        gloss : LiteLLM proxy fronting the JedAI models; routes requests to them, is not a model itself
        reason: same thing as JedAI Gateway; the gateway IS the LiteLLM proxy, second surface name for the same node

  CONVERGENCE — surface names sharing one node id:
    n-001 (JedAI Gateway): ['JedAI Gateway', 'LiteLLM proxy']  <-- two surface names, one concept
    n-002 (JedAI models): ['JedAI models']
    n-003 (agent-memory MCP server): ['agent-memory MCP server', 'agent-memory', 'agent-memory MCP']  <-- two surface names, one concept
    n-004 (chart): ['chart']
    n-005 (session affinity): ['session affinity']
    n-006 (C4): ['C4']
    n-007 (text-embedding-3): ['text-embedding-3']

  ALIASES RECORDED — every surface name the router sent here, so a later search
  by the name I happen to know is an EXACT hit rather than a similarity guess:
    n-006 (C4): (none)
    n-001 (JedAI Gateway): ['LiteLLM proxy']
    n-002 (JedAI models): (none)
    n-003 (agent-memory MCP server): ['agent-memory', 'agent-memory MCP']
    n-004 (chart): (none)
    n-005 (session affinity): (none)
    n-007 (text-embedding-3): (none)

  created=['n-001', 'n-002', 'n-003', 'n-004', 'n-005', 'n-006', 'n-007']  bound=[]
  dirty=['n-001', 'n-002', 'n-003', 'n-004', 'n-005', 'n-006', 'n-007']  pressure=0

  THE MATCHER'S THIRD ANSWER -- a belief BETWEEN two concepts is a typed EDGE.
  A claim naming one concept binds and marks dirty, and the dreamer writes it as a
  fact. A claim naming two becomes a relation, with a TYPE from this scope's
  vocabulary or a new UPPER_SNAKE word the matcher coins, and the ledger claim
  carried verbatim as its first text. That is the three-way question this stage is
  actually asked: new concept, new belief about one, or a kind of belief the scope
  already has a word for, now applied to another pair.

  relations created   : 2 ['r-001', 'r-002']
  relations REINFORCED: 0 []
  edge types COINED   : ['FRONTS', 'DEPLOYS']   (the scope was empty, so every type here is a coinage)
  the scope's vocabulary now: {'DEPLOYS': 1, 'FRONTS': 1}
  M for this motive         : 30
  node ids to traverse      : ['n-001', 'n-002', 'n-003', 'n-004', 'n-005', 'n-006', 'n-007']
    r-001  [n-001] JedAI Gateway -FRONTS-> [n-002] JedAI models
         claim (the ledger claim, verbatim): JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself.
         evidence: 1 ['e-ca478da7f99146d9bb88abd46a1cb388']
    r-002  [n-004] chart -DEPLOYS-> [n-003] agent-memory MCP server
         claim (the ledger claim, verbatim): The chart is where the agent-memory MCP deploy lives; that came in with #240.
         evidence: 1 ['e-cdb046a300714229a19c3732313a9ce5']

  GRAPH AFTER RECONCILE (factless by design): 7 node(s)
    C4 (service) [n-006] as of 2026-09-11, 2 entries   dirty=True reads=0 facts=0
      (no facts yet -- reconcile created it, the dreamer writes its first fact set)
    JedAI Gateway (service) [n-001] as of 2026-09-11, 5 entries, aka LiteLLM proxy   dirty=True reads=0 facts=0
      (no facts yet -- reconcile created it, the dreamer writes its first fact set)
      FRONTS [n-002]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself.   [r-001]
    JedAI models (service) [n-002] as of 2026-09-11, 1 entries   dirty=True reads=0 facts=0
      (no facts yet -- reconcile created it, the dreamer writes its first fact set)
    agent-memory MCP server (service) [n-003] as of 2026-09-11, 3 entries, aka agent-memory, agent-memory MCP   dirty=True reads=0 facts=0
      (no facts yet -- reconcile created it, the dreamer writes its first fact set)
    chart (artifact) [n-004] as of 2026-09-11, 1 entries   dirty=True reads=0 facts=0
      (no facts yet -- reconcile created it, the dreamer writes its first fact set)
      DEPLOYS [n-003]: The chart is where the agent-memory MCP deploy lives; that came in with #240.   [r-002]
    session affinity (attribute) [n-005] as of 2026-09-11, 2 entries   dirty=True reads=0 facts=0
      (no facts yet -- reconcile created it, the dreamer writes its first fact set)
    text-embedding-3 (model) [n-007] as of 2026-09-11, 1 entries   dirty=True reads=0 facts=0
      (no facts yet -- reconcile created it, the dreamer writes its first fact set)
  RELATIONS: 2 edge(s) over 2 type(s), each carrying its own evidence
    r-001  n-001 (JedAI Gateway) -FRONTS-> n-002 (JedAI models)  evidence=1 [cooccurrence says 1]
    r-002  n-004 (chart) -DEPLOYS-> n-003 (agent-memory MCP server)  evidence=1 [cooccurrence says 1]
  EDGE TYPES: {'DEPLOYS': 1, 'FRONTS': 1}

====================================================================================================
4. DREAM INCREMENTAL -- one live call per dirty node. The dreamer writes the facts.
====================================================================================================
  LIVE CALLS: 7
    DREAM-NODE     3.20s  prompt  5,878 chars -> response   515 chars
    DREAM-NODE     5.65s  prompt  6,777 chars -> response 1,718 chars
    DREAM-NODE     2.98s  prompt  5,760 chars -> response   406 chars
    DREAM-NODE     4.62s  prompt  6,201 chars -> response   885 chars
    DREAM-NODE     4.42s  prompt  5,729 chars -> response   561 chars
    DREAM-NODE     3.22s  prompt  5,804 chars -> response   580 chars
    DREAM-NODE     3.70s  prompt  5,649 chars -> response   529 chars

  7 node(s) re-dreamed. The dreamer is the ONLY writer of fact text.

    NODE n-006 — C4|service  ->  C4|service
      entries it saw (2, via ledger.for_node(since=dreamed_at)):
        e-b74cfc963f39456b8167f5a6316fd605 attribute C4 is returning 24 tools.
        e-5c26ce857234420da3b55e04bdd73a73 attribute C4 memory server returns 31 tools after #248, not 24; 24 was the count before #248.
      facts BEFORE (0):
        (none)
      facts AFTER (1):
        returns 31 tools after #248; count was 24 before #248   [13 tokens]
      demoted (0) — left the node, still in the ledger:
        (none)
      dreamed_at=None  dirty=True

    NODE n-001 — JedAI Gateway|service  ->  JedAI Gateway|service
      entries it saw (5, via ledger.for_node(since=dreamed_at)):
        e-fc9b2270fe5a47ec9e9297143a82e12f rule Every real run goes through the JedAI Gateway; no hermetic mode, no local stub, not even for a quick check.
        e-ca478da7f99146d9bb88abd46a1cb388 is JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself.
        e-9590178378db4b689615facabd2118a8 attribute Before #245 the gateway refused the Host header outright, so the agent-memory MCP server could not reach it; #245 fixed the Host rewrite.
        e-86cc728cfda342fda1385e2d60a4d3ff attribute Chat default on the gateway is claude-haiku-4-5.
        e-0976177bcc1d4505a3209847885a4a15 rule Rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-haiku-4-5, never a dated pin); a dated alias goes stale and the gateway starts refusing it.
      facts BEFORE (0):
        (none)
      facts AFTER (5):
        real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check   [26 tokens]
        only ever name undated aliases on the LiteLLM proxy (e.g. claude-haiku-4-5, never a dated pin); dated alias goes stale and gateway starts refusing it   [37 tokens]
        LiteLLM proxy fronting the JedAI models, not a model itself   [15 tokens]
        chat default is claude-haiku-4-5   [8 tokens]
        before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it   [30 tokens]
      demoted (0) — left the node, still in the ledger:
        (none)
      dreamed_at=None  dirty=True

    NODE n-002 — JedAI models|service  ->  JedAI models|service
      entries it saw (1, via ledger.for_node(since=dreamed_at)):
        e-ca478da7f99146d9bb88abd46a1cb388 is JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself.
      facts BEFORE (0):
        (none)
      facts AFTER (1):
        collection of LiteLLM-routed models sitting behind JedAI Gateway   [16 tokens]
      demoted (0) — left the node, still in the ledger:
        (none)
      dreamed_at=None  dirty=True

    NODE n-003 — agent-memory MCP server|service  ->  agent-memory MCP server|service
      entries it saw (3, via ledger.for_node(since=dreamed_at)):
        e-9590178378db4b689615facabd2118a8 attribute Before #245 the gateway refused the Host header outright, so the agent-memory MCP server could not reach it; #245 fixed the Host rewrite.
        e-1b9152680510459f8b49f69f7310b2ab attribute #245 also fixed the chart deploying agent-memory from the wrong image tag.
        e-cdb046a300714229a19c3732313a9ce5 attribute The chart is where the agent-memory MCP deploy lives; that came in with #240.
      facts BEFORE (0):
        (none)
      facts AFTER (3):
        MCP server providing agent memory, deployed via chart introduced in #240   [18 tokens]
        before #245, could not reach JedAI Gateway because gateway refused Host header; #245 fixed the Host rewrite   [27 tokens]
        #245 also fixed chart deploying agent-memory from wrong image tag   [16 tokens]
      demoted (0) — left the node, still in the ledger:
        (none)
      dreamed_at=None  dirty=True

    NODE n-004 — chart|artifact  ->  chart|artifact
      entries it saw (1, via ledger.for_node(since=dreamed_at)):
        e-cdb046a300714229a19c3732313a9ce5 attribute The chart is where the agent-memory MCP deploy lives; that came in with #240.
      facts BEFORE (0):
        (none)
      facts AFTER (1):
        agent-memory MCP deploy lives in this chart; introduced in #240   [16 tokens]
      demoted (0) — left the node, still in the ledger:
        (none)
      dreamed_at=None  dirty=True

    NODE n-005 — session affinity|attribute  ->  session affinity|attribute
      entries it saw (2, via ledger.for_node(since=dreamed_at)):
        e-8695547b60be4a428f7fcda5e437405c unsure Roughly one call in twenty loses session affinity and lands on a cold pod; never reproduced on demand.
        e-e91daa0551b44aa988580c356131715d attribute The intermittent affinity loss is named and asserted (not claimed fixed) by the probe in #246.
      facts BEFORE (0):
        (none)
      facts AFTER (2):
        intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246   [21 tokens]
        roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand   [24 tokens]
      demoted (0) — left the node, still in the ledger:
        (none)
      dreamed_at=None  dirty=True

    NODE n-007 — text-embedding-3|model  ->  text-embedding-3|model
      entries it saw (1, via ledger.for_node(since=dreamed_at)):
        e-6300eb1412aa47acb7362a17d130c1f3 attribute Embedding alias is text-embedding-3 at 3072 dimensions; must be selected explicitly, no default embedding model is applied automatically.
      facts BEFORE (0):
        (none)
      facts AFTER (2):
        must be selected explicitly; no default embedding model is applied automatically   [20 tokens]
        embedding alias text-embedding-3 at 3072 dimensions   [13 tokens]
      demoted (0) — left the node, still in the ledger:
        (none)
      dreamed_at=None  dirty=True

  GRAPH AFTER THE INCREMENTAL DREAM: 7 node(s)
    C4 (service) [n-006] as of 2026-09-11, 2 entries   dirty=False reads=0 facts=1
      attribute: returns 31 tools after #248; count was 24 before #248 (x2)
    JedAI Gateway (service) [n-001] as of 2026-09-11, 5 entries, aka LiteLLM proxy   dirty=False reads=0 facts=5
      rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check
      rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-haiku-4-5, never a dated pin); dated alias goes stale and gateway starts refusing it
      is: LiteLLM proxy fronting the JedAI models, not a model itself
      attribute: chat default is claude-haiku-4-5
      attribute: before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it
      FRONTS [n-002]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself.   [r-001]
    JedAI models (service) [n-002] as of 2026-09-11, 1 entries   dirty=False reads=0 facts=1
      is: collection of LiteLLM-routed models sitting behind JedAI Gateway
    agent-memory MCP server (service) [n-003] as of 2026-09-11, 3 entries, aka agent-memory, agent-memory MCP   dirty=False reads=0 facts=3
      is: MCP server providing agent memory, deployed via chart introduced in #240
      attribute: before #245, could not reach JedAI Gateway because gateway refused Host header; #245 fixed the Host rewrite
      attribute: #245 also fixed chart deploying agent-memory from wrong image tag
    chart (artifact) [n-004] as of 2026-09-11, 1 entries   dirty=False reads=0 facts=1
      attribute: agent-memory MCP deploy lives in this chart; introduced in #240
      DEPLOYS [n-003]: chart is where the agent-memory MCP deploy lives; came in with #240   [r-002]
    session affinity (attribute) [n-005] as of 2026-09-11, 2 entries   dirty=False reads=0 facts=2
      attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
      unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand
    text-embedding-3 (model) [n-007] as of 2026-09-11, 1 entries   dirty=False reads=0 facts=2
      rule: must be selected explicitly; no default embedding model is applied automatically
      attribute: embedding alias text-embedding-3 at 3072 dimensions
  RELATIONS: 2 edge(s) over 2 type(s), each carrying its own evidence
    r-001  n-001 (JedAI Gateway) -FRONTS-> n-002 (JedAI models)  evidence=1 [cooccurrence says 1]
    r-002  n-004 (chart) -DEPLOYS-> n-003 (agent-memory MCP server)  evidence=1 [cooccurrence says 1]
  EDGE TYPES: {'DEPLOYS': 1, 'FRONTS': 1}

====================================================================================================
5. DREAM GLOBAL, FREE — one live call at N=500. No pressure, so nothing is forced.
====================================================================================================
  LIVE CALLS: 1
    DREAM-GLOBAL   3.08s  prompt 10,437 chars -> response    11 chars

  count 7 of N=500   pressure = max(0, 7 - 500) = 0
  forced-merge slate: empty — a free pass
  ops applied       : 0
  count after       : 7

  OPS, read back from the receipt stream (the audit path, not a return value):
    (none — four-ish nodes under a budget of five hundred need no reorganising,
     and zero ops is a valid answer rather than a failure to answer)

  GRAPH AFTER THE FREE GLOBAL PASS: 7 node(s)
    C4 (service) [n-006] as of 2026-09-11, 2 entries   dirty=False reads=0 facts=1
      attribute: returns 31 tools after #248; count was 24 before #248 (x2)
    JedAI Gateway (service) [n-001] as of 2026-09-11, 5 entries, aka LiteLLM proxy   dirty=False reads=0 facts=5
      rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check
      rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-haiku-4-5, never a dated pin); dated alias goes stale and gateway starts refusing it
      is: LiteLLM proxy fronting the JedAI models, not a model itself
      attribute: chat default is claude-haiku-4-5
      attribute: before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it
      FRONTS [n-002]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself.   [r-001]
    JedAI models (service) [n-002] as of 2026-09-11, 1 entries   dirty=False reads=0 facts=1
      is: collection of LiteLLM-routed models sitting behind JedAI Gateway
    agent-memory MCP server (service) [n-003] as of 2026-09-11, 3 entries, aka agent-memory, agent-memory MCP   dirty=False reads=0 facts=3
      is: MCP server providing agent memory, deployed via chart introduced in #240
      attribute: before #245, could not reach JedAI Gateway because gateway refused Host header; #245 fixed the Host rewrite
      attribute: #245 also fixed chart deploying agent-memory from wrong image tag
    chart (artifact) [n-004] as of 2026-09-11, 1 entries   dirty=False reads=0 facts=1
      attribute: agent-memory MCP deploy lives in this chart; introduced in #240
      DEPLOYS [n-003]: chart is where the agent-memory MCP deploy lives; came in with #240   [r-002]
    session affinity (attribute) [n-005] as of 2026-09-11, 2 entries   dirty=False reads=0 facts=2
      attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
      unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand
    text-embedding-3 (model) [n-007] as of 2026-09-11, 1 entries   dirty=False reads=0 facts=2
      rule: must be selected explicitly; no default embedding model is applied automatically
      attribute: embedding alias text-embedding-3 at 3072 dimensions
  RELATIONS: 2 edge(s) over 2 type(s), each carrying its own evidence
    r-001  n-001 (JedAI Gateway) -FRONTS-> n-002 (JedAI models)  evidence=1 [cooccurrence says 1]
    r-002  n-004 (chart) -DEPLOYS-> n-003 (agent-memory MCP server)  evidence=1 [cooccurrence says 1]
  EDGE TYPES: {'DEPLOYS': 1, 'FRONTS': 1}

  RELATIONS AFTER THE PASS: 2 edge(s), none dangling.
  A merge re-points the edges of the nodes it absorbs and drops the self-loop
  that makes -- so no third node is left pointing at a name that went away.
    r-002  DEPLOYS [n-003]: chart is where the agent-memory MCP deploy lives; came in with #240
  GRAPH AFTER THE FREE PASS: 7 node(s)
    C4 (service) [n-006] as of 2026-09-11, 2 entries   dirty=False reads=0 facts=1
      attribute: returns 31 tools after #248; count was 24 before #248 (x2)
    JedAI Gateway (service) [n-001] as of 2026-09-11, 5 entries, aka LiteLLM proxy   dirty=False reads=0 facts=5
      rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check
      rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-haiku-4-5, never a dated pin); dated alias goes stale and gateway starts refusing it
      is: LiteLLM proxy fronting the JedAI models, not a model itself
      attribute: chat default is claude-haiku-4-5
      attribute: before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it
      FRONTS [n-002]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself.   [r-001]
    JedAI models (service) [n-002] as of 2026-09-11, 1 entries   dirty=False reads=0 facts=1
      is: collection of LiteLLM-routed models sitting behind JedAI Gateway
    agent-memory MCP server (service) [n-003] as of 2026-09-11, 3 entries, aka agent-memory, agent-memory MCP   dirty=False reads=0 facts=3
      is: MCP server providing agent memory, deployed via chart introduced in #240
      attribute: before #245, could not reach JedAI Gateway because gateway refused Host header; #245 fixed the Host rewrite
      attribute: #245 also fixed chart deploying agent-memory from wrong image tag
    chart (artifact) [n-004] as of 2026-09-11, 1 entries   dirty=False reads=0 facts=1
      attribute: agent-memory MCP deploy lives in this chart; introduced in #240
      DEPLOYS [n-003]: chart is where the agent-memory MCP deploy lives; came in with #240   [r-002]
    session affinity (attribute) [n-005] as of 2026-09-11, 2 entries   dirty=False reads=0 facts=2
      attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
      unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand
    text-embedding-3 (model) [n-007] as of 2026-09-11, 1 entries   dirty=False reads=0 facts=2
      rule: must be selected explicitly; no default embedding model is applied automatically
      attribute: embedding alias text-embedding-3 at 3072 dimensions
  RELATIONS: 2 edge(s) over 2 type(s), each carrying its own evidence
    r-001  n-001 (JedAI Gateway) -FRONTS-> n-002 (JedAI models)  evidence=1 [cooccurrence says 1]
    r-002  n-004 (chart) -DEPLOYS-> n-003 (agent-memory MCP server)  evidence=1 [cooccurrence says 1]
  EDGE TYPES: {'DEPLOYS': 1, 'FRONTS': 1}

====================================================================================================
5b. A SECOND EPISODE INTO THE SAME SCOPE -- what a restated belief does
====================================================================================================
  Everything above was one conversation. A memory that only ever grows is not a
  memory, so this is the case that separates the two: four turns, a day later,
  into the SAME scope, three of them restating something episode one already
  recorded and one of them replacing it.

  What each turn is here to do:
    1  restates the gateway's identity and the always-through-it rule  -> reinforce
    2  restates the agent-memory/gateway edge, still fixed by #245     -> reinforce
    3  moves the chat default to claude-sonnet-4-6               -> supersede
    4  restates the session-affinity flake nobody can reproduce       -> reinforce

  A restatement must NOT become a second fact or a second edge. It must land on
  the record that is already there: one more entry in its evidence, last_seen
  moved, and (xN) in the render. That is the whole difference between evidence
  and duplication.

  episode  : ep-251-02   scope=repo:jedai/memotron   occurred_at=2026-09-09T10:15:00Z

  TURNS -- the exact text stage 1 reads:
    1. Priya: Quick recap for the new folks: the JedAI Gateway is still the LiteLLM proxy in front of the JedAI models, and every real run goes through it. Nothing has changed there.
    2. agent: Confirmed again from today's deploy logs: the agent-memory MCP server reaches the gateway fine since #245 fixed the Host header rewrite.
    3. Logan: One update. The chat default on the gateway moved to claude-sonnet-4-6 this morning; claude-haiku-4-5 is no longer the default, though it still resolves.
    4. Priya: And the session-affinity flake hit again in the load test, about one in twenty calls, same as before. Still not reproducible on demand.

  LIVE CALLS: 2
    EXTRACT        0.37s  prompt  6,301 chars -> response 2,046 chars
    RECONCILE      5.62s  prompt  8,761 chars -> response 1,438 chars

  6 claim(s) extracted:

    e-bef62a6485e348be8a083a8fd6e9ada7  is             mode=report       conf=0.99  turns=[1]
      claim      : JedAI Gateway is the LiteLLM proxy in front of the JedAI models, not a model itself.
      subjects   : ['JedAI Gateway']
      objects    : ['JedAI models']

    e-d0b2ca5e9cfb4264a7b5caea7496cfc4  rule           mode=directive    conf=0.99  turns=[1]
      claim      : Every real run goes through the JedAI Gateway; no exceptions stated.
      subjects   : ['JedAI Gateway']

    e-c820b984fd6d4c67bbf774b625b32502  attribute      mode=report       conf=0.97  turns=[2]
      claim      : agent-memory MCP server reaches the gateway successfully since #245 fixed the Host header rewrite.
      subjects   : ['agent-memory MCP server', 'JedAI Gateway']
      identifiers: ['#245']

    e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687  attribute      mode=report       conf=0.95  turns=[3]
      claim      : chat default on the gateway was claude-haiku-4-5.
      subjects   : ['gateway']
      identifiers: ['claude-haiku-4-5']

    e-fe716a06788d4e80add2cf2afe6cfb5a  attribute      mode=correction   conf=0.97  turns=[3]
      claim      : chat default on the gateway moved to claude-sonnet-4-6 as of this morning; claude-haiku-4-5 no longer the default but still resolves.
      subjects   : ['gateway']
      identifiers: ['claude-sonnet-4-6', 'claude-haiku-4-5']
      supersedes : e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687   <-- the in-episode correction, resolved to its sibling

    e-a852e1bc1cf64e4daaa6f9d211ef41d2  unsure         mode=report       conf=0.93  turns=[4]
      claim      : session-affinity flake hits approximately one in twenty calls under load test; not reproducible on demand.
      subjects   : ['session-affinity']

  WHERE THEY ROUTED -- no new node is the CORRECT answer for a recap:
    'JedAI Gateway' -> [n-001] JedAI Gateway (bound to what episode one built)
    'JedAI models' -> [n-002] JedAI models (bound to what episode one built)
    'agent-memory MCP server' -> [n-003] agent-memory MCP server (bound to what episode one built)
    'gateway' -> [n-001] JedAI Gateway (bound to what episode one built)
    'session-affinity' -> [n-005] session affinity (bound to what episode one built)

  nodes created            : 0 []
  nodes bound              : 4 ['n-001', 'n-002', 'n-003', 'n-005']
  relations created        : 0 []
  relations REINFORCED     : 1 ['r-001']
  new edge types           : none -- the vocabulary held
  node ids to traverse     : ['n-001', 'n-002', 'n-003', 'n-005']
    r-001 FRONTS now carries evidence 2 ['e-ca478da7f99146d9bb88abd46a1cb388', 'e-bef62a6485e348be8a083a8fd6e9ada7'], claim unchanged: 'JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself.'
      ^ a reinforcement passes claim=None, so the dreamer's compressed text and its
        'until' marker survive a restatement instead of being overwritten by the
        raw ledger claim. Reconcile has no opinion about either.

  LIVE CALLS: 4
    DREAM-NODE     7.58s  prompt  7,673 chars -> response 2,219 chars
    DREAM-NODE     2.63s  prompt  5,949 chars -> response   361 chars
    DREAM-NODE     4.00s  prompt  6,254 chars -> response 1,122 chars
    DREAM-NODE     3.04s  prompt  5,960 chars -> response   605 chars

  4 node(s) re-dreamed over the two episodes' evidence.

  GRAPH AFTER THE SECOND EPISODE: 7 node(s)
    C4 (service) [n-006] as of 2026-09-11, 2 entries   dirty=False reads=0 facts=1
      attribute: returns 31 tools after #248; count was 24 before #248 (x2)
    JedAI Gateway (service) [n-001] as of 2026-09-11, 10 entries, aka LiteLLM proxy, gateway   dirty=False reads=0 facts=6
      rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
      rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
      is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)
      attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
      attribute: before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it (x2)
      superseded: chat default was claude-haiku-4-5 (x2)
      FRONTS [n-002]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)   [r-001]
    JedAI models (service) [n-002] as of 2026-09-11, 2 entries   dirty=False reads=0 facts=1
      is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)
    agent-memory MCP server (service) [n-003] as of 2026-09-11, 4 entries, aka agent-memory, agent-memory MCP   dirty=False reads=0 facts=4
      is: MCP server providing agent memory, deployed via chart introduced in #240
      attribute: before #245, could not reach JedAI Gateway because gateway refused Host header; #245 fixed the Host rewrite
      attribute: #245 also fixed chart deploying agent-memory from wrong image tag
      attribute: reaches JedAI Gateway successfully since #245 fixed the Host header rewrite (x2)
    chart (artifact) [n-004] as of 2026-09-11, 1 entries   dirty=False reads=0 facts=1
      attribute: agent-memory MCP deploy lives in this chart; introduced in #240
      DEPLOYS [n-003]: chart is where the agent-memory MCP deploy lives; came in with #240   [r-002]
    session affinity (attribute) [n-005] as of 2026-09-11, 3 entries, aka session-affinity   dirty=False reads=0 facts=2
      attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
      unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand (x2)
    text-embedding-3 (model) [n-007] as of 2026-09-11, 1 entries   dirty=False reads=0 facts=2
      rule: must be selected explicitly; no default embedding model is applied automatically
      attribute: embedding alias text-embedding-3 at 3072 dimensions
  RELATIONS: 2 edge(s) over 2 type(s), each carrying its own evidence
    r-001  n-001 (JedAI Gateway) -FRONTS-> n-002 (JedAI models)  evidence=2 [cooccurrence says 2]
    r-002  n-004 (chart) -DEPLOYS-> n-003 (agent-memory MCP server)  evidence=1 [cooccurrence says 1]
  EDGE TYPES: {'DEPLOYS': 1, 'FRONTS': 1}

  REINFORCEMENT -- every fact and edge whose evidence GREW rather than being copied:
    edge n-001 FRONTS n-002: evidence 1 -> 2
    n-001 fact LiteLLM proxy fronting the JedAI models, not a model itself: evidence 1 -> 2
    n-001 fact before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it: evidence 1 -> 2
    n-001 fact real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check: evidence 1 -> 2
    n-002 fact collection of LiteLLM-routed models sitting behind JedAI Gateway: evidence 1 -> 2
    n-005 fact roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand: evidence 1 -> 2

  facts and edges now supported by more than one entry: 10
    n-006 attribute: returns 31 tools after #248; count was 24 before #248 (x2)   ['e-5c26ce857234420da3b55e04bdd73a73', 'e-b74cfc963f39456b8167f5a6316fd605']
    n-001 rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)   ['e-fc9b2270fe5a47ec9e9297143a82e12f', 'e-d0b2ca5e9cfb4264a7b5caea7496cfc4']
    n-001 rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)   ['e-0976177bcc1d4505a3209847885a4a15', 'e-fe716a06788d4e80add2cf2afe6cfb5a']
    n-001 is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)   ['e-ca478da7f99146d9bb88abd46a1cb388', 'e-bef62a6485e348be8a083a8fd6e9ada7']
    n-001 attribute: before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it (x2)   ['e-9590178378db4b689615facabd2118a8', 'e-c820b984fd6d4c67bbf774b625b32502']
    n-001 superseded: chat default was claude-haiku-4-5 (x2)   ['e-86cc728cfda342fda1385e2d60a4d3ff', 'e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687']
    n-002 is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)   ['e-ca478da7f99146d9bb88abd46a1cb388', 'e-bef62a6485e348be8a083a8fd6e9ada7']
    n-003 attribute: reaches JedAI Gateway successfully since #245 fixed the Host header rewrite (x2)   ['e-9590178378db4b689615facabd2118a8', 'e-c820b984fd6d4c67bbf774b625b32502']
    n-005 unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand (x2)   ['e-8695547b60be4a428f7fcda5e437405c', 'e-a852e1bc1cf64e4daaa6f9d211ef41d2']
    r-001 FRONTS [n-002]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)   ['e-ca478da7f99146d9bb88abd46a1cb388', 'e-bef62a6485e348be8a083a8fd6e9ada7']

====================================================================================================
HOW AN AGENT SEARCHES THIS — the five calls a reader actually makes
====================================================================================================
  Against the 7-node graph the free global pass just left — NOT against the
  N=3 graph step 6 is about to force. A scope that fits inside one read gives a
  query nothing to decide, and what the query decides is this section's subject.

  Written from the reader's seat: what I would type, in the state I would be in.
  Every call here is deterministic and makes ZERO LLM calls. Each one prints its
  SEEDS — the mechanism that put each node in the read — because 'why did this
  come back' is a question a searcher has to be able to answer. The KEPT/DROPPED
  column is the kNN similarity floor at 0.25: a measured neighbour under it
  seeds nothing and expands nothing, and an exact hit is never floored at all. Each
  read also prints its duplicate count and its READ_EMITTED detail: two lines that
  state one fact are folded before the budget cut, and the receipt says how many.

  a) brief()                              no query at all — the session-start read
  b) read('#246')                         an identifier; #245 and #246 embed alike
  c) read('C4')                           a name I know, which may not be the kept name
  d) read('the gateway refuses my Host header')   a task, in my own words
  e) node(<node id>)                      re-read ONE concept, whole
  f) neighbors(<node id>)                 what it connects to, and by what belief
  g) explain(<node id>)                   the deep read the footer's first call names

  e, f and g are what the [n-...] id in every block header is FOR. A read answers a
  query; an id turns the answer into a graph an agent can walk, with no further
  model call and no re-query. That loop -- read, walk, read -- is why the ids are
  carried back in the rendering at all rather than being an internal detail.

====================================================================================================
a. brief() — the session-start / post-compaction read, no query
====================================================================================================
  The read I have when the context that knew what to ask for is gone. Every rule
  line in the scope first — rank.CONSTRAINT_FLOOR puts them there — then the
  highest-value nodes by pressure.node_value, the same function that decides
  which nodes survive a forced merge. No query, so no vector and no read_k.

  QUERY: (none — brief() takes no query)
  ------ rendered block, verbatim, exactly as an LLM reader receives it ------
  | JedAI Gateway (service) [n-001] as of 2026-09-11, 10 entries, aka LiteLLM proxy, gateway
  | rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
  | rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
  | is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)
  | attribute: before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it (x2)
  | attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
  | 
  | text-embedding-3 (model) [n-007] as of 2026-09-11, 1 entries
  | rule: must be selected explicitly; no default embedding model is applied automatically
  | attribute: embedding alias text-embedding-3 at 3072 dimensions
  | 
  | JedAI models (service) [n-002] as of 2026-09-11, 2 entries
  | is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)
  | from JedAI Gateway [n-001] FRONTS: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)
  | 
  | agent-memory MCP server (service) [n-003] as of 2026-09-11, 4 entries, aka agent-memory, agent-memory MCP
  | is: MCP server providing agent memory, deployed via chart introduced in #240
  | attribute: reaches JedAI Gateway successfully since #245 fixed the Host header rewrite (x2)
  | attribute: #245 also fixed chart deploying agent-memory from wrong image tag
  | attribute: before #245, could not reach JedAI Gateway because gateway refused Host header; #245 fixed the Host rewrite
  | from chart [n-004] DEPLOYS: chart is where the agent-memory MCP deploy lives; came in with #240
  | 
  | chart (artifact) [n-004] as of 2026-09-11, 1 entries
  | attribute: agent-memory MCP deploy lives in this chart; introduced in #240
  | 
  | C4 (service) [n-006] as of 2026-09-11, 2 entries
  | attribute: returns 31 tools after #248; count was 24 before #248 (x2)
  | 
  | session affinity (attribute) [n-005] as of 2026-09-11, 3 entries, aka session-affinity
  | attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
  | unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand (x2)
  | 
  | more: explain(n-001, n-007, n-002, n-003, n-004, n-006, n-005), neighbors(n-001, n-007, n-002, n-003, n-004, n-006, n-005)
  ---------------------------------------------------------------------------
  SEEDS: none — a brief() has no query, so nothing was seeded by one.
  nodes emitted   : 7  (JedAI Gateway, text-embedding-3, JedAI models, agent-memory MCP server, chart, C4, session affinity)
  lines           : 18 of a 100-line budget
  tokens          : 603 (block incl. headers) against a ceiling of 1612
  duplicates      : 2 fact(s) folded away   <-- rank.dedupe_lines: one fact stated twice, kept once, before the budget cut
  saturated       : False
  vectors bought  : 0
  contract_digest : c8d76f4718e26fd2432920c50e6f13260d36232afed308670811b503672428ed
  receipt detail  : 18 of 18 ranked facts from 7 node(s); dropped 2 duplicate fact(s); budget 100 lines / 1500 tokens; brief over 7 node(s) by value, no query
  LIVE CALLS: none — this step makes no LLM call at all.

====================================================================================================
b — read('#246')
====================================================================================================
  An IDENTIFIER. The ledger indexes every identifier its entries carry, so this is an
  exact hit on the nodes whose evidence names #246 — not a near-neighbour of it.

  QUERY: #246
  ------ rendered block, verbatim, exactly as an LLM reader receives it ------
  | session affinity (attribute) [n-005] as of 2026-09-11, 3 entries, aka session-affinity
  | attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
  | unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand (x2)
  | 
  | C4 (service) [n-006] as of 2026-09-11, 2 entries
  | attribute: returns 31 tools after #248; count was 24 before #248 (x2)
  | 
  | more: explain(n-005, n-006), neighbors(n-005, n-006)
  ---------------------------------------------------------------------------
  SEEDS (7 candidate(s) of k=8; 1 exact, 5 dropped by the kNN floor 0.25) — why each node was in the read, or was not:
    KEPT     n-005  identifier  sim=1.0000  on '#246'                   EXACT — the ledger's identifier index
    KEPT     n-006  knn         sim=0.3634  on (whole query)            measured — embedding neighbour of the whole query
    DROPPED  n-003  knn         sim=0.2218  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-001  knn         sim=0.2111  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-004  knn         sim=0.1981  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-007  knn         sim=0.1940  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-002  knn         sim=0.1348  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    ^ 5 candidate(s) below 0.25 seeded nothing and expanded nothing: an exact hit is never floored, a measured one is.
  nodes emitted   : 2  (session affinity, C4)
  lines           : 3 of a 100-line budget
  tokens          : 117 (block incl. headers) against a ceiling of 1532
  duplicates      : 0 fact(s) folded away   (nothing in this read stated one fact twice)
  saturated       : False
  vectors bought  : 1
  contract_digest : 9fd12dd9f8c2bbcd2a1c1ff7d3d018e0f48dd567ca35ba3ccd13e1e5656c1480
  receipt detail  : 3 of 3 ranked facts from 2 node(s); budget 100 lines / 1500 tokens; seeds 2 of k=8 (1 exact, 5 below the kNN floor 0.25), neighbours 0
  LIVE CALLS: none — this step makes no LLM call at all.

====================================================================================================
c — read('C4')
====================================================================================================
  A NAME. Reconcile recorded every surface name it routed as an alias, so a search by
  the name I happen to know reaches the node the dreamer named something else.

  QUERY: C4
  ------ rendered block, verbatim, exactly as an LLM reader receives it ------
  | C4 (service) [n-006] as of 2026-09-11, 2 entries
  | attribute: returns 31 tools after #248; count was 24 before #248 (x2)
  | 
  | more: explain(n-006), neighbors(n-006)
  ---------------------------------------------------------------------------
  SEEDS (7 candidate(s) of k=8; 1 exact, 6 dropped by the kNN floor 0.25) — why each node was in the read, or was not:
    KEPT     n-006  alias       sim=1.0000  on 'C4'                     EXACT — the node's name or one of its recorded aliases
    DROPPED  n-007  knn         sim=0.1566  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-004  knn         sim=0.1536  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-003  knn         sim=0.1295  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-001  knn         sim=0.1142  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-005  knn         sim=0.1031  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-002  knn         sim=0.0918  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    ^ 6 candidate(s) below 0.25 seeded nothing and expanded nothing: an exact hit is never floored, a measured one is.
  nodes emitted   : 1  (C4)
  lines           : 1 of a 100-line budget
  tokens          : 40 (block incl. headers) against a ceiling of 1516
  duplicates      : 0 fact(s) folded away   (nothing in this read stated one fact twice)
  saturated       : False
  vectors bought  : 1
  contract_digest : 8ef6742dc2db405e892d9b4f72266a039c7c0c481c7f9e30fedce5e34c13d20e
  receipt detail  : 1 of 1 ranked facts from 1 node(s); budget 100 lines / 1500 tokens; seeds 1 of k=8 (1 exact, 6 below the kNN floor 0.25), neighbours 0
  LIVE CALLS: none — this step makes no LLM call at all.

====================================================================================================
d — read('the gateway refuses my Host header')
====================================================================================================
  A TASK, in my own words. No identifier, no name — nothing to match exactly, which is
  what embedding similarity is for. The seeds below say 'knn' and that is correct.

  QUERY: the gateway refuses my Host header
  ------ rendered block, verbatim, exactly as an LLM reader receives it ------
  | JedAI Gateway (service) [n-001] as of 2026-09-11, 10 entries, aka LiteLLM proxy, gateway
  | rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
  | rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
  | is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)
  | attribute: before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it (x2)
  | attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
  | FRONTS JedAI models [n-002]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)
  | 
  | agent-memory MCP server (service) [n-003] as of 2026-09-11, 4 entries, aka agent-memory, agent-memory MCP
  | is: MCP server providing agent memory, deployed via chart introduced in #240
  | attribute: reaches JedAI Gateway successfully since #245 fixed the Host header rewrite (x2)
  | attribute: #245 also fixed chart deploying agent-memory from wrong image tag
  | attribute: before #245, could not reach JedAI Gateway because gateway refused Host header; #245 fixed the Host rewrite
  | from chart [n-004] DEPLOYS: chart is where the agent-memory MCP deploy lives; came in with #240
  | 
  | session affinity (attribute) [n-005] as of 2026-09-11, 3 entries, aka session-affinity
  | attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
  | unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand (x2)
  | 
  | more: explain(n-001, n-003, n-005), neighbors(n-001, n-003, n-005)
  ---------------------------------------------------------------------------
  SEEDS (7 candidate(s) of k=8; 1 exact, 4 dropped by the kNN floor 0.25) — why each node was in the read, or was not:
    KEPT     n-001  alias       sim=1.0000  on 'gateway'                EXACT — the node's name or one of its recorded aliases
    KEPT     n-003  knn         sim=0.3686  on (whole query)            measured — embedding neighbour of the whole query
    KEPT     n-005  knn         sim=0.2634  on (whole query)            measured — embedding neighbour of the whole query
    DROPPED  n-002  knn         sim=0.1590  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-004  knn         sim=0.1371  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-007  knn         sim=0.1170  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-006  knn         sim=0.0915  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    ^ 4 candidate(s) below 0.25 seeded nothing and expanded nothing: an exact hit is never floored, a measured one is.
  nodes emitted   : 3  (JedAI Gateway, agent-memory MCP server, session affinity)
  lines           : 13 of a 100-line budget
  tokens          : 439 (block incl. headers) against a ceiling of 1548
  duplicates      : 2 fact(s) folded away   <-- rank.dedupe_lines: one fact stated twice, kept once, before the budget cut
  saturated       : False
  vectors bought  : 1
  contract_digest : 3fb25675830f5520303d2e4aff1510600e2aa07fc7c8c8a39617b3248d15796f
  receipt detail  : 13 of 13 ranked facts from 3 node(s); dropped 2 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 3 of k=8 (1 exact, 4 below the kNN floor 0.25), neighbours 2
  LIVE CALLS: none — this step makes no LLM call at all.

====================================================================================================
WHAT THE FOUR READS ABOVE ACTUALLY SHOW
====================================================================================================
  Block order, per read — the order an LLM reader receives them in:
    a: ['JedAI Gateway', 'text-embedding-3', 'JedAI models', 'agent-memory MCP server', 'chart', 'C4', 'session affinity']
    b: ['session affinity', 'C4']
    c: ['C4']
    d: ['JedAI Gateway', 'agent-memory MCP server', 'session affinity']

  4 distinct block order(s) over 7 node(s) in the scope.
  Membership, per read — how much of the scope each one actually emitted:
    a: 7 of 7   the whole scope
    b: 2 of 7   not emitted: ['JedAI Gateway', 'JedAI models', 'agent-memory MCP server', 'chart', 'text-embedding-3']
    c: 1 of 7   not emitted: ['JedAI Gateway', 'JedAI models', 'agent-memory MCP server', 'chart', 'session affinity', 'text-embedding-3']
    d: 3 of 7   not emitted: ['C4', 'JedAI models', 'chart', 'text-embedding-3']

  Two things decide those two tables, and they are not the same thing:

  1. WHAT IS IN A READ is decided by seeding and the budget. read() seeds at most
     read_k=8 nodes — exact hits first, kNN filling the rest — then expands 1-hop,
     so a query can only exclude a node once the scope is bigger than seeds plus
     their neighbours. brief() has no read_k at all: it ranks all 7 nodes and
     emits them until the budget bites, which is exactly what a session-start read
     should do. So brief() is the read that carries the most and decides the least.

  2. WHAT ORDER IT COMES IN is decided by rank, and there are two floors, not one.
     Block order follows the rank of each node's best line.
     rank.EXACT_FLOOR (2e+06) lifts every line of a node the query NAMED, so a query
     read leads with what was asked for. rank.CONSTRAINT_FLOOR
     (1e+06) then floats every rule above the whole measured set, so the 0 node(s)
     carrying one () come next,
     and inside every block a rule is still first. An agent cannot search its way
     past a rule — but it is no longer made to read past two of somebody else's
     rules to reach the node it named. brief() passes no exact ids and is unchanged:
     with no query there is nothing named, so its constraint-holders lead.

  What the query DID decide is on the page too — the seeds, which are the answer to
  'why is this node here at all' and the half of retrieval similarity cannot do:
    a: 0 seed(s), 0 exact -> (none)
    b: 7 seed(s), 1 exact -> n-005 identifier on '#246'
    c: 7 seed(s), 1 exact -> n-006 alias on 'C4'
    d: 7 seed(s), 1 exact -> n-001 alias on 'gateway'

====================================================================================================
e. node(<node id>) -- re-read ONE concept, exactly as a read would have shown it
====================================================================================================
  I have an id from a block header or from an ingest summary, and I want that one
  concept whole rather than whatever a query would rank into a budget. Same
  renderer as a read, so the block is the same text -- including the header's entry
  count, which comes from the ledger and not from adding up the facts' evidence.
  A pure projection: it records no read, so walking past a node does not move it
  up the survival ranking.

  node(n-001)
  ------ rendered verbatim, exactly as an LLM reader receives it -------------
  | JedAI Gateway (service) [n-001] as of 2026-09-11, 10 entries, aka LiteLLM proxy, gateway
  | rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
  | rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
  | is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)
  | attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
  | attribute: before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it (x2)
  | FRONTS JedAI models [n-002]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)
  | more: explain(n-001), neighbors(n-001)
  ---------------------------------------------------------------------------
  vectors bought  : 0
  LIVE CALLS: none — this step makes no LLM call at all.

====================================================================================================
f. neighbors(<node id>) -- the walk: what this concept connects to, and why
====================================================================================================
  One line per typed edge, both directions, each carrying the id on the OTHER end.
  This is the half a flat key-value memory cannot do: the answer to 'what else
  should I know about before I touch this' is an edge, with its own claim and its
  own evidence, and the footer hands back the ids to read next.

  neighbors(n-001)
  ------ rendered verbatim, exactly as an LLM reader receives it -------------
  | JedAI Gateway (service) [n-001], 1 relations
  | FRONTS JedAI models [n-002]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)
  | more: explain(n-002), neighbors(n-002)
  ---------------------------------------------------------------------------
  vectors bought  : 0
  LIVE CALLS: none — this step makes no LLM call at all.

  AND THEN ONE HOP FURTHER -- every id that footer named, re-read as a block:

  node(n-002) -- reached from [n-001] by FRONTS
  ------ rendered verbatim, exactly as an LLM reader receives it -------------
  | JedAI models (service) [n-002] as of 2026-09-11, 2 entries
  | is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)
  | from JedAI Gateway [n-001] FRONTS: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)
  | more: explain(n-002), neighbors(n-002)
  ---------------------------------------------------------------------------

====================================================================================================
g. explain(<node id>) -- the deep read the footer's first call points at
====================================================================================================
  A read is bounded to L lines a node, so 'what is the evidence for this' is a
  question it structurally cannot answer. This answers it: every ledger entry on
  the node, newest first, superseded ones included and marked. Zero LLM calls,
  no embedding, no budget — it is the record, not a read.

  explain(n-001) -- the deep read the footer above names
  ------ rendered verbatim, exactly as an LLM reader receives it -------------
  | JedAI Gateway (service) [n-001] as of 2026-09-11, 10 entries, aka LiteLLM proxy, gateway
  | facts:
  | 2026-09-11 rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2) [e-fc9b2270fe5a47ec9e9297143a82e12f, e-d0b2ca5e9cfb4264a7b5caea7496cfc4]
  | 2026-09-11 rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2) [e-0976177bcc1d4505a3209847885a4a15, e-fe716a06788d4e80add2cf2afe6cfb5a]
  | 2026-09-11 is: LiteLLM proxy fronting the JedAI models, not a model itself (x2) [e-ca478da7f99146d9bb88abd46a1cb388, e-bef62a6485e348be8a083a8fd6e9ada7]
  | 2026-09-11 attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves [e-fe716a06788d4e80add2cf2afe6cfb5a]
  | 2026-09-11 attribute: before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it (x2) [e-9590178378db4b689615facabd2118a8, e-c820b984fd6d4c67bbf774b625b32502]
  | 2026-09-11 superseded: chat default was claude-haiku-4-5 (x2) [e-86cc728cfda342fda1385e2d60a4d3ff, e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687]
  | relations:
  | 2026-09-11 FRONTS JedAI models [n-002]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2) [e-ca478da7f99146d9bb88abd46a1cb388, e-bef62a6485e348be8a083a8fd6e9ada7]
  | evidence:
  | e-fe716a06788d4e80add2cf2afe6cfb5a | 2026-09-11 | attribute | chat default on the gateway moved to claude-sonnet-4-6 as of this morning; claude-haiku-4-5 no longer the default but still resolves. | supersedes e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687
  | e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687 | 2026-09-11 | attribute | chat default on the gateway was claude-haiku-4-5. | SUPERSEDED by e-fe716a06788d4e80add2cf2afe6cfb5a
  | e-c820b984fd6d4c67bbf774b625b32502 | 2026-09-11 | attribute | agent-memory MCP server reaches the gateway successfully since #245 fixed the Host header rewrite.
  | e-d0b2ca5e9cfb4264a7b5caea7496cfc4 | 2026-09-11 | rule | Every real run goes through the JedAI Gateway; no exceptions stated.
  | e-bef62a6485e348be8a083a8fd6e9ada7 | 2026-09-11 | is | JedAI Gateway is the LiteLLM proxy in front of the JedAI models, not a model itself.
  | e-0976177bcc1d4505a3209847885a4a15 | 2026-09-11 | rule | Rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-haiku-4-5, never a dated pin); a dated alias goes stale and the gateway starts refusing it.
  | e-86cc728cfda342fda1385e2d60a4d3ff | 2026-09-11 | attribute | Chat default on the gateway is claude-haiku-4-5.
  | e-9590178378db4b689615facabd2118a8 | 2026-09-11 | attribute | Before #245 the gateway refused the Host header outright, so the agent-memory MCP server could not reach it; #245 fixed the Host rewrite.
  | e-ca478da7f99146d9bb88abd46a1cb388 | 2026-09-11 | is | JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself.
  | e-fc9b2270fe5a47ec9e9297143a82e12f | 2026-09-11 | rule | Every real run goes through the JedAI Gateway; no hermetic mode, no local stub, not even for a quick check.
  | history:
  | 2026-09-11 relation_upserted: reinforced FRONTS n-001 to n-002, evidence 2
  | 2026-09-11 node_rewritten: 6 fact(s), type=service
  | 2026-09-11 relation_upserted: reinforced FRONTS n-001 to n-002, evidence 2
  | 2026-09-11 aliases_added: aliases now LiteLLM proxy, gateway
  | 2026-09-11 aliases_added: aliases now LiteLLM proxy
  | 2026-09-11 relation_upserted: reinforced FRONTS n-001 to n-002, evidence 1
  | 2026-09-11 node_rewritten: 5 fact(s), type=service
  | 2026-09-11 relation_upserted: created FRONTS n-001 to n-002, evidence 1
  | 2026-09-11 node_created: created 'JedAI Gateway' as service
  | aliases: LiteLLM proxy, gateway
  ---------------------------------------------------------------------------
  lines           : 32 (unbounded — every entry, newest first)
  tokens          : 942 (no budget applies; this is the record, not a read)
  vectors bought  : 0
  LIVE CALLS: none — this step makes no LLM call at all.

====================================================================================================
6a. DREAM GLOBAL, M FORCED -- the edge-type vocabulary is bounded too
====================================================================================================
  N bounds how many CONCEPTS a scope may hold. M bounds how many kinds of BELIEF
  it may hold between them, and it is the ceiling the amendment added: a scope
  that coins a new relation type for every pair has a vocabulary, not a schema,
  and nothing downstream can traverse it by type.

  the scope's vocabulary now : {'DEPLOYS': 1, 'FRONTS': 1}
  M at the engineering preset: 30  -- no pressure at all here

  So M is set to 1 for one pass -- one less than the vocabulary -- which forces
  exactly one rename and nothing else. WHICH type is doomed is arithmetic and not
  the model's: the least used one, ties broken by name, through the same
  dream.compaction_targets the prompt and the validation both read. What the model
  decides is the only part that is a judgement about meaning -- what it folds INTO.

  MUST COMPACT: ['DEPLOYS']

  LIVE CALLS: 1
    DREAM-GLOBAL   2.49s  prompt 11,742 chars -> response   219 chars

  edge-type pressure the pass computed: ['DEPLOYS']
  ops applied                         : 1

  OPS, from the receipt stream:
      1. graph_mutated            subject=DEPLOYS->FRONTS in=c433e25bd374 out=8b7daa100f1d
         renamed 1 relation(s) from DEPLOYS to FRONTS
      2. dream_edge_type_renamed  subject=DEPLOYS      in=7e054247a62e out=a07a3e77b3bb
         1 edge(s) re-labelled DEPLOYS to FRONTS: scope must keep at most 1 edge type; DEPLOYS (1 edge) is the least-used type and must be folded into FRONTS, the type that is staying

  vocabulary before : {'DEPLOYS': 1, 'FRONTS': 1}
  vocabulary after  : {'FRONTS': 2}
  types that went   : ['DEPLOYS']
  types that arrived: (none -- a fold into a type that was already held)

  THE RENAME, EDGE BY EDGE. The claim and the evidence are untouched: a
  compaction re-LABELS a belief, it does not restate or re-evidence one.
    r-002: DEPLOYS -> FRONTS   claim='chart is where the agent-memory MCP deploy lives; came in with #240'   evidence=['e-cdb046a300714229a19c3732313a9ce5']

  GRAPH AFTER THE EDGE-TYPE COMPACTION (M=1): 7 node(s)
    C4 (service) [n-006] as of 2026-09-11, 2 entries   dirty=False reads=3 facts=1
      attribute: returns 31 tools after #248; count was 24 before #248 (x2)
    JedAI Gateway (service) [n-001] as of 2026-09-11, 10 entries, aka LiteLLM proxy, gateway   dirty=False reads=2 facts=6
      rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
      rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
      is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)
      attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
      attribute: before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it (x2)
      superseded: chat default was claude-haiku-4-5 (x2)
      FRONTS [n-002]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)   [r-001]
    JedAI models (service) [n-002] as of 2026-09-11, 2 entries   dirty=False reads=1 facts=1
      is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)
    agent-memory MCP server (service) [n-003] as of 2026-09-11, 4 entries, aka agent-memory, agent-memory MCP   dirty=False reads=2 facts=4
      is: MCP server providing agent memory, deployed via chart introduced in #240
      attribute: before #245, could not reach JedAI Gateway because gateway refused Host header; #245 fixed the Host rewrite
      attribute: #245 also fixed chart deploying agent-memory from wrong image tag
      attribute: reaches JedAI Gateway successfully since #245 fixed the Host header rewrite (x2)
    chart (artifact) [n-004] as of 2026-09-11, 1 entries   dirty=False reads=1 facts=1
      attribute: agent-memory MCP deploy lives in this chart; introduced in #240
      FRONTS [n-003]: chart is where the agent-memory MCP deploy lives; came in with #240   [r-002]
    session affinity (attribute) [n-005] as of 2026-09-11, 3 entries, aka session-affinity   dirty=False reads=3 facts=2
      attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
      unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand (x2)
    text-embedding-3 (model) [n-007] as of 2026-09-11, 1 entries   dirty=False reads=1 facts=2
      rule: must be selected explicitly; no default embedding model is applied automatically
      attribute: embedding alias text-embedding-3 at 3072 dimensions
  RELATIONS: 2 edge(s) over 1 type(s), each carrying its own evidence
    r-001  n-001 (JedAI Gateway) -FRONTS-> n-002 (JedAI models)  evidence=2 [cooccurrence says 2]
    r-002  n-004 (chart) -FRONTS-> n-003 (agent-memory MCP server)  evidence=1 [cooccurrence says 1]
  EDGE TYPES: {'FRONTS': 2}

====================================================================================================
6b. DREAM GLOBAL, N FORCED — down to N=3. This is where the bound bites.
====================================================================================================
  A forced slate is `pressure` DISJOINT pairs, so it needs 2 x pressure distinct
  nodes and each pair removes exactly one. One pass can therefore remove at most
  count // 2 nodes — so reaching N=3 from 7 takes more than one pass,
  and the budget is stepped down to what each pass can actually satisfy.

  ---- PASS 1 ----
  count 7, budget for this pass N=4 (target 3)   pressure = max(0, 7 - 4) = 3

  NODE VALUES — recomputed here through pressure.node_value, the same pure
  function dream_global uses. Weights: recency=0.8 reads=1.2 degree=1.0 type=0.6, half-life 30.0d.
    n-007 text-embedding-3 type=model      reads=1 degree=0  value=2.2318
    n-004 chart            type=artifact   reads=1 degree=1  value=2.9249
    n-002 JedAI models     type=service    reads=1 degree=1  value=2.9849
    n-005 session affinity type=attribute  reads=3 degree=0  value=3.0636
    n-006 C4               type=service    reads=3 degree=0  value=3.1235
    n-001 JedAI Gateway    type=service    reads=2 degree=1  value=3.4715
    n-003 agent-memory MCP server type=service    reads=2 degree=1  value=3.4715

  SLATE, recomputed deterministically. The doomed node is the lowest-value one;
  its peer is chosen in one order over the WHOLE scope: the most LEDGER ENTRIES
  naming both, then the most conversational turns the ledger says they share, then
  embedding similarity. The survivor is the higher-value half and KEEPS ITS OWN NAME;
  the doomed node's name becomes one of its aliases. Each mandate carries the evidence
  that chose the peer, in the same words the prompt states it to the model:
    MUST MERGE: 'text-embedding-3' (n-007) INTO 'JedAI Gateway' (n-001) because same turn x1
        shared entries=0  shared turns=1  similarity=0.3021
    MUST MERGE: 'chart' (n-004) INTO 'agent-memory MCP server' (n-003) because linked x1
        shared entries=1  shared turns=1  similarity=0.7180
    MUST MERGE: 'JedAI models' (n-002) INTO 'C4' (n-006) because nearest by embedding
        shared entries=0  shared turns=0  similarity=0.2174
    The model is told WHICH node goes, WHICH name survives and ON WHAT EVIDENCE;
    it decides only what the survivor's lines and type say. 'nearest by embedding'
    is the weakest of the three mandates and says so on its face — it is no
    warrant for a line asserting a relationship the ledger never recorded.
  LIVE CALLS: 1
    DREAM-GLOBAL  12.65s  prompt 12,321 chars -> response 3,133 chars

  pressure the pass computed: 3
  slate the pass mandated   : ['n-007 INTO n-001', 'n-004 INTO n-003', 'n-002 INTO n-006']
  same pairs as recomputed  : True  (pure function, same inputs — a False here is a clock-skew tiebreak, not a defect)
  ops applied               : 3
  count 7 -> 4   (N=4)

  OPS, from the receipt stream:
      1. graph_mutated            subject=n-001        in=53d6ac12fc14 out=6ebd6c7062b1
         n-007 absorbed into n-001 as 'JedAI Gateway'
      2. dream_merged             subject=n-001        in=45e0294b60c4 out=7ccca4846251
         n-007 absorbed into n-001 as 'JedAI Gateway': forced merge: text-embedding-3 (n-007) into JedAI Gateway (n-001) per slate; same turn x1
      3. graph_mutated            subject=n-003        in=6e7ef6687881 out=6c0c4b4878ce
         n-004 absorbed into n-003 as 'agent-memory MCP server'
      4. dream_merged             subject=n-003        in=45e0294b60c4 out=7ccca4846251
         n-004 absorbed into n-003 as 'agent-memory MCP server': forced merge: chart (n-004) into agent-memory MCP server (n-003) per slate; linked x1
      5. graph_mutated            subject=n-006        in=a71884b749cd out=d09912889139
         n-002 absorbed into n-006 as 'C4'
      6. dream_merged             subject=n-006        in=45e0294b60c4 out=7ccca4846251
         n-002 absorbed into n-006 as 'C4': forced merge: JedAI models (n-002) into C4 (n-006) per slate; nearest by embedding

  GRAPH AFTER PASS 1 (N=4): 4 node(s)
    C4 (service) [n-006] as of 2026-09-11, 4 entries, aka JedAI models   dirty=False reads=3 facts=2
      is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)
      attribute: returns 31 tools after #248; count was 24 before #248 (x2)
    JedAI Gateway (service) [n-001] as of 2026-09-11, 11 entries, aka LiteLLM proxy, gateway, text-embedding-3   dirty=False reads=2 facts=7
      rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
      rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
      is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)
      attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
      attribute: before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it (x2)
      attribute: embedding alias text-embedding-3 at 3072 dimensions; must be selected explicitly, no default applied automatically
      superseded: chat default was claude-haiku-4-5 (x2)
      FRONTS [n-006]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)   [r-001]
    agent-memory MCP server (service) [n-003] as of 2026-09-11, 4 entries, aka agent-memory, agent-memory MCP, chart   dirty=False reads=2 facts=4
      is: MCP server providing agent memory, deployed via chart introduced in #240
      attribute: chart deploying agent-memory introduced in #240
      attribute: #245 fixed chart deploying agent-memory from wrong image tag
      attribute: reaches JedAI Gateway successfully since #245 fixed the Host header rewrite (x2)
    session affinity (attribute) [n-005] as of 2026-09-11, 3 entries, aka session-affinity   dirty=False reads=3 facts=2
      attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
      unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand (x2)
  RELATIONS: 1 edge(s) over 1 type(s), each carrying its own evidence
    r-001  n-001 (JedAI Gateway) -FRONTS-> n-006 (C4)  evidence=2 [cooccurrence says 2]
  EDGE TYPES: {'FRONTS': 1}
  SURVIVOR NAMES: 4 of the 7 node(s) this pass started with are still here, each under its own name.

  RELATIONS AFTER THE PASS: 1 edge(s), none dangling.
  A merge re-points the edges of the nodes it absorbs and drops the self-loop
  that makes -- so no third node is left pointing at a name that went away.
    r-001  FRONTS [n-006]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)
  GRAPH AFTER THE FORCED-1 PASS: 4 node(s)
    C4 (service) [n-006] as of 2026-09-11, 4 entries, aka JedAI models   dirty=False reads=3 facts=2
      is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)
      attribute: returns 31 tools after #248; count was 24 before #248 (x2)
    JedAI Gateway (service) [n-001] as of 2026-09-11, 11 entries, aka LiteLLM proxy, gateway, text-embedding-3   dirty=False reads=2 facts=7
      rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
      rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
      is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)
      attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
      attribute: before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it (x2)
      attribute: embedding alias text-embedding-3 at 3072 dimensions; must be selected explicitly, no default applied automatically
      superseded: chat default was claude-haiku-4-5 (x2)
      FRONTS [n-006]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)   [r-001]
    agent-memory MCP server (service) [n-003] as of 2026-09-11, 4 entries, aka agent-memory, agent-memory MCP, chart   dirty=False reads=2 facts=4
      is: MCP server providing agent memory, deployed via chart introduced in #240
      attribute: chart deploying agent-memory introduced in #240
      attribute: #245 fixed chart deploying agent-memory from wrong image tag
      attribute: reaches JedAI Gateway successfully since #245 fixed the Host header rewrite (x2)
    session affinity (attribute) [n-005] as of 2026-09-11, 3 entries, aka session-affinity   dirty=False reads=3 facts=2
      attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
      unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand (x2)
  RELATIONS: 1 edge(s) over 1 type(s), each carrying its own evidence
    r-001  n-001 (JedAI Gateway) -FRONTS-> n-006 (C4)  evidence=2 [cooccurrence says 2]
  EDGE TYPES: {'FRONTS': 1}

  ---- PASS 2 ----
  count 4, budget for this pass N=3 (target 3)   pressure = max(0, 4 - 3) = 1

  NODE VALUES — recomputed here through pressure.node_value, the same pure
  function dream_global uses. Weights: recency=0.8 reads=1.2 degree=1.0 type=0.6, half-life 30.0d.
    n-003 agent-memory MCP server type=service    reads=2 degree=0  value=2.7783
    n-005 session affinity type=attribute  reads=3 degree=0  value=3.0635
    n-001 JedAI Gateway    type=service    reads=2 degree=1  value=3.4715
    n-006 C4               type=service    reads=3 degree=1  value=3.8167

  SLATE, recomputed deterministically. The doomed node is the lowest-value one;
  its peer is chosen in one order over the WHOLE scope: the most LEDGER ENTRIES
  naming both, then the most conversational turns the ledger says they share, then
  embedding similarity. The survivor is the higher-value half and KEEPS ITS OWN NAME;
  the doomed node's name becomes one of its aliases. Each mandate carries the evidence
  that chose the peer, in the same words the prompt states it to the model:
    MUST MERGE: 'agent-memory MCP server' (n-003) INTO 'JedAI Gateway' (n-001) because linked x2
        shared entries=2  shared turns=2  similarity=0.5068
    The model is told WHICH node goes, WHICH name survives and ON WHAT EVIDENCE;
    it decides only what the survivor's lines and type say. 'nearest by embedding'
    is the weakest of the three mandates and says so on its face — it is no
    warrant for a line asserting a relationship the ledger never recorded.
  LIVE CALLS: 1
    DREAM-GLOBAL   8.39s  prompt 10,535 chars -> response 1,891 chars

  pressure the pass computed: 1
  slate the pass mandated   : ['n-003 INTO n-001']
  same pairs as recomputed  : True  (pure function, same inputs — a False here is a clock-skew tiebreak, not a defect)
  ops applied               : 1
  count 4 -> 3   (N=3)

  OPS, from the receipt stream:
      1. graph_mutated            subject=n-001        in=7337f0b21d64 out=79b0aa3e67f1
         n-003 absorbed into n-001 as 'JedAI Gateway'
      2. dream_merged             subject=n-001        in=f520a9d9a65a out=72009bf9bd62
         n-003 absorbed into n-001 as 'JedAI Gateway': forced merge: agent-memory MCP server linked x2 to JedAI Gateway; absorb its facts into the gateway node

  GRAPH AFTER PASS 2 (N=3): 3 node(s)
    C4 (service) [n-006] as of 2026-09-11, 4 entries, aka JedAI models   dirty=False reads=3 facts=2
      is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)
      attribute: returns 31 tools after #248; count was 24 before #248 (x2)
    JedAI Gateway (service) [n-001] as of 2026-09-11, 13 entries, aka LiteLLM proxy, gateway, text-embedding-3, agent-memory MCP server, agent-memory, agent-memory MCP, chart   dirty=False reads=2 facts=8
      rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
      rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
      is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)
      attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
      attribute: embedding alias text-embedding-3 at 3072 dimensions; must be selected explicitly, no default applied automatically
      attribute: agent-memory MCP server deployed via chart introduced in #240; #245 fixed wrong image tag (x2)
      attribute: agent-memory reaches gateway successfully since #245 fixed Host header rewrite (x2)
      superseded: chat default was claude-haiku-4-5 (x2)
      FRONTS [n-006]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)   [r-001]
    session affinity (attribute) [n-005] as of 2026-09-11, 3 entries, aka session-affinity   dirty=False reads=3 facts=2
      attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
      unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand (x2)
  RELATIONS: 1 edge(s) over 1 type(s), each carrying its own evidence
    r-001  n-001 (JedAI Gateway) -FRONTS-> n-006 (C4)  evidence=2 [cooccurrence says 2]
  EDGE TYPES: {'FRONTS': 1}
  SURVIVOR NAMES: 3 of the 4 node(s) this pass started with are still here, each under its own name.

  RELATIONS AFTER THE PASS: 1 edge(s), none dangling.
  A merge re-points the edges of the nodes it absorbs and drops the self-loop
  that makes -- so no third node is left pointing at a name that went away.
    r-001  FRONTS [n-006]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)
  GRAPH AFTER THE FORCED-2 PASS: 3 node(s)
    C4 (service) [n-006] as of 2026-09-11, 4 entries, aka JedAI models   dirty=False reads=3 facts=2
      is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)
      attribute: returns 31 tools after #248; count was 24 before #248 (x2)
    JedAI Gateway (service) [n-001] as of 2026-09-11, 13 entries, aka LiteLLM proxy, gateway, text-embedding-3, agent-memory MCP server, agent-memory, agent-memory MCP, chart   dirty=False reads=2 facts=8
      rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
      rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
      is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)
      attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
      attribute: embedding alias text-embedding-3 at 3072 dimensions; must be selected explicitly, no default applied automatically
      attribute: agent-memory MCP server deployed via chart introduced in #240; #245 fixed wrong image tag (x2)
      attribute: agent-memory reaches gateway successfully since #245 fixed Host header rewrite (x2)
      superseded: chat default was claude-haiku-4-5 (x2)
      FRONTS [n-006]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)   [r-001]
    session affinity (attribute) [n-005] as of 2026-09-11, 3 entries, aka session-affinity   dirty=False reads=3 facts=2
      attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
      unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand (x2)
  RELATIONS: 1 edge(s) over 1 type(s), each carrying its own evidence
    r-001  n-001 (JedAI Gateway) -FRONTS-> n-006 (C4)  evidence=2 [cooccurrence says 2]
  EDGE TYPES: {'FRONTS': 1}

  N=3 ENFORCED: 3 node(s) remain.

====================================================================================================
AFTER THE SQUEEZE — read('C4') once more, on the N=3 graph
====================================================================================================
  The same query section c asked of the 7-node graph, asked again now that
  compression has taken most of those nodes away. This is the one read worth
  repeating, because a merge is where a search key is most likely to be lost:
  the node the answer was on may no longer exist. merge_nodes unions the absorbed
  node's name AND its aliases into the survivor's aliases, so every spelling that
  ever pointed at the concept still points at it — through the merge, and exactly
  rather than by resemblance.

  QUERY: C4
  ------ rendered block, verbatim, exactly as an LLM reader receives it ------
  | C4 (service) [n-006] as of 2026-09-11, 4 entries, aka JedAI models
  | is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)
  | attribute: returns 31 tools after #248; count was 24 before #248 (x2)
  | from JedAI Gateway [n-001] FRONTS: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)
  | 
  | JedAI Gateway (service) [n-001] as of 2026-09-11, 13 entries, aka LiteLLM proxy, gateway, text-embedding-3, agent-memory MCP server, agent-memory, agent-memory MCP, chart
  | rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
  | rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
  | 
  | more: explain(n-006, n-001), neighbors(n-006, n-001)
  ---------------------------------------------------------------------------
  SEEDS (3 candidate(s) of k=8; 1 exact, 2 dropped by the kNN floor 0.25) — why each node was in the read, or was not:
    KEPT     n-006  alias       sim=1.0000  on 'C4'                     EXACT — the node's name or one of its recorded aliases
    DROPPED  n-001  knn         sim=0.1439  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-005  knn         sim=0.1031  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    ^ 2 candidate(s) below 0.25 seeded nothing and expanded nothing: an exact hit is never floored, a measured one is.
  nodes emitted   : 2  (C4, JedAI Gateway)
  lines           : 5 of a 100-line budget
  tokens          : 218 (block incl. headers) against a ceiling of 1532
  duplicates      : 1 fact(s) folded away   <-- rank.dedupe_lines: one fact stated twice, kept once, before the budget cut
  saturated       : False
  vectors bought  : 1
  contract_digest : a9f5edf722fdac9df49bd3dee3032fe1f360be3020b786e969f24f55b9d1b813
  receipt detail  : 5 of 5 ranked facts from 2 node(s); dropped 1 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 1 of k=8 (1 exact, 2 below the kNN floor 0.25), neighbours 1
  LIVE CALLS: none — this step makes no LLM call at all.

  the node carrying the corrected '31' tool count: ['C4']
  seeded n-006 (C4) by alias on 'C4' at similarity 1.0000
    its aliases now: ['JedAI models']

====================================================================================================
REPLAY PROOF -- the compression is provable, not merely trusted
====================================================================================================
  Every write above went through a journal. Each mutation is a DreamEvent in the
  ledger carrying the node and relation content BEFORE and AFTER, and nothing
  else: no vectors, because a vector is derived from the content, and no
  bookkeeping, because 'somebody read this' is not a belief changing.

  So 'this graph is what the record says' can be CHECKED rather than asserted.
  The journal is applied in order onto a fresh empty graph -- no model call, no
  embedding -- and the two are compared by a digest over what they ASSERT: names,
  types, aliases, facts, relations. Not ids, which a replay mints itself, and not
  timestamps, which record when a claim arrived rather than what it says.

  replay(scope)
  ------ rendered verbatim, exactly as an LLM reader receives it -------------
  | scope: repo:jedai/memotron
  | live content digest: 1e8a0616df85286f36496e1456a041d0e19aff15abeecb33e86b4ca0a9705d8c
  | replayed content digest: 1e8a0616df85286f36496e1456a041d0e19aff15abeecb33e86b4ca0a9705d8c
  | digests equal: true
  | digest covers: names, types, aliases, facts, relations
  | journalled events replayed: 34
  ---------------------------------------------------------------------------
  vectors bought  : 0
  LIVE CALLS: none — this step makes no LLM call at all.

  the replayed graph holds 3 node(s) and 1 relation(s), rebuilt from the journal alone:
    n-006 C4 (service)  facts=2  aka ['JedAI models']
    n-001 JedAI Gateway (service)  facts=8  aka ['LiteLLM proxy', 'gateway', 'text-embedding-3', 'agent-memory MCP server', 'agent-memory', 'agent-memory MCP', 'chart']
    n-005 session affinity (attribute)  facts=2  aka ['session-affinity']

  Its node ids are its OWN -- the store mints them -- which is exactly why the
  digest is over content. Two stores holding one scope digest identically; a graph
  somebody wrote to behind the journal's back does not, and that is the case a
  proof exists for.

  PROVED: 34 journalled event(s) rebuild this scope exactly.

====================================================================================================
7. READ — two queries, ZERO live calls. Deterministic: embed, kNN, 1-hop, rank, cut.
====================================================================================================

  QUERY: what do I know about the gateway
  ------ rendered block, verbatim, exactly as an LLM reader receives it ------
  | JedAI Gateway (service) [n-001] as of 2026-09-11, 13 entries, aka LiteLLM proxy, gateway, text-embedding-3, agent-memory MCP server, agent-memory, agent-memory MCP, chart
  | rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
  | rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
  | is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)
  | attribute: agent-memory MCP server deployed via chart introduced in #240; #245 fixed wrong image tag (x2)
  | attribute: agent-memory reaches gateway successfully since #245 fixed Host header rewrite (x2)
  | attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
  | attribute: embedding alias text-embedding-3 at 3072 dimensions; must be selected explicitly, no default applied automatically
  | FRONTS C4 [n-006]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)
  | 
  | more: explain(n-001), neighbors(n-001)
  ---------------------------------------------------------------------------
  SEEDS (3 candidate(s) of k=8; 1 exact, 2 dropped by the kNN floor 0.25) — why each node was in the read, or was not:
    KEPT     n-001  alias       sim=1.0000  on 'gateway'                EXACT — the node's name or one of its recorded aliases
    DROPPED  n-006  knn         sim=0.1914  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    DROPPED  n-005  knn         sim=0.1636  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    ^ 2 candidate(s) below 0.25 seeded nothing and expanded nothing: an exact hit is never floored, a measured one is.
  nodes emitted   : 1  (JedAI Gateway)
  lines           : 8 of a 100-line budget
  tokens          : 282 (block incl. headers) against a ceiling of 1516
  duplicates      : 1 fact(s) folded away   <-- rank.dedupe_lines: one fact stated twice, kept once, before the budget cut
  saturated       : False
  vectors bought  : 1
  contract_digest : 877ac82440d08d86219cbde4be42a269aac4fde81f60ae553fddb51bfec13bc7
  receipt detail  : 8 of 8 ranked facts from 1 node(s); dropped 1 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 1 of k=8 (1 exact, 2 below the kNN floor 0.25), neighbours 1

  QUERY: how many tools does the C4 memory server return
  ------ rendered block, verbatim, exactly as an LLM reader receives it ------
  | C4 (service) [n-006] as of 2026-09-11, 4 entries, aka JedAI models
  | is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)
  | attribute: returns 31 tools after #248; count was 24 before #248 (x2)
  | from JedAI Gateway [n-001] FRONTS: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)
  | 
  | JedAI Gateway (service) [n-001] as of 2026-09-11, 13 entries, aka LiteLLM proxy, gateway, text-embedding-3, agent-memory MCP server, agent-memory, agent-memory MCP, chart
  | rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
  | rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
  | is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)
  | attribute: agent-memory MCP server deployed via chart introduced in #240; #245 fixed wrong image tag (x2)
  | attribute: agent-memory reaches gateway successfully since #245 fixed Host header rewrite (x2)
  | attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
  | attribute: embedding alias text-embedding-3 at 3072 dimensions; must be selected explicitly, no default applied automatically
  | 
  | more: explain(n-006, n-001), neighbors(n-006, n-001)
  ---------------------------------------------------------------------------
  SEEDS (3 candidate(s) of k=8; 1 exact, 1 dropped by the kNN floor 0.25) — why each node was in the read, or was not:
    KEPT     n-006  alias       sim=1.0000  on 'C4'                     EXACT — the node's name or one of its recorded aliases
    KEPT     n-001  knn         sim=0.2889  on (whole query)            measured — embedding neighbour of the whole query
    DROPPED  n-005  knn         sim=0.2033  on (whole query)            measured — embedding neighbour of the whole query, under the 0.25 floor
    ^ 1 candidate(s) below 0.25 seeded nothing and expanded nothing: an exact hit is never floored, a measured one is.
  nodes emitted   : 2  (C4, JedAI Gateway)
  lines           : 10 of a 100-line budget
  tokens          : 342 (block incl. headers) against a ceiling of 1532
  duplicates      : 1 fact(s) folded away   <-- rank.dedupe_lines: one fact stated twice, kept once, before the budget cut
  saturated       : False
  vectors bought  : 1
  contract_digest : cd2a986cc36c0997956012101cae1df9a5a0bdefbdc1444ceb873619b9508b3a
  receipt detail  : 10 of 10 ranked facts from 2 node(s); dropped 1 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 2 of k=8 (1 exact, 1 below the kNN floor 0.25), neighbours 0

  LIVE CALLS: none — this step makes no LLM call at all.

  the same query read twice -> the same contract_digest: True

====================================================================================================
8. DEEP -- 'if needed', the header's node id is handed back to the ledger.
====================================================================================================
  node n-001: JedAI Gateway (service) [n-001] as of 2026-09-11, 13 entries, aka LiteLLM proxy, gateway, text-embedding-3, agent-memory MCP server, agent-memory, agent-memory MCP, chart
  its 8 compressed fact(s) are what a reader normally gets:
    real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check
    only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it
    LiteLLM proxy fronting the JedAI models, not a model itself
    chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
    embedding alias text-embedding-3 at 3072 dimensions; must be selected explicitly, no default applied automatically
    agent-memory MCP server deployed via chart introduced in #240; #245 fixed wrong image tag
    agent-memory reaches gateway successfully since #245 fixed Host header rewrite
    chat default was claude-haiku-4-5

  ledger.for_node('n-001') — 13 entry/entries, oldest first,
  including anything superseded. This is the unbounded record behind the bounded node:

    e-fc9b2270fe5a47ec9e9297143a82e12f  rule           mode=directive    conf=0.99  turns=[1]
      claim      : Every real run goes through the JedAI Gateway; no hermetic mode, no local stub, not even for a quick check.
      subjects   : ['JedAI Gateway']
      node_ids   : ['n-001']

    e-ca478da7f99146d9bb88abd46a1cb388  is             mode=report       conf=0.99  turns=[2]
      claim      : JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself.
      subjects   : ['JedAI Gateway']
      objects    : ['JedAI models']
      node_ids   : ['n-001', 'n-006']

    e-9590178378db4b689615facabd2118a8  attribute      mode=report       conf=0.97  turns=[3]
      claim      : Before #245 the gateway refused the Host header outright, so the agent-memory MCP server could not reach it; #245 fixed the Host rewrite.
      subjects   : ['JedAI Gateway', 'agent-memory MCP server']
      identifiers: ['#245']
      node_ids   : ['n-001']

    e-1b9152680510459f8b49f69f7310b2ab  attribute      mode=report       conf=0.97  turns=[4]
      claim      : #245 also fixed the chart deploying agent-memory from the wrong image tag.
      subjects   : ['agent-memory']
      identifiers: ['#245']
      node_ids   : ['n-001']

    e-cdb046a300714229a19c3732313a9ce5  attribute      mode=report       conf=0.97  turns=[4]
      claim      : The chart is where the agent-memory MCP deploy lives; that came in with #240.
      subjects   : ['agent-memory MCP']
      objects    : ['chart']
      identifiers: ['#240']
      node_ids   : ['n-001']

    e-86cc728cfda342fda1385e2d60a4d3ff  attribute      mode=report       conf=0.99  turns=[8]
      claim      : Chat default on the gateway is claude-haiku-4-5.
      subjects   : ['JedAI Gateway']
      identifiers: ['claude-haiku-4-5']
      node_ids   : ['n-001']

    e-6300eb1412aa47acb7362a17d130c1f3  attribute      mode=report       conf=0.99  turns=[8]
      claim      : Embedding alias is text-embedding-3 at 3072 dimensions; must be selected explicitly, no default embedding model is applied automatically.
      subjects   : ['text-embedding-3']
      identifiers: ['text-embedding-3', '3072']
      node_ids   : ['n-001']

    e-0976177bcc1d4505a3209847885a4a15  rule           mode=directive    conf=0.99  turns=[10]
      claim      : Rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-haiku-4-5, never a dated pin); a dated alias goes stale and the gateway starts refusing it.
      subjects   : ['LiteLLM proxy']
      identifiers: ['claude-haiku-4-5']
      node_ids   : ['n-001']

    e-bef62a6485e348be8a083a8fd6e9ada7  is             mode=report       conf=0.99  turns=[1]
      claim      : JedAI Gateway is the LiteLLM proxy in front of the JedAI models, not a model itself.
      subjects   : ['JedAI Gateway']
      objects    : ['JedAI models']
      node_ids   : ['n-001', 'n-006']

    e-d0b2ca5e9cfb4264a7b5caea7496cfc4  rule           mode=directive    conf=0.99  turns=[1]
      claim      : Every real run goes through the JedAI Gateway; no exceptions stated.
      subjects   : ['JedAI Gateway']
      node_ids   : ['n-001']

    e-c820b984fd6d4c67bbf774b625b32502  attribute      mode=report       conf=0.97  turns=[2]
      claim      : agent-memory MCP server reaches the gateway successfully since #245 fixed the Host header rewrite.
      subjects   : ['agent-memory MCP server', 'JedAI Gateway']
      identifiers: ['#245']
      node_ids   : ['n-001']

    e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687  attribute      mode=report       conf=0.95  turns=[3]
      claim      : chat default on the gateway was claude-haiku-4-5.
      subjects   : ['gateway']
      identifiers: ['claude-haiku-4-5']
      node_ids   : ['n-001']

    e-fe716a06788d4e80add2cf2afe6cfb5a  attribute      mode=correction   conf=0.97  turns=[3]
      claim      : chat default on the gateway moved to claude-sonnet-4-6 as of this morning; claude-haiku-4-5 no longer the default but still resolves.
      subjects   : ['gateway']
      identifiers: ['claude-sonnet-4-6', 'claude-haiku-4-5']
      supersedes : e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687   <-- the in-episode correction, resolved to its sibling
      node_ids   : ['n-001']

  aliases absorbed into this node (2) — a merge recorded so it can be replayed:
    n-007 -> n-001, 1 entry/entries moved, receipt ffe53e709dff478086167f8e3576c222
    n-003 -> n-001, 4 entry/entries moved, receipt 0f0c38f1662b4dcbb1b8f94c72b4fef2

====================================================================================================
9. EVERY RECEIPT, THEN THE FINAL GRAPH AND THE WHOLE LEDGER
====================================================================================================
  76 receipt(s), in emission order. The stream IS the audit:

      1. extract_accepted         subject=ep-251-01    in=d779c4250429 out=de8fc3842295
         12 claim(s) appended under motive 'engineering', 1 superseding, from 10 turn(s)
      2. graph_mutated            subject=n-001        in=44136fa355b3 out=b209ce345993
         created 'JedAI Gateway' as service
      3. graph_mutated            subject=n-002        in=44136fa355b3 out=b2fa07ee2311
         created 'JedAI models' as service
      4. graph_mutated            subject=n-003        in=44136fa355b3 out=963cadc22b2d
         created 'agent-memory MCP server' as service
      5. graph_mutated            subject=n-004        in=44136fa355b3 out=a1f48a89f97d
         created 'chart' as artifact
      6. graph_mutated            subject=n-005        in=44136fa355b3 out=9c7841880e28
         created 'session affinity' as attribute
      7. graph_mutated            subject=n-006        in=44136fa355b3 out=3bdd504c8079
         created 'C4' as service
      8. graph_mutated            subject=n-007        in=44136fa355b3 out=08e7168bdd7c
         created 'text-embedding-3' as model
      9. graph_mutated            subject=r-001        in=44136fa355b3 out=ae5adcfeb62b
         created FRONTS n-001 to n-002
     10. graph_mutated            subject=r-002        in=44136fa355b3 out=a98942cd228f
         created DEPLOYS n-004 to n-003
     11. reconcile_new            subject=n-001        in=1548e447d416 out=3000bc932798
         decision=new candidates_offered=0 names_in_group=2
     12. reconcile_new            subject=n-002        in=1548e447d416 out=22e9b9685ec8
         decision=new candidates_offered=0 names_in_group=1
     13. reconcile_new            subject=n-003        in=1548e447d416 out=3806bde7aaf9
         decision=new candidates_offered=0 names_in_group=3
     14. reconcile_new            subject=n-003        in=1548e447d416 out=8c46f276ef0e
         decision=new candidates_offered=0 names_in_group=3
     15. reconcile_new            subject=n-003        in=1548e447d416 out=4ea82572f627
         decision=new candidates_offered=0 names_in_group=3
     16. reconcile_new            subject=n-004        in=1548e447d416 out=f0b543e7de0e
         decision=new candidates_offered=0 names_in_group=1
     17. reconcile_new            subject=n-005        in=1548e447d416 out=4b85984c5fc6
         decision=new candidates_offered=0 names_in_group=1
     18. reconcile_new            subject=n-006        in=1548e447d416 out=880ce2bf7a54
         decision=new candidates_offered=0 names_in_group=1
     19. reconcile_new            subject=n-007        in=1548e447d416 out=1d80b12c40f6
         decision=new candidates_offered=0 names_in_group=1
     20. reconcile_new            subject=n-001        in=1548e447d416 out=654c7378cbbe
         decision=new candidates_offered=0 names_in_group=2
     21. graph_mutated            subject=n-006        in=3bdd504c8079 out=311de124bd67
         1 fact(s), type=service
     22. dream_node_applied       subject=n-006        in=75b7889b64b9 out=1a95a061d274
         1 fact(s), 0 relation(s), 0 retired, type=service: e-5c26ce857234420da3b55e04bdd73a73 supersedes e-b74cfc963f39456b8167f5a6316fd605; live fact records current tool count 31 post-#248 with old count 24 preserved in same fact for context
     23. graph_mutated            subject=n-001        in=b209ce345993 out=714a4c7a4c27
         5 fact(s), type=service
     24. graph_mutated            subject=r-001        in=ae5adcfeb62b out=5d0f8bbbd690
         reinforced FRONTS to evidence 1
     25. dream_node_applied       subject=n-001        in=c303797ef9e2 out=ed5cc1454430
         5 fact(s), 1 relation(s), 0 retired, type=service: Populated node from first-time claims: hard rules, is-definition, chat default, alias rule, and #245 Host-header fix history. Reinforced existing FRONTS relation r-001.
     26. graph_mutated            subject=n-002        in=b2fa07ee2311 out=29c3d18c99b6
         1 fact(s), type=service
     27. dream_node_applied       subject=n-002        in=fa6669788676 out=daf27c64e971
         1 fact(s), 0 relation(s), 0 retired, type=service: Single new claim establishes what JedAI models is: the model layer that JedAI Gateway fronts. No additional facts, rules, or relations are evidenced by the available entry.
     28. graph_mutated            subject=n-003        in=963cadc22b2d out=86dd6eb8d24a
         3 fact(s), type=service
     29. dream_node_applied       subject=n-003        in=1f53e7bea0b8 out=87c1fe0f22fb
         3 fact(s), 0 relation(s), 0 retired, type=service: First dream of this node; recorded deployment origin (#240), two bugs fixed by #245 (Host header rewrite and wrong image tag).
     30. graph_mutated            subject=n-004        in=a1f48a89f97d out=3cbbfb6b0eae
         1 fact(s), type=artifact
     31. graph_mutated            subject=r-002        in=a98942cd228f out=754dad06cce1
         reinforced DEPLOYS to evidence 1
     32. dream_node_applied       subject=n-004        in=4287b62d1366 out=f68834ca1106
         1 fact(s), 1 relation(s), 0 retired, type=artifact: first dream of this node; one attribute and one outbound DEPLOYS relation established from the single claim
     33. graph_mutated            subject=n-005        in=9c7841880e28 out=40934a82fa26
         2 fact(s), type=attribute
     34. dream_node_applied       subject=n-005        in=b222d0b2142d out=e2da0d9b7436
         2 fact(s), 0 relation(s), 0 retired, type=attribute: first dream of node; recorded intermittent affinity loss rate and issue #246 reference from the two new claims
     35. graph_mutated            subject=n-007        in=08e7168bdd7c out=e5712b184ffe
         2 fact(s), type=model
     36. dream_node_applied       subject=n-007        in=d9cb7209332b out=6312e126796d
         2 fact(s), 0 relation(s), 0 retired, type=model: first dream of this node; recorded embedding alias, dimension count, and explicit-selection rule from single claim
     37. extract_accepted         subject=ep-251-02    in=5107c2e4b899 out=011acccd07d6
         6 claim(s) appended under motive 'engineering', 1 superseding, from 4 turn(s)
     38. graph_mutated            subject=n-001        in=714a4c7a4c27 out=714a4c7a4c27
         aliases now ['LiteLLM proxy']
     39. graph_mutated            subject=n-002        in=29c3d18c99b6 out=29c3d18c99b6
         aliases now []
     40. graph_mutated            subject=n-003        in=86dd6eb8d24a out=86dd6eb8d24a
         aliases now ['agent-memory', 'agent-memory MCP']
     41. graph_mutated            subject=n-001        in=714a4c7a4c27 out=c55c4876df6f
         aliases now ['LiteLLM proxy', 'gateway']
     42. graph_mutated            subject=n-005        in=40934a82fa26 out=7f9d00ae5c8e
         aliases now ['session-affinity']
     43. graph_mutated            subject=r-001        in=5d0f8bbbd690 out=75b5be7bdcf6
         reinforced FRONTS to evidence 2
     44. reconcile_bound          subject=n-001        in=f78df483ffa3 out=a016a9c593d8
         decision=bind candidates_offered=7 names_in_group=2
     45. reconcile_bound          subject=n-002        in=f78df483ffa3 out=0fac7c648649
         decision=bind candidates_offered=7 names_in_group=1
     46. reconcile_bound          subject=n-003        in=f78df483ffa3 out=a7d5f5449ab1
         decision=bind candidates_offered=7 names_in_group=1
     47. reconcile_bound          subject=n-001        in=f78df483ffa3 out=220514561cc4
         decision=bind candidates_offered=7 names_in_group=2
     48. reconcile_bound          subject=n-005        in=f78df483ffa3 out=e57417942cb1
         decision=bind candidates_offered=7 names_in_group=1
     49. graph_mutated            subject=n-001        in=c55c4876df6f out=f851bf2c7757
         6 fact(s), type=service
     50. graph_mutated            subject=r-001        in=75b5be7bdcf6 out=6367190675af
         reinforced FRONTS to evidence 2
     51. dream_node_applied       subject=n-001        in=bff7be60e360 out=03ab371161ae
         6 fact(s), 1 relation(s), 0 retired, type=service: chat default rewritten from claude-haiku-4-5 to claude-sonnet-4-6 per superseding claim e-fe716a06788d4e80add2cf2afe6cfb5a; old value kept as superseded; all rules and existing facts reinforced with new evidence ids
     52. graph_mutated            subject=n-002        in=29c3d18c99b6 out=f97293a88395
         1 fact(s), type=service
     53. dream_node_applied       subject=n-002        in=7fce212a23e0 out=6896f1a99e09
         1 fact(s), 0 relation(s), 0 retired, type=service: New claim reinforces existing is-fact; no new facts or relations introduced.
     54. graph_mutated            subject=n-003        in=86dd6eb8d24a out=b57954befd95
         4 fact(s), type=service
     55. dream_node_applied       subject=n-003        in=0c62e2b2c55d out=921669465192
         4 fact(s), 0 relation(s), 0 retired, type=service: reinforced gateway-reachability fact with new claim e-c820b984fd6d4c67bbf774b625b32502; all existing facts retained
     56. graph_mutated            subject=n-005        in=7f9d00ae5c8e out=8ef411380e37
         2 fact(s), type=attribute
     57. dream_node_applied       subject=n-005        in=42d4b7fbb7cf out=0dbba0cafa9e
         2 fact(s), 0 relation(s), 0 retired, type=attribute: new claim reinforces existing unsure fact with same rate and non-reproducibility; merged entry ids; no new facts introduced
     58. read_emitted             subject=c8d76f4718e26fd2432920c50e6f13260d36232afed308670811b503672428ed in=c8d76f4718e2 out=8b33e0561b9f
         18 of 18 ranked facts from 7 node(s); dropped 2 duplicate fact(s); budget 100 lines / 1500 tokens; brief over 7 node(s) by value, no query
     59. read_emitted             subject=9fd12dd9f8c2bbcd2a1c1ff7d3d018e0f48dd567ca35ba3ccd13e1e5656c1480 in=9fd12dd9f8c2 out=925fb61395cb
         3 of 3 ranked facts from 2 node(s); budget 100 lines / 1500 tokens; seeds 2 of k=8 (1 exact, 5 below the kNN floor 0.25), neighbours 0
     60. read_emitted             subject=8ef6742dc2db405e892d9b4f72266a039c7c0c481c7f9e30fedce5e34c13d20e in=8ef6742dc2db out=1c0fcd20bdb1
         1 of 1 ranked facts from 1 node(s); budget 100 lines / 1500 tokens; seeds 1 of k=8 (1 exact, 6 below the kNN floor 0.25), neighbours 0
     61. read_emitted             subject=3fb25675830f5520303d2e4aff1510600e2aa07fc7c8c8a39617b3248d15796f in=3fb25675830f out=6418e68dff5f
         13 of 13 ranked facts from 3 node(s); dropped 2 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 3 of k=8 (1 exact, 4 below the kNN floor 0.25), neighbours 2
     62. graph_mutated            subject=DEPLOYS->FRONTS in=c433e25bd374 out=8b7daa100f1d
         renamed 1 relation(s) from DEPLOYS to FRONTS
     63. dream_edge_type_renamed  subject=DEPLOYS      in=7e054247a62e out=a07a3e77b3bb
         1 edge(s) re-labelled DEPLOYS to FRONTS: scope must keep at most 1 edge type; DEPLOYS (1 edge) is the least-used type and must be folded into FRONTS, the type that is staying
     64. graph_mutated            subject=n-001        in=53d6ac12fc14 out=6ebd6c7062b1
         n-007 absorbed into n-001 as 'JedAI Gateway'
     65. dream_merged             subject=n-001        in=45e0294b60c4 out=7ccca4846251
         n-007 absorbed into n-001 as 'JedAI Gateway': forced merge: text-embedding-3 (n-007) into JedAI Gateway (n-001) per slate; same turn x1
     66. graph_mutated            subject=n-003        in=6e7ef6687881 out=6c0c4b4878ce
         n-004 absorbed into n-003 as 'agent-memory MCP server'
     67. dream_merged             subject=n-003        in=45e0294b60c4 out=7ccca4846251
         n-004 absorbed into n-003 as 'agent-memory MCP server': forced merge: chart (n-004) into agent-memory MCP server (n-003) per slate; linked x1
     68. graph_mutated            subject=n-006        in=a71884b749cd out=d09912889139
         n-002 absorbed into n-006 as 'C4'
     69. dream_merged             subject=n-006        in=45e0294b60c4 out=7ccca4846251
         n-002 absorbed into n-006 as 'C4': forced merge: JedAI models (n-002) into C4 (n-006) per slate; nearest by embedding
     70. graph_mutated            subject=n-001        in=7337f0b21d64 out=79b0aa3e67f1
         n-003 absorbed into n-001 as 'JedAI Gateway'
     71. dream_merged             subject=n-001        in=f520a9d9a65a out=72009bf9bd62
         n-003 absorbed into n-001 as 'JedAI Gateway': forced merge: agent-memory MCP server linked x2 to JedAI Gateway; absorb its facts into the gateway node
     72. read_emitted             subject=a9f5edf722fdac9df49bd3dee3032fe1f360be3020b786e969f24f55b9d1b813 in=a9f5edf722fd out=ca3f0e1ddcfb
         5 of 5 ranked facts from 2 node(s); dropped 1 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 1 of k=8 (1 exact, 2 below the kNN floor 0.25), neighbours 1
     73. read_emitted             subject=877ac82440d08d86219cbde4be42a269aac4fde81f60ae553fddb51bfec13bc7 in=877ac82440d0 out=c35a58a9def0
         8 of 8 ranked facts from 1 node(s); dropped 1 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 1 of k=8 (1 exact, 2 below the kNN floor 0.25), neighbours 1
     74. read_emitted             subject=cd2a986cc36c0997956012101cae1df9a5a0bdefbdc1444ceb873619b9508b3a in=cd2a986cc36c out=3637c48409ac
         10 of 10 ranked facts from 2 node(s); dropped 1 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 2 of k=8 (1 exact, 1 below the kNN floor 0.25), neighbours 0
     75. read_emitted             subject=877ac82440d08d86219cbde4be42a269aac4fde81f60ae553fddb51bfec13bc7 in=877ac82440d0 out=c35a58a9def0
         8 of 8 ranked facts from 1 node(s); dropped 1 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 1 of k=8 (1 exact, 2 below the kNN floor 0.25), neighbours 1
     76. read_emitted             subject=cd2a986cc36c0997956012101cae1df9a5a0bdefbdc1444ceb873619b9508b3a in=cd2a986cc36c out=3637c48409ac
         10 of 10 ranked facts from 2 node(s); dropped 1 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 2 of k=8 (1 exact, 1 below the kNN floor 0.25), neighbours 0

  FINAL GRAPH: 3 node(s)
    C4 (service) [n-006] as of 2026-09-11, 4 entries, aka JedAI models   dirty=False reads=6 facts=2
      is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)
      attribute: returns 31 tools after #248; count was 24 before #248 (x2)
    JedAI Gateway (service) [n-001] as of 2026-09-11, 13 entries, aka LiteLLM proxy, gateway, text-embedding-3, agent-memory MCP server, agent-memory, agent-memory MCP, chart   dirty=False reads=7 facts=8
      rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
      rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
      is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)
      attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
      attribute: embedding alias text-embedding-3 at 3072 dimensions; must be selected explicitly, no default applied automatically
      attribute: agent-memory MCP server deployed via chart introduced in #240; #245 fixed wrong image tag (x2)
      attribute: agent-memory reaches gateway successfully since #245 fixed Host header rewrite (x2)
      superseded: chat default was claude-haiku-4-5 (x2)
      FRONTS [n-006]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)   [r-001]
    session affinity (attribute) [n-005] as of 2026-09-11, 3 entries, aka session-affinity   dirty=False reads=3 facts=2
      attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
      unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand (x2)
  RELATIONS: 1 edge(s) over 1 type(s), each carrying its own evidence
    r-001  n-001 (JedAI Gateway) -FRONTS-> n-006 (C4)  evidence=2 [cooccurrence says 2]
  EDGE TYPES: {'FRONTS': 1}

  EVERY NODE, AS AN AGENT RECEIVES IT -- the rendered block, verbatim:

  node(n-006)
  ------ rendered verbatim, exactly as an LLM reader receives it -------------
  | C4 (service) [n-006] as of 2026-09-11, 4 entries, aka JedAI models
  | is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)
  | attribute: returns 31 tools after #248; count was 24 before #248 (x2)
  | from JedAI Gateway [n-001] FRONTS: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)
  | more: explain(n-006), neighbors(n-006)
  ---------------------------------------------------------------------------

  node(n-001)
  ------ rendered verbatim, exactly as an LLM reader receives it -------------
  | JedAI Gateway (service) [n-001] as of 2026-09-11, 13 entries, aka LiteLLM proxy, gateway, text-embedding-3, agent-memory MCP server, agent-memory, agent-memory MCP, chart
  | rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)
  | rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)
  | is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)
  | attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
  | attribute: embedding alias text-embedding-3 at 3072 dimensions; must be selected explicitly, no default applied automatically
  | attribute: agent-memory MCP server deployed via chart introduced in #240; #245 fixed wrong image tag (x2)
  | attribute: agent-memory reaches gateway successfully since #245 fixed Host header rewrite (x2)
  | FRONTS C4 [n-006]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)
  | more: explain(n-001), neighbors(n-001)
  ---------------------------------------------------------------------------

  node(n-005)
  ------ rendered verbatim, exactly as an LLM reader receives it -------------
  | session affinity (attribute) [n-005] as of 2026-09-11, 3 entries, aka session-affinity
  | attribute: intermittent affinity loss is named and asserted (not claimed fixed) by probe in #246
  | unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand (x2)
  | more: explain(n-005), neighbors(n-005)
  ---------------------------------------------------------------------------

  EVERY RELATION -- 1 edge(s) over 1 type(s).
  A belief BETWEEN two concepts, with its own claim and its own evidence:
    r-001  [n-001] JedAI Gateway -FRONTS-> [n-006] C4
         claim   : JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself.
         evidence: 2 ['e-ca478da7f99146d9bb88abd46a1cb388', 'e-bef62a6485e348be8a083a8fd6e9ada7']

  EVERY DREAM EVENT -- 34 journalled mutation(s), oldest first.
  This is the history the replay proof above was computed from. One line each:
  the op, the nodes it touched, and what it did, summarised by explain.event_summary
  -- the same one-liner a node's own 'history:' section shows a reader:

      1. node_created           ['n-001']
         created 'JedAI Gateway' as service
      2. node_created           ['n-002']
         created 'JedAI models' as service
      3. node_created           ['n-003']
         created 'agent-memory MCP server' as service
      4. node_created           ['n-004']
         created 'chart' as artifact
      5. node_created           ['n-005']
         created 'session affinity' as attribute
      6. node_created           ['n-006']
         created 'C4' as service
      7. node_created           ['n-007']
         created 'text-embedding-3' as model
      8. relation_upserted      ['n-001', 'n-002']
         created FRONTS n-001 to n-002, evidence 1
      9. relation_upserted      ['n-004', 'n-003']
         created DEPLOYS n-004 to n-003, evidence 1
     10. node_rewritten         ['n-006']
         1 fact(s), type=service
     11. node_rewritten         ['n-001']
         5 fact(s), type=service
     12. relation_upserted      ['n-001', 'n-002']
         reinforced FRONTS n-001 to n-002, evidence 1
     13. node_rewritten         ['n-002']
         1 fact(s), type=service
     14. node_rewritten         ['n-003']
         3 fact(s), type=service
     15. node_rewritten         ['n-004']
         1 fact(s), type=artifact
     16. relation_upserted      ['n-004', 'n-003']
         reinforced DEPLOYS n-004 to n-003, evidence 1
     17. node_rewritten         ['n-005']
         2 fact(s), type=attribute
     18. node_rewritten         ['n-007']
         2 fact(s), type=model
     19. aliases_added          ['n-001']
         aliases now LiteLLM proxy
     20. aliases_added          ['n-002']
         aliases now 
     21. aliases_added          ['n-003']
         aliases now agent-memory, agent-memory MCP
     22. aliases_added          ['n-001']
         aliases now LiteLLM proxy, gateway
     23. aliases_added          ['n-005']
         aliases now session-affinity
     24. relation_upserted      ['n-001', 'n-002']
         reinforced FRONTS n-001 to n-002, evidence 2
     25. node_rewritten         ['n-001']
         6 fact(s), type=service
     26. relation_upserted      ['n-001', 'n-002']
         reinforced FRONTS n-001 to n-002, evidence 2
     27. node_rewritten         ['n-002']
         1 fact(s), type=service
     28. node_rewritten         ['n-003']
         4 fact(s), type=service
     29. node_rewritten         ['n-005']
         2 fact(s), type=attribute
     30. edge_type_renamed      []
         renamed DEPLOYS to FRONTS
     31. node_merged            ['n-001', 'n-007']
         absorbed n-007 as 'JedAI Gateway'
     32. node_merged            ['n-003', 'n-004']
         absorbed n-004 as 'agent-memory MCP server'
     33. node_merged            ['n-006', 'n-002']
         absorbed n-002 as 'C4'
     34. node_merged            ['n-001', 'n-003']
         absorbed n-003 as 'JedAI Gateway'

  THE WHOLE LEDGER — 18 entry/entries over ep-251-01 and ep-251-02,
  append-only, unbounded, and the thing the graph is fully regenerable from:

    e-fc9b2270fe5a47ec9e9297143a82e12f  rule           mode=directive    conf=0.99  turns=[1]
      claim      : Every real run goes through the JedAI Gateway; no hermetic mode, no local stub, not even for a quick check.
      subjects   : ['JedAI Gateway']
      node_ids   : ['n-001']

    e-ca478da7f99146d9bb88abd46a1cb388  is             mode=report       conf=0.99  turns=[2]
      claim      : JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself.
      subjects   : ['JedAI Gateway']
      objects    : ['JedAI models']
      node_ids   : ['n-001', 'n-006']

    e-9590178378db4b689615facabd2118a8  attribute      mode=report       conf=0.97  turns=[3]
      claim      : Before #245 the gateway refused the Host header outright, so the agent-memory MCP server could not reach it; #245 fixed the Host rewrite.
      subjects   : ['JedAI Gateway', 'agent-memory MCP server']
      identifiers: ['#245']
      node_ids   : ['n-001']

    e-1b9152680510459f8b49f69f7310b2ab  attribute      mode=report       conf=0.97  turns=[4]
      claim      : #245 also fixed the chart deploying agent-memory from the wrong image tag.
      subjects   : ['agent-memory']
      identifiers: ['#245']
      node_ids   : ['n-001']

    e-cdb046a300714229a19c3732313a9ce5  attribute      mode=report       conf=0.97  turns=[4]
      claim      : The chart is where the agent-memory MCP deploy lives; that came in with #240.
      subjects   : ['agent-memory MCP']
      objects    : ['chart']
      identifiers: ['#240']
      node_ids   : ['n-001']

    e-8695547b60be4a428f7fcda5e437405c  unsure         mode=report       conf=0.90  turns=[5]
      claim      : Roughly one call in twenty loses session affinity and lands on a cold pod; never reproduced on demand.
      subjects   : ['session affinity']
      node_ids   : ['n-005']

    e-e91daa0551b44aa988580c356131715d  attribute      mode=report       conf=0.97  turns=[6]
      claim      : The intermittent affinity loss is named and asserted (not claimed fixed) by the probe in #246.
      subjects   : ['session affinity']
      identifiers: ['#246']
      node_ids   : ['n-005']

    e-b74cfc963f39456b8167f5a6316fd605  attribute      mode=report       conf=0.85  turns=[7]
      claim      : C4 is returning 24 tools.
      subjects   : ['C4']
      identifiers: ['24']
      node_ids   : ['n-006']

    e-86cc728cfda342fda1385e2d60a4d3ff  attribute      mode=report       conf=0.99  turns=[8]
      claim      : Chat default on the gateway is claude-haiku-4-5.
      subjects   : ['JedAI Gateway']
      identifiers: ['claude-haiku-4-5']
      node_ids   : ['n-001']

    e-6300eb1412aa47acb7362a17d130c1f3  attribute      mode=report       conf=0.99  turns=[8]
      claim      : Embedding alias is text-embedding-3 at 3072 dimensions; must be selected explicitly, no default embedding model is applied automatically.
      subjects   : ['text-embedding-3']
      identifiers: ['text-embedding-3', '3072']
      node_ids   : ['n-001']

    e-5c26ce857234420da3b55e04bdd73a73  attribute      mode=correction   conf=0.97  turns=[9]
      claim      : C4 memory server returns 31 tools after #248, not 24; 24 was the count before #248.
      subjects   : ['C4']
      identifiers: ['31', '#248', '24']
      supersedes : e-b74cfc963f39456b8167f5a6316fd605   <-- the in-episode correction, resolved to its sibling
      node_ids   : ['n-006']

    e-0976177bcc1d4505a3209847885a4a15  rule           mode=directive    conf=0.99  turns=[10]
      claim      : Rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-haiku-4-5, never a dated pin); a dated alias goes stale and the gateway starts refusing it.
      subjects   : ['LiteLLM proxy']
      identifiers: ['claude-haiku-4-5']
      node_ids   : ['n-001']

    e-bef62a6485e348be8a083a8fd6e9ada7  is             mode=report       conf=0.99  turns=[1]
      claim      : JedAI Gateway is the LiteLLM proxy in front of the JedAI models, not a model itself.
      subjects   : ['JedAI Gateway']
      objects    : ['JedAI models']
      node_ids   : ['n-001', 'n-006']

    e-d0b2ca5e9cfb4264a7b5caea7496cfc4  rule           mode=directive    conf=0.99  turns=[1]
      claim      : Every real run goes through the JedAI Gateway; no exceptions stated.
      subjects   : ['JedAI Gateway']
      node_ids   : ['n-001']

    e-c820b984fd6d4c67bbf774b625b32502  attribute      mode=report       conf=0.97  turns=[2]
      claim      : agent-memory MCP server reaches the gateway successfully since #245 fixed the Host header rewrite.
      subjects   : ['agent-memory MCP server', 'JedAI Gateway']
      identifiers: ['#245']
      node_ids   : ['n-001']

    e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687  attribute      mode=report       conf=0.95  turns=[3]
      claim      : chat default on the gateway was claude-haiku-4-5.
      subjects   : ['gateway']
      identifiers: ['claude-haiku-4-5']
      node_ids   : ['n-001']

    e-fe716a06788d4e80add2cf2afe6cfb5a  attribute      mode=correction   conf=0.97  turns=[3]
      claim      : chat default on the gateway moved to claude-sonnet-4-6 as of this morning; claude-haiku-4-5 no longer the default but still resolves.
      subjects   : ['gateway']
      identifiers: ['claude-sonnet-4-6', 'claude-haiku-4-5']
      supersedes : e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687   <-- the in-episode correction, resolved to its sibling
      node_ids   : ['n-001']

    e-a852e1bc1cf64e4daaa6f9d211ef41d2  unsure         mode=report       conf=0.93  turns=[4]
      claim      : session-affinity flake hits approximately one in twenty calls under load test; not reproducible on demand.
      subjects   : ['session-affinity']
      node_ids   : ['n-005']


====================================================================================================
THE ASSERTIONS — a failure here is a finding about a PROMPT, not a flaky test
====================================================================================================
  [1/21] 12 claims extracted, 1 with supersedes set.  PASS
  [2/21] 'the gateway': ['JedAI Gateway', 'LiteLLM proxy'] -> one node id n-001.  PASS
  [2/21] 'the agent-memory server': ['agent-memory', 'agent-memory MCP', 'agent-memory MCP server'] -> one node id n-003.  PASS

  FINDING — the design's second claimed convergence, NOT asserted:
    no surface name named the C4 memory server at all this run.
  [3/21] 2 rule(s) survived compression, 2 carrying the 'rule' kind; the gateway read emits 2.  PASS
        rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check
        rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it
  [4/21] 1 relation(s) over 1 type(s), every endpoint real and every edge evidenced.  PASS
  [5/21] superseded e-b74cfc963f39456b8167f5a6316fd605 reachable via ledger.for_node['n-006'], repeated on no node's lines.  PASS
        superseded: C4 is returning 24 tools.
        surviving : C4 memory server returns 31 tools after #248, not 24; 24 was the count before #248.
  [6/21] 4 read(s): exactly-named nodes lead and the rules are first inside every block; brief() has no exact hits and kept both rules in 33 rendered line(s).  PASS
        hermetic
        dated
  [7/21] read('#246'): indexed on ['n-005'], 1 exact seed(s) [('n-005', 'identifier')].  PASS
  [8/21] read('C4') reaches ['C4'] (holds '31') via ['alias'].  PASS
        'C4' is a recorded alias of C4 (n-006) -- an EXACT hit.
  [9/21] 4 distinct block order(s) from 4 read(s) over a 7-node scope (query read(s) that excluded part of it: ['b', 'c', 'd']).  PASS
        a: 7 of 7 -> ['JedAI Gateway', 'text-embedding-3', 'JedAI models', 'agent-memory MCP server', 'chart', 'C4', 'session affinity']
           the whole scope (brief has no read_k; a query read here is read_k + 1-hop wide)
        b: 2 of 7 -> ['session affinity', 'C4']
           not emitted: ['JedAI Gateway', 'JedAI models', 'agent-memory MCP server', 'chart', 'text-embedding-3']
        c: 1 of 7 -> ['C4']
           not emitted: ['JedAI Gateway', 'JedAI models', 'agent-memory MCP server', 'chart', 'session affinity', 'text-embedding-3']
        d: 3 of 7 -> ['JedAI Gateway', 'agent-memory MCP server', 'session affinity']
           not emitted: ['C4', 'JedAI models', 'chart', 'text-embedding-3']
  [10/21] read('C4') leads with C4 (n-006), an exact hit carrying 'C4', and emits 1 of 7 node(s).  PASS
        not emitted: ['JedAI Gateway', 'JedAI models', 'agent-memory MCP server', 'chart', 'session affinity', 'text-embedding-3']
        dropped by the floor: n-007 at sim=0.1566
        dropped by the floor: n-004 at sim=0.1536
        dropped by the floor: n-003 at sim=0.1295
        dropped by the floor: n-001 at sim=0.1142
        dropped by the floor: n-005 at sim=0.1031
        dropped by the floor: n-002 at sim=0.0918
  [11/21] read('#246') leads with session affinity (n-005), an exact hit of 1.  PASS
        block order: ['session affinity', 'C4']
  [12/21] 7 read(s) receipted; 5 folded a duplicate and each says so in its READ_EMITTED detail.  PASS
        (none — brief() takes no query): 18 of 18 ranked facts from 7 node(s); dropped 2 duplicate fact(s); budget 100 lines / 1500 tokens; brief over 7 node(s) by value, no query
        the gateway refuses my Host header: 13 of 13 ranked facts from 3 node(s); dropped 2 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 3 of k=8 (1 exact, 4 below the kNN floor 0.25), neighbours 2
        C4: 5 of 5 ranked facts from 2 node(s); dropped 1 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 1 of k=8 (1 exact, 2 below the kNN floor 0.25), neighbours 1
        what do I know about the gateway: 8 of 8 ranked facts from 1 node(s); dropped 1 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 1 of k=8 (1 exact, 2 below the kNN floor 0.25), neighbours 1
        how many tools does the C4 memory server return: 10 of 10 ranked facts from 2 node(s); dropped 1 duplicate fact(s); budget 100 lines / 1500 tokens; seeds 2 of k=8 (1 exact, 1 below the kNN floor 0.25), neighbours 0
  [13/21] 4 MUST MERGE mandate(s), each naming its evidence.  PASS
        MUST MERGE: 'text-embedding-3' (n-007) INTO 'JedAI Gateway' (n-001) because same turn x1
        MUST MERGE: 'chart' (n-004) INTO 'agent-memory MCP server' (n-003) because linked x1
        MUST MERGE: 'JedAI models' (n-002) INTO 'C4' (n-006) because nearest by embedding
        MUST MERGE: 'agent-memory MCP server' (n-003) INTO 'JedAI Gateway' (n-001) because linked x2
  [14/21] after the squeeze read('C4') seeds [('n-006', 'alias')] and lands on ['C4'], which holds '31'.  PASS
        aka JedAI models
  [15/21] 15 render(s) and 19 prompt(s) carry none of the four characters the design removed.  PASS
        non-ASCII characters present at all (model-authored text only): ['—']
  [16/21] every one of 14 render(s) about concepts carries a node id; 7 distinct.  PASS
        not asked for one: replay(scope) -- about a scope and its journal, not a node
  [17/21] 1 relation(s), every type well formed, 1 type(s) within M=1, 1 edge(s) re-labelled.  PASS
        r-002: DEPLOYS -> FRONTS   claim='chart is where the agent-memory MCP deploy lives; came in with #240'   evidence=['e-cdb046a300714229a19c3732313a9ce5']
  [18/21] 6 record(s) gained evidence across the two episodes; 10 fact(s)/edge(s) now carry more than one entry.  PASS
        edge n-001 FRONTS n-002: evidence 1 -> 2
        n-001 fact LiteLLM proxy fronting the JedAI models, not a model itself: evidence 1 -> 2
        n-001 fact before #245 gateway refused Host header outright; #245 fixed the Host rewrite so agent-memory MCP server could reach it: evidence 1 -> 2
        n-001 fact real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check: evidence 1 -> 2
        n-002 fact collection of LiteLLM-routed models sitting behind JedAI Gateway: evidence 1 -> 2
        n-005 fact roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand: evidence 1 -> 2
        n-006 is: collection of LiteLLM-routed models sitting behind JedAI Gateway (x2)   ['e-ca478da7f99146d9bb88abd46a1cb388', 'e-bef62a6485e348be8a083a8fd6e9ada7']
        n-006 attribute: returns 31 tools after #248; count was 24 before #248 (x2)   ['e-5c26ce857234420da3b55e04bdd73a73', 'e-b74cfc963f39456b8167f5a6316fd605']
        n-001 rule: real runs always go through the gateway, never hermetic and never a local stub, not even for a quick check (x2)   ['e-fc9b2270fe5a47ec9e9297143a82e12f', 'e-d0b2ca5e9cfb4264a7b5caea7496cfc4']
        n-001 rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-sonnet-4-6, never a dated pin); dated alias goes stale and gateway starts refusing it (x2)   ['e-0976177bcc1d4505a3209847885a4a15', 'e-fe716a06788d4e80add2cf2afe6cfb5a']
        n-001 is: LiteLLM proxy fronting the JedAI models, not a model itself (x2)   ['e-ca478da7f99146d9bb88abd46a1cb388', 'e-bef62a6485e348be8a083a8fd6e9ada7']
        n-001 attribute: agent-memory MCP server deployed via chart introduced in #240; #245 fixed wrong image tag (x2)   ['e-cdb046a300714229a19c3732313a9ce5', 'e-1b9152680510459f8b49f69f7310b2ab']
        n-001 attribute: agent-memory reaches gateway successfully since #245 fixed Host header rewrite (x2)   ['e-9590178378db4b689615facabd2118a8', 'e-c820b984fd6d4c67bbf774b625b32502']
        n-001 superseded: chat default was claude-haiku-4-5 (x2)   ['e-86cc728cfda342fda1385e2d60a4d3ff', 'e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687']
        n-005 unsure: roughly 1 call in 20 loses session affinity and lands on a cold pod; never reproduced on demand (x2)   ['e-8695547b60be4a428f7fcda5e437405c', 'e-a852e1bc1cf64e4daaa6f9d211ef41d2']
        r-001 FRONTS [n-006]: JedAI Gateway is a LiteLLM proxy sitting in front of the JedAI models; it routes to them, it is not a model itself. (x2)   ['e-ca478da7f99146d9bb88abd46a1cb388', 'e-bef62a6485e348be8a083a8fd6e9ada7']
  [19/21] the chat default reads 'claude-sonnet-4-6' and the superseded value survives in explain.  PASS
        current : attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves
        record  : 2026-09-11 attribute: chat default is claude-sonnet-4-6; claude-haiku-4-5 no longer default but still resolves [e-fe716a06788d4e80add2cf2afe6cfb5a]
        record  : 2026-09-11 superseded: chat default was claude-haiku-4-5 (x2) [e-86cc728cfda342fda1385e2d60a4d3ff, e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687]
        record  : e-fe716a06788d4e80add2cf2afe6cfb5a | 2026-09-11 | attribute | chat default on the gateway moved to claude-sonnet-4-6 as of this morning; claude-haiku-4-5 no longer the default but still resolves. | supersedes e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687
        record  : e-b2a9ecf9f5aa4c4eaf9c25d3cc7f3687 | 2026-09-11 | attribute | chat default on the gateway was claude-haiku-4-5. | SUPERSEDED by e-fe716a06788d4e80add2cf2afe6cfb5a
        record  : e-0976177bcc1d4505a3209847885a4a15 | 2026-09-11 | rule | Rule: only ever name undated aliases on the LiteLLM proxy (e.g. claude-haiku-4-5, never a dated pin); a dated alias goes stale and the gateway starts refusing it.
        record  : e-86cc728cfda342fda1385e2d60a4d3ff | 2026-09-11 | attribute | Chat default on the gateway is claude-haiku-4-5.
  [20/21] 34 journalled event(s) replay to the live digest 1e8a0616df85286f.  PASS
  [21/21] 19 live call(s) across 4 stage(s), every stage reached.  PASS
        EXTRACT       2 call(s)
        RECONCILE     2 call(s)
        DREAM-NODE   11 call(s)
        DREAM-GLOBAL  4 call(s)

  ALL ASSERTIONS PASSED.

  TOTAL LIVE CHAT CALLS: 19
    EXTRACT       2 call(s),   0.73s total
    RECONCILE     2 call(s),   5.99s total
    DREAM-NODE   11 call(s),  45.03s total
    DREAM-GLOBAL  4 call(s),  26.60s total
    READ          0 call(s) — deterministic by design
```
