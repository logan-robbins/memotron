// Pure view-transform math for the Memory Explorer graph canvas.
//
// The semantics deliberately mirror d3-zoom -- the interaction grammar every
// well-loved graph explorer (Neo4j Browser, Obsidian graph view, Cytoscape.js)
// shares:
//   * the view is a single `translate(x, y) scale(k)` on the content group;
//   * panning translates the content 1:1 WITH the pointer;
//   * zooming about an anchor keeps the graph point under that anchor fixed;
//   * "fit" scales the content bounding box into the viewport with padding.
// Keeping the math here, free of React and the DOM, makes every formula
// directly unit-testable.

export interface ViewTransform {
  x: number;
  y: number;
  k: number;
}

export interface Point {
  x: number;
  y: number;
}

export interface Size {
  width: number;
  height: number;
}

export interface Bounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export const MIN_ZOOM = 0.2;
export const MAX_ZOOM = 8;
export const IDENTITY_VIEW: ViewTransform = { x: 0, y: 0, k: 1 };

export function clampScale(k: number, min = MIN_ZOOM, max = MAX_ZOOM): number {
  return Math.min(max, Math.max(min, k));
}

// d3-zoom `transform.invert`: a viewport point -> the graph point under it.
export function toGraphPoint(view: ViewTransform, point: Point): Point {
  return { x: (point.x - view.x) / view.k, y: (point.y - view.y) / view.k };
}

// d3-zoom `transform.apply`: a graph point -> where it lands in the viewport.
export function toViewportPoint(view: ViewTransform, point: Point): Point {
  return { x: point.x * view.k + view.x, y: point.y * view.k + view.y };
}

// d3-zoom `translateBy`: content follows the pointer 1:1 in viewport units.
export function panBy(view: ViewTransform, dx: number, dy: number): ViewTransform {
  return { ...view, x: view.x + dx, y: view.y + dy };
}

// d3-zoom `scaleBy` about an anchor: the graph point under the anchor before
// the scale change is still under it afterwards.
export function zoomAround(
  view: ViewTransform,
  factor: number,
  anchor: Point,
  min = MIN_ZOOM,
  max = MAX_ZOOM
): ViewTransform {
  const k = clampScale(view.k * factor, min, max);
  if (k === view.k) return view;
  const graphAnchor = toGraphPoint(view, anchor);
  return { k, x: anchor.x - graphAnchor.x * k, y: anchor.y - graphAnchor.y * k };
}

// d3-zoom's wheelDelta: ~0.002 per pixel, ~0.05 per line, 1 per page, and a
// pinch gesture (trackpads report it as ctrl+wheel) zooms 10x faster.  A
// negative deltaY (wheel up) yields a factor > 1: zoom in.
export function wheelZoomFactor(event: { deltaY: number; deltaMode: number; ctrlKey: boolean }): number {
  const unit = event.deltaMode === 1 ? 0.05 : event.deltaMode ? 1 : 0.002;
  return Math.pow(2, -event.deltaY * unit * (event.ctrlKey ? 10 : 1));
}

// Bounding box of node centers, grown by `margin` so node circles and a little
// breathing room count as content.  Null when there is nothing to bound.
export function contentBounds(points: ReadonlyArray<Point>, margin = 24): Bounds | null {
  if (!points.length) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const point of points) {
    if (point.x < minX) minX = point.x;
    if (point.y < minY) minY = point.y;
    if (point.x > maxX) maxX = point.x;
    if (point.y > maxY) maxY = point.y;
  }
  return { minX: minX - margin, minY: minY - margin, maxX: maxX + margin, maxY: maxY + margin };
}

// The "fit" every real explorer ships (d3 fitExtent, cytoscape.fit, Neo4j
// zoom-to-fit): scale the content bounds to the viewport with padding and
// center them.  Never a bare reset to k=1.
export function fitToBounds(
  bounds: Bounds,
  viewport: Size,
  padding = 48,
  min = MIN_ZOOM,
  max = MAX_ZOOM
): ViewTransform {
  const pad = Math.min(padding, Math.min(viewport.width, viewport.height) / 4);
  const width = Math.max(bounds.maxX - bounds.minX, 1e-6);
  const height = Math.max(bounds.maxY - bounds.minY, 1e-6);
  const k = clampScale(
    Math.min((viewport.width - pad * 2) / width, (viewport.height - pad * 2) / height),
    min,
    max
  );
  return {
    k,
    x: viewport.width / 2 - ((bounds.minX + bounds.maxX) / 2) * k,
    y: viewport.height / 2 - ((bounds.minY + bounds.maxY) / 2) * k
  };
}

// Undirected adjacency from the edge list, for hover highlighting and the
// ego-network focus mode.  Every endpoint gets an entry, even if isolated
// from everything else in the set.
export function buildAdjacency(
  edges: ReadonlyArray<{ source_id: string; target_id: string }>
): Map<string, Set<string>> {
  const adjacency = new Map<string, Set<string>>();
  for (const edge of edges) {
    let source = adjacency.get(edge.source_id);
    if (!source) {
      source = new Set();
      adjacency.set(edge.source_id, source);
    }
    let target = adjacency.get(edge.target_id);
    if (!target) {
      target = new Set();
      adjacency.set(edge.target_id, target);
    }
    source.add(edge.target_id);
    target.add(edge.source_id);
  }
  return adjacency;
}

export const MIN_NODE_RADIUS = 6;
export const MAX_NODE_RADIUS = 22;

/** Distinct neighbors per node — the degree that drives node size. */
export function degreeMap(
  adjacency: ReadonlyMap<string, ReadonlySet<string>>
): Map<string, number> {
  const degrees = new Map<string, number>();
  for (const [id, neighbors] of adjacency) degrees.set(id, neighbors.size);
  return degrees;
}

/**
 * Radius encoding degree, scaled by AREA rather than radius.
 *
 * Perceived magnitude of a disc tracks its area, so a linear radius ramp
 * exaggerates hubs badly (a 10x-degree node would read as 100x). Taking the
 * square root makes area proportional to degree — the convention Gephi,
 * Cytoscape and Obsidian all use for degree sizing.
 *
 * `maxDegree` normalizes against the busiest node in the current view, so the
 * ramp uses its full range whether the scope holds six facts or six hundred.
 */
export function nodeRadius(
  degree: number,
  maxDegree: number,
  min = MIN_NODE_RADIUS,
  max = MAX_NODE_RADIUS
): number {
  if (maxDegree <= 0) return min;
  const ratio = Math.sqrt(Math.max(0, Math.min(degree, maxDegree)) / maxDegree);
  return min + (max - min) * ratio;
}

/**
 * Ids of the highest-degree nodes — the hubs worth labelling even when the
 * view is too dense for every label (Obsidian labels big nodes first).
 * Ties break on id so the set is stable across renders.
 */
export function hubIds(degrees: ReadonlyMap<string, number>, count: number): Set<string> {
  return new Set(
    [...degrees.entries()]
      .filter(([, degree]) => degree > 0)
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, Math.max(0, count))
      .map(([id]) => id)
  );
}

// Ego network: every node within `depth` hops of `start`, start included.
export function neighborhood(
  adjacency: ReadonlyMap<string, ReadonlySet<string>>,
  start: string,
  depth: number
): Set<string> {
  const seen = new Set([start]);
  let frontier = [start];
  for (let hop = 0; hop < depth && frontier.length; hop += 1) {
    const next: string[] = [];
    for (const id of frontier) {
      for (const neighbor of adjacency.get(id) ?? []) {
        if (!seen.has(neighbor)) {
          seen.add(neighbor);
          next.push(neighbor);
        }
      }
    }
    frontier = next;
  }
  return seen;
}

// ---------------------------------------------------------------------------
// Labels, colour and captions -- Neo4j Browser's "colour by label" grammar.
//
// Neo4j Browser colours a node by its LABEL, keeps that colour stable across
// renders, and lists every label with a count in the legend / database
// information drawer ("node and relationship counts, displayed in
// parentheses").  Clicking a legend entry selects that label.
// ---------------------------------------------------------------------------

/** The shape of a graph node this module needs.  Structural, so tests can pass
 *  literals and the real `GraphNode` from types.ts satisfies it. */
export interface LabelledNode {
  id: string;
  label: string;
  node_type: string;
  labels?: ReadonlyArray<string>;
  memory_type?: string | null;
  relationship_type?: string | null;
  properties?: Record<string, unknown>;
}

export const ENTITY_FALLBACK_LABEL = 'Entity';
export const MEMORY_FALLBACK_LABEL = 'Memory';

/**
 * The label set a node is coloured and legended by.
 *
 * The server populates `labels` only for entity nodes (real graph labels, e.g.
 * `["Entity", "Customer"]` -- and Neo4j nodes genuinely carry several, so this
 * returns a list, not one string).  Memory/fact nodes come back with `labels`
 * empty but carry the field an operator actually reasons about: the memory type
 * (`decision`, `rollup`, `anchor`, ...), with the relationship type
 * (`DECIDED`, `REQUIRES`) as the next best thing.  Deriving the label instead
 * of hardcoding a map is what keeps the view correct when the vocabulary is
 * renamed -- `identity`->`anchor` and `theme`->`rollup` already happened once
 * and left a hardcoded palette stranded.
 */
export function nodeLabels(node: LabelledNode): string[] {
  const declared = (node.labels ?? []).filter((label) => label.trim().length > 0);
  if (declared.length) return [...declared];
  const derived = node.memory_type?.trim() || node.relationship_type?.trim();
  if (derived) return [derived];
  return [node.node_type === 'entity' ? ENTITY_FALLBACK_LABEL : MEMORY_FALLBACK_LABEL];
}

export interface Tally {
  name: string;
  count: number;
}

/** Descending by count, then ascending by name so the order never flickers. */
function tally(counts: ReadonlyMap<string, number>): Tally[] {
  return [...counts.entries()]
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}

/** Every label in the view with its node count -- the legend's rows.  A node
 *  with several labels counts once under each, exactly as Neo4j's drawer does. */
export function labelCounts(nodes: ReadonlyArray<LabelledNode>): Tally[] {
  const counts = new Map<string, number>();
  for (const node of nodes) {
    for (const label of nodeLabels(node)) {
      counts.set(label, (counts.get(label) ?? 0) + 1);
    }
  }
  return tally(counts);
}

/** The one label a node is COLOURED by -- `nodeColor` already reads only
 *  `nodeLabels(node)[0]`, on the strength of the server's own convention
 *  (`(entity_type_label, scope_kind_title)`, e.g. `["Resource", "Tenant"]` --
 *  every entity-node label tuple across dreaming.py/migration.py puts the
 *  real type first and the scope-kind marker second). `labelCounts` fans a
 *  node out across every declared label, Neo4j-drawer style, which is right
 *  for the Inspector's chip list but wrong for a legend a "node KINDS" panel
 *  promises: it would list "Tenant" as if it were an entity type alongside
 *  "Resource"/"Component". Counting only the colour label keeps the legend's
 *  rows exactly the kinds the canvas actually draws in different colours. */
export function primaryLabelCounts(nodes: ReadonlyArray<LabelledNode>): Tally[] {
  const counts = new Map<string, number>();
  for (const node of nodes) {
    const label = nodeLabels(node)[0];
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return tally(counts);
}

/** Every relationship type in the view with its edge count. */
export function relationshipTypeCounts(
  edges: ReadonlyArray<{ edge_type: string }>
): Tally[] {
  const counts = new Map<string, number>();
  for (const edge of edges) {
    counts.set(edge.edge_type, (counts.get(edge.edge_type) ?? 0) + 1);
  }
  return tally(counts);
}

/**
 * Property keys present on the nodes in view, with how many nodes carry each.
 *
 * Vector columns are dropped: a 256-float `name_embedding` is not a property an
 * operator inspects, and listing it pushes the real keys off the panel.
 */
export const HIDDEN_PROPERTY_KEY_PATTERN = /(^|_)embedding($|_)/;

export function isInspectableProperty(key: string, value: unknown): boolean {
  if (HIDDEN_PROPERTY_KEY_PATTERN.test(key)) return false;
  // Any long numeric vector is a model artefact, whatever it is called.
  return !(Array.isArray(value) && value.length > 12 && value.every((item) => typeof item === 'number'));
}

export function propertyKeyCounts(nodes: ReadonlyArray<LabelledNode>): Tally[] {
  const counts = new Map<string, number>();
  for (const node of nodes) {
    for (const [key, value] of Object.entries(node.properties ?? {})) {
      if (!isInspectableProperty(key, value)) continue;
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
  }
  return tally(counts);
}

/**
 * Categorical palette for label colouring.
 *
 * Hues extend the app's existing memory-type palette so the graph reads as part
 * of the same product rather than a bolted-on widget, spaced around the wheel
 * so adjacent legend rows stay separable, and dark enough that white node text
 * clears WCAG AA on every one.
 */
export const LABEL_PALETTE: readonly string[] = [
  '#2764c4',
  '#15835f',
  '#7c52c4',
  '#b56a13',
  '#087fa3',
  '#aa3f88',
  '#c74252',
  '#8c7812',
  '#3f6ea8',
  '#4a8c2f',
  '#a8455f',
  '#5d5fc4'
];

/** FNV-1a over the label: the same label gets the same colour in every render,
 *  in every scope, without a registry to keep in sync. */
export function labelColorIndex(label: string, paletteSize = LABEL_PALETTE.length): number {
  if (paletteSize <= 0) return 0;
  let hash = 0x811c9dc5;
  for (let index = 0; index < label.length; index += 1) {
    hash ^= label.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash % paletteSize;
}

export function labelColor(label: string, palette: readonly string[] = LABEL_PALETTE): string {
  return palette[labelColorIndex(label, palette.length)];
}

/** A node's colour is its first label's colour -- Neo4j paints a multi-label
 *  node with one label's style, not a blend. */
export function nodeColor(node: LabelledNode, palette: readonly string[] = LABEL_PALETTE): string {
  return labelColor(nodeLabels(node)[0], palette);
}

/**
 * The node's on-canvas caption.
 *
 * Neo4j Browser "auto-selects a property from the property list to use as a
 * caption".  `node.label` is already the server's chosen human field AND the
 * only decrypted one -- `properties.name` holds the raw (possibly encrypted)
 * value for a sealed scope, so reading the property directly would render
 * ciphertext.  Hence: label first, property fallbacks only when it is blank.
 */
export const CAPTION_PROPERTY_KEYS: readonly string[] = ['name', 'title', 'fact', 'summary', 'key'];

export function nodeCaption(node: LabelledNode, maxLength = 34): string {
  let caption = node.label?.trim() ?? '';
  if (!caption) {
    for (const key of CAPTION_PROPERTY_KEYS) {
      const value = node.properties?.[key];
      if (typeof value === 'string' && value.trim()) {
        caption = value.trim();
        break;
      }
    }
  }
  if (!caption) caption = node.id;
  if (maxLength > 1 && caption.length > maxLength) return `${caption.slice(0, maxLength - 1)}…`;
  return caption;
}

// ---------------------------------------------------------------------------
// Force-directed layout.
//
// Neo4j Browser lays its result graph out with a live d3-force simulation:
// links behave as springs, nodes repel, the whole thing is pulled to centre,
// circles refuse to overlap, and a node you drag is PINNED where you drop it.
// The same four forces are implemented here directly rather than pulling in
// d3-force, for two reasons: every formula stays unit-testable without a DOM,
// and the step is fully deterministic (d3 jitters coincident nodes with
// Math.random, which would make layout untestable and renders irreproducible).
// ---------------------------------------------------------------------------

export interface SimNode {
  id: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
  /** Pinned coordinates (d3's `fx`/`fy`).  Non-null == dropped here on purpose:
   *  forces still act on neighbours but this node does not move. */
  fx: number | null;
  fy: number | null;
  radius: number;
}

export interface SimLink {
  source: string;
  target: string;
}

export interface ForceParams {
  /** Spring rest length between two linked nodes. */
  linkDistance: number;
  /** 0..1 -- how hard a link pulls toward `linkDistance`. */
  linkStrength: number;
  /** Negative == repulsion (d3's manyBody convention). */
  chargeStrength: number;
  /** Repulsion is ignored past this range; keeps big graphs from imploding. */
  chargeMaxDistance: number;
  centerX: number;
  centerY: number;
  /** 0..1 -- gravity toward the centre.  Also what keeps disconnected islands
   *  (this graph is full of them) from drifting off-canvas forever. */
  centerStrength: number;
  /** 0..1 -- fraction of velocity shed each tick (d3 default 0.4). */
  velocityDecay: number;
  /** Extra clearance beyond the two radii before collision pushes. */
  collidePadding: number;
}

/**
 * Two constraints fix these numbers, and both are easy to get wrong.
 *
 * 1. `linkDistance` has to clear about 2.5x the largest node radius, or a spring
 *    at rest leaves two hub circles touching and their captions overlapping.
 *    With MAX_NODE_RADIUS at 22 that puts the floor near 110.
 *
 * 2. `centerStrength` has to stay TINY, and it — not `linkDistance` — is what
 *    actually sets the layout's overall size. Unlike d3's `forceCenter`, which
 *    rigidly translates the centroid and so cannot affect spacing, this is a
 *    spring, and a spring competes with repulsion: pull grows linearly with
 *    radius while repulsion falls off as 1/r^2, so together they pin the layout
 *    radius at roughly cbrt(|charge| * n / (2 * centerStrength)). At 0.05 that
 *    was ~70 units for a 19-node graph — a ball too small for any caption to fit
 *    beside a node, whatever `linkDistance` asked for.
 *
 *    What has to hold is `spacing > caption width`, both measured in GRAPH
 *    units. (Zoom is irrelevant: captions live inside the zoom transform and
 *    scale with it, so magnifying never relieves crowding.) A 24-character
 *    caption is ~130 units wide, so the layout radius needs to be ~330 for a
 *    small graph, which is where 0.0004 puts it. It grows as cbrt(n) from
 *    there — gentle, and the automatic fit absorbs the rest.
 *
 * A spring is used at all -- where d3 would recentre rigidly -- because this
 * projection is bipartite (entity -> fact -> entity) and fragments into many
 * disconnected components, which a rigid recentre would let drift apart forever.
 */
export const DEFAULT_FORCE_PARAMS: ForceParams = {
  linkDistance: 150,
  linkStrength: 0.5,
  chargeStrength: -1600,
  chargeMaxDistance: 900,
  centerX: 0,
  centerY: 0,
  centerStrength: 0.0004,
  velocityDecay: 0.4,
  collidePadding: 10
};

// d3-force's cooling schedule: alpha 1 -> ~0 over ~300 ticks, then stop.
export const ALPHA_MIN = 0.001;
export const ALPHA_DECAY = 1 - Math.pow(ALPHA_MIN, 1 / 300);

export function coolAlpha(alpha: number, decay = ALPHA_DECAY): number {
  return alpha + (0 - alpha) * decay;
}

export function isSettled(alpha: number, min = ALPHA_MIN): boolean {
  return alpha < min;
}

/**
 * Deterministic starting positions: a phyllotaxis (sunflower) disc.
 *
 * Radius grows as sqrt(index) so nodes fill the disc at even density for any
 * count, and the golden angle keeps successive nodes maximally separated -- a
 * far better seed for a force layout than a random cloud, and the reason two
 * runs over the same data produce the same picture.
 */
export const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

export function seedPositions(
  nodes: ReadonlyArray<{ id: string; radius?: number }>,
  centerX: number,
  centerY: number,
  spacing = 26
): SimNode[] {
  return nodes.map((node, index) => {
    const radius = spacing * Math.sqrt(index + 0.5);
    const angle = index * GOLDEN_ANGLE;
    return {
      id: node.id,
      x: centerX + Math.cos(angle) * radius,
      y: centerY + Math.sin(angle) * radius,
      vx: 0,
      vy: 0,
      fx: null,
      fy: null,
      radius: node.radius ?? MIN_NODE_RADIUS
    };
  });
}

/**
 * One simulation step. Returns a new array; never mutates the input, so React
 * state updates and test assertions both behave.
 *
 * Order matches d3: accumulate every force into velocity, then integrate.
 * A pinned node is snapped back to its pin after integration, which is exactly
 * how d3 lets you drag a node while the rest of the graph reacts to it.
 */
export function forceTick(
  nodes: ReadonlyArray<SimNode>,
  links: ReadonlyArray<SimLink>,
  alpha: number,
  params: ForceParams = DEFAULT_FORCE_PARAMS
): SimNode[] {
  const next = nodes.map((node) => ({ ...node }));
  if (!next.length) return next;
  const indexById = new Map<string, number>();
  next.forEach((node, index) => indexById.set(node.id, index));

  // Coincident nodes have no direction to separate along. Nudge them apart by a
  // value derived from their index -- deterministic where d3 uses Math.random.
  function jitter(index: number): number {
    return ((index % 7) - 3) * 1e-3 + 1e-6;
  }

  // -- link force (springs) -------------------------------------------------
  // d3 weights each end by degree, so a leaf swings around its hub instead of
  // dragging the hub around.
  const degree = new Map<string, number>();
  for (const link of links) {
    degree.set(link.source, (degree.get(link.source) ?? 0) + 1);
    degree.set(link.target, (degree.get(link.target) ?? 0) + 1);
  }
  for (const link of links) {
    const sourceIndex = indexById.get(link.source);
    const targetIndex = indexById.get(link.target);
    if (sourceIndex === undefined || targetIndex === undefined || sourceIndex === targetIndex) continue;
    const source = next[sourceIndex];
    const target = next[targetIndex];
    let dx = target.x + target.vx - (source.x + source.vx);
    let dy = target.y + target.vy - (source.y + source.vy);
    if (dx === 0 && dy === 0) {
      dx = jitter(targetIndex);
      dy = jitter(sourceIndex);
    }
    const distance = Math.sqrt(dx * dx + dy * dy) || 1;
    const sourceDegree = degree.get(link.source) ?? 1;
    const targetDegree = degree.get(link.target) ?? 1;
    const strength = params.linkStrength / Math.max(1, Math.min(sourceDegree, targetDegree));
    const push = ((distance - params.linkDistance) / distance) * alpha * strength;
    const bias = sourceDegree / (sourceDegree + targetDegree);
    target.vx -= dx * push * bias;
    target.vy -= dy * push * bias;
    source.vx += dx * push * (1 - bias);
    source.vy += dy * push * (1 - bias);
  }

  // -- many-body repulsion --------------------------------------------------
  // Naive all-pairs rather than d3's Barnes-Hut quadtree: at the few hundred
  // nodes this view caps at, the pair loop costs less than a tree rebuild each
  // tick, and it has no approximation error to reason about.
  const maxDistanceSquared = params.chargeMaxDistance * params.chargeMaxDistance;
  for (let i = 0; i < next.length; i += 1) {
    const a = next[i];
    for (let j = i + 1; j < next.length; j += 1) {
      const b = next[j];
      let dx = b.x - a.x;
      let dy = b.y - a.y;
      if (dx === 0 && dy === 0) {
        dx = jitter(j);
        dy = jitter(i);
      }
      let distanceSquared = dx * dx + dy * dy;
      if (distanceSquared > maxDistanceSquared) continue;
      // Floor the denominator so a near-collision cannot fling nodes to infinity.
      distanceSquared = Math.max(distanceSquared, 1);
      const magnitude = (params.chargeStrength * alpha) / distanceSquared;
      const distance = Math.sqrt(distanceSquared);
      const ux = dx / distance;
      const uy = dy / distance;
      // chargeStrength < 0 => magnitude < 0 => a moves against b: they repel.
      a.vx += ux * magnitude;
      a.vy += uy * magnitude;
      b.vx -= ux * magnitude;
      b.vy -= uy * magnitude;
    }
  }

  // -- centring gravity -----------------------------------------------------
  for (const node of next) {
    node.vx += (params.centerX - node.x) * params.centerStrength * alpha;
    node.vy += (params.centerY - node.y) * params.centerStrength * alpha;
  }

  // -- integrate ------------------------------------------------------------
  for (const node of next) {
    if (node.fx !== null && node.fy !== null) {
      node.x = node.fx;
      node.y = node.fy;
      node.vx = 0;
      node.vy = 0;
      continue;
    }
    node.vx *= 1 - params.velocityDecay;
    node.vy *= 1 - params.velocityDecay;
    node.x += node.vx;
    node.y += node.vy;
  }

  // -- collision ------------------------------------------------------------
  // Positional, after integration, so circles never render overlapping even
  // mid-flight. One relaxation pass is enough when it runs every tick.
  for (let i = 0; i < next.length; i += 1) {
    const a = next[i];
    for (let j = i + 1; j < next.length; j += 1) {
      const b = next[j];
      const minDistance = a.radius + b.radius + params.collidePadding;
      let dx = b.x - a.x;
      let dy = b.y - a.y;
      if (dx === 0 && dy === 0) {
        dx = jitter(j);
        dy = jitter(i);
      }
      const distance = Math.sqrt(dx * dx + dy * dy);
      if (distance >= minDistance) continue;
      const overlap = (minDistance - distance) / 2;
      const ux = dx / distance;
      const uy = dy / distance;
      const aPinned = a.fx !== null && a.fy !== null;
      const bPinned = b.fx !== null && b.fy !== null;
      // A pinned node holds its ground and its partner takes the whole push.
      if (!aPinned) {
        a.x -= ux * overlap * (bPinned ? 2 : 1);
        a.y -= uy * overlap * (bPinned ? 2 : 1);
      }
      if (!bPinned) {
        b.x += ux * overlap * (aPinned ? 2 : 1);
        b.y += uy * overlap * (aPinned ? 2 : 1);
      }
    }
  }

  return next;
}

/** Run the simulation to rest. Used for the initial layout so the graph appears
 *  settled instead of visibly exploding on first paint. */
export function settle(
  nodes: ReadonlyArray<SimNode>,
  links: ReadonlyArray<SimLink>,
  params: ForceParams = DEFAULT_FORCE_PARAMS,
  maxTicks = 300
): SimNode[] {
  let current = nodes.map((node) => ({ ...node }));
  let alpha = 1;
  for (let tick = 0; tick < maxTicks && !isSettled(alpha); tick += 1) {
    current = forceTick(current, links, alpha, params);
    alpha = coolAlpha(alpha);
  }
  return current;
}

// ---------------------------------------------------------------------------
// Edge geometry: curved multi-edges with arrowheads.
//
// Two relationships between the same pair of nodes drawn as one straight line
// each are indistinguishable. Neo4j Browser bows them apart into arcs; this
// computes the same fan-out, plus the endpoint trim that keeps an arrowhead
// sitting ON the target's rim instead of hidden underneath it.
// ---------------------------------------------------------------------------

export interface EdgeSibling {
  /** Position within the bundle sharing this node pair, 0-based. */
  index: number;
  /** How many edges share the pair. 1 == draw it straight. */
  count: number;
  /** True when the edge runs against the pair's canonical (sorted) order.
   *  The perpendicular flips with direction, so without this an A->B and a
   *  B->A edge would bow to the SAME side of the chord and still overlap. */
  flipped: boolean;
}

/** Unordered pair key: A->B and B->A belong to the same bundle. */
export function edgePairKey(sourceId: string, targetId: string): string {
  return sourceId <= targetId ? `${sourceId} ${targetId}` : `${targetId} ${sourceId}`;
}

/** Bundle every edge by node pair. Index order follows edge id, so the fan-out
 *  is identical on every render. */
export function edgeSiblings(
  edges: ReadonlyArray<{ id: string; source_id: string; target_id: string }>
): Map<string, EdgeSibling> {
  const bundles = new Map<string, Array<{ id: string; flipped: boolean }>>();
  for (const edge of edges) {
    const key = edgePairKey(edge.source_id, edge.target_id);
    const bundle = bundles.get(key) ?? [];
    bundle.push({ id: edge.id, flipped: edge.source_id > edge.target_id });
    bundles.set(key, bundle);
  }
  const result = new Map<string, EdgeSibling>();
  for (const bundle of bundles.values()) {
    bundle.sort((a, b) => a.id.localeCompare(b.id));
    bundle.forEach((member, index) => {
      result.set(member.id, { index, count: bundle.length, flipped: member.flipped });
    });
  }
  return result;
}

export interface EdgeEnd extends Point {
  radius: number;
}

export interface EdgeGeometry {
  /** SVG `d`, trimmed to the two rims. Straight `L` when it is the only edge. */
  path: string;
  /** Apex of the arc -- where the relationship-type caption goes. */
  labelX: number;
  labelY: number;
  /** Caption rotation, always within [-90, 90] so text is never upside-down. */
  labelAngle: number;
}

/** Perpendicular distance between adjacent arcs in a bundle. */
export const EDGE_ARC_SPREAD = 26;

/**
 * The arc for one edge of a bundle.
 *
 * `arrowLength` extends the trim at the target end so the marker tip, not the
 * line, touches the rim.
 */
export function edgeGeometry(
  source: EdgeEnd,
  target: EdgeEnd,
  sibling: EdgeSibling,
  arrowLength = 0,
  spread = EDGE_ARC_SPREAD
): EdgeGeometry {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const distance = Math.sqrt(dx * dx + dy * dy);

  // Self-loop (or fully coincident pair): a teardrop out of the top-right, the
  // only way to show a relationship whose ends are the same point.
  if (distance < 1e-6) {
    const loop = Math.max(source.radius * 2.2, 26) + sibling.index * spread * 0.6;
    const path =
      `M ${source.x} ${source.y - source.radius} ` +
      `C ${source.x + loop} ${source.y - loop} ${source.x + loop} ${source.y + loop} ` +
      `${source.x + target.radius} ${source.y}`;
    return { path, labelX: source.x + loop * 0.72, labelY: source.y - loop * 0.1, labelAngle: 0 };
  }

  const ux = dx / distance;
  const uy = dy / distance;
  // Canonical perpendicular: flip for reversed edges so the bundle fans out in
  // screen space rather than folding back onto itself.
  const orientation = sibling.flipped ? -1 : 1;
  const px = -uy * orientation;
  const py = ux * orientation;

  // Symmetric offsets: a lone edge gets 0 (dead straight), a pair gets +/-half
  // a spread, and so on outward.
  const offset = (sibling.index - (sibling.count - 1) / 2) * spread;

  const startX = source.x + ux * source.radius;
  const startY = source.y + uy * source.radius;
  const endX = target.x - ux * (target.radius + arrowLength);
  const endY = target.y - uy * (target.radius + arrowLength);
  const midX = (startX + endX) / 2;
  const midY = (startY + endY) / 2;

  let labelAngle = (Math.atan2(endY - startY, endX - startX) * 180) / Math.PI;
  if (labelAngle > 90) labelAngle -= 180;
  if (labelAngle < -90) labelAngle += 180;

  if (offset === 0) {
    return { path: `M ${startX} ${startY} L ${endX} ${endY}`, labelX: midX, labelY: midY, labelAngle };
  }

  // A quadratic Bezier sits at (P0 + 2*C + P2) / 4 when t = 0.5, so its apex is
  // half the control offset from the chord: double the offset to land the apex
  // exactly `offset` away.
  const controlX = midX + px * offset * 2;
  const controlY = midY + py * offset * 2;
  return {
    path: `M ${startX} ${startY} Q ${controlX} ${controlY} ${endX} ${endY}`,
    labelX: midX + px * offset,
    labelY: midY + py * offset,
    labelAngle
  };
}
