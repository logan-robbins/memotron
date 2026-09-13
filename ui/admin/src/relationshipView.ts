// Structured-triple rendering for a relationship row.
//
// Every list surface in the admin UI (the Memory Explorer table, the graph
// canvas, the Inspector, the truth timeline, the archive) renders a
// relationship as a (subject, predicate, object) triple rather than its bare
// `fact` sentence -- a natural-language string like "Jedai Knowledge Base
// ingests Special Offers" that repeats both entity names and, sitting in a
// list (or drawn as a circle) next to real entity rows, reads as a bogus
// entity of its own. `Memotron.knowledge_graph` (client.py) stores each
// relationship reified as entity --subject--> fact --object--> entity, so
// evidence, the truth timeline and supersession can all attach to the fact;
// this module undoes that reification for every DISPLAY surface, including
// the canvas (`collapseFactNodes`), while keeping the fact fully
// inspectable.
//
// Kept free of React so the redundancy heuristic and the collapse walk --
// the parts with actual judgment calls in them -- are unit-testable without
// mounting a component.

import { type LabelledNode, nodeLabels } from './graphView';

export interface RelationshipTriple {
  subject: string;
  predicate: string;
  object: string;
  /** The natural-language sentence, e.g. `properties.fact` or a
   *  `MemoryEvidence`/`TimelineEntry`'s `fact` field. Empty when unavailable. */
  fact: string;
}

function pickString(source: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

/**
 * Build a triple from whatever shape the endpoint served.
 *
 * Every relationship-bearing payload in this app carries the same field
 * names in one of two places: nested in a knowledge-graph memory node's
 * `properties` bag (subject/object already RESOLVED to entity display names,
 * per `Memotron.knowledge_graph`, with `subject_surface`/`object_surface`
 * as the extraction-time fallback), or flat on a `MemoryEvidence` /
 * `TimelineEntry` object (subject/object already resolved there too -- see
 * `Memotron.memory_evidence` / `truth_timeline`). Passing either shape
 * here works: resolved names win when present, surface strings degrade
 * gracefully when they are all a payload has, and `predicate_canonical` backs
 * up a blank `predicate`.
 */
export function buildTriple(source: Record<string, unknown> | null | undefined): RelationshipTriple {
  const bag = source ?? {};
  return {
    subject: pickString(bag, 'subject', 'subject_surface'),
    predicate: pickString(bag, 'predicate', 'predicate_canonical'),
    object: pickString(bag, 'object', 'object_surface'),
    fact: pickString(bag, 'fact')
  };
}

// Function words stripped before comparing token sets. Trimming them means a
// fact sentence that only differs from `subject predicate object` by articles
// or copulas ("The X is ingested by Y") still counts as a pure restatement,
// while any CONTENT word absent from the triple -- a qualifier, a number, a
// date, a condition -- always counts as real information.
const STOPWORDS = new Set([
  'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
  'to', 'of', 'in', 'on', 'at', 'by', 'with', 'for', 'as', 'that', 'this',
  'these', 'those', 'it', 'its', 'and', 'or', 'but', 'from', 'into', 'than', 'then', 'now'
]);

/** Lowercase, punctuation-stripped, stopword-filtered token set. Order and
 *  repetition do not matter for redundancy detection -- only new content. */
export function contentTokens(text: string): Set<string> {
  const tokens = text
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, ' ')
    .split(/\s+/)
    .filter((token) => token.length > 0 && !STOPWORDS.has(token));
  return new Set(tokens);
}

/**
 * True when the `fact` sentence's content is fully covered by the triple's
 * own words -- it is a restatement of `subject predicate object` and prints
 * nothing a reader does not already have from the chips above it. False the
 * moment the sentence carries one token the triple does not, because that is
 * real information (a qualifier, a number, a condition) the triple alone
 * would lose.
 *
 * A blank fact counts as redundant: there is nothing to show either way.
 */
export function isRedundantFactSentence(triple: Pick<RelationshipTriple, 'subject' | 'predicate' | 'object' | 'fact'>): boolean {
  const factTokens = contentTokens(triple.fact);
  if (factTokens.size === 0) return true;
  const tripleTokens = new Set([
    ...contentTokens(triple.subject),
    ...contentTokens(triple.predicate),
    ...contentTokens(triple.object)
  ]);
  for (const token of factTokens) {
    if (!tripleTokens.has(token)) return false;
  }
  return true;
}

export interface GraphEdgeLike {
  source_id: string;
  target_id: string;
  edge_type: string;
}

/**
 * Resolve a memory/fact node's two entity endpoints from the same payload's
 * node + edge arrays.
 *
 * `Memotron.knowledge_graph` (the source of `/api/graph` and
 * `/api/archive`) models each relationship as `entity --subject--> fact
 * --object--> entity`; this walks those two reified edges back to the real
 * entity nodes so the caller can read their `labels` (entity type) for the
 * graph view's own colour chip -- reusing `nodeLabels`/`labelColor` from
 * `graphView.ts` keeps the colouring identical across every screen.
 *
 * Returns null for an endpoint the payload does not carry edges for (e.g.
 * `/api/archive`, whose `facts` array holds only the memory nodes, no
 * entities and no edges) -- callers degrade to a plain, uncoloured name in
 * that case rather than fabricating a label.
 */
export function resolveFactEndpoints<T extends LabelledNode & { id: string }>(
  factNodeId: string,
  nodes: ReadonlyArray<T>,
  edges: ReadonlyArray<GraphEdgeLike>
): { subject: T | null; object: T | null } {
  const byId = new Map(nodes.map((node) => [node.id, node] as const));
  let subject: T | null = null;
  let object: T | null = null;
  for (const edge of edges) {
    if (edge.edge_type === 'subject' && edge.target_id === factNodeId) {
      subject = byId.get(edge.source_id) ?? subject;
    } else if (edge.edge_type === 'object' && edge.source_id === factNodeId) {
      object = byId.get(edge.target_id) ?? object;
    }
  }
  return { subject, object };
}

/** Convenience: an endpoint node's entity-type labels, or null when the node
 *  itself is unresolved (see `resolveFactEndpoints`). */
export function endpointLabels(node: LabelledNode | null): readonly string[] | null {
  return node ? nodeLabels(node) : null;
}

// ---------------------------------------------------------------------------
// Canvas collapse: entity -> fact -> entity drawn as ONE entity -> entity edge.
// ---------------------------------------------------------------------------

/** A fact/memory node's shape this module needs to collapse it into an edge. */
export interface FactLikeNode extends LabelledNode {
  id: string;
  node_type: string;
  relationship_uuid?: string | null;
}

/** The edge a collapsed fact becomes. Shaped like the server's `GraphEdge` --
 *  `id`/`source_id`/`target_id`/`edge_type`/`label` -- so it drops straight
 *  into every canvas helper that already consumes that shape (adjacency,
 *  sibling-bundling, arc geometry, the relationship-type legend), plus
 *  `factNode`, the one thing collapsing away the reification would otherwise
 *  cost: clicking the edge must still be able to open the Inspector on the
 *  exact fact it came from. */
export interface CollapsedEdge<T extends FactLikeNode = FactLikeNode> {
  id: string;
  source_id: string;
  target_id: string;
  edge_type: string;
  label: string;
  relationship_uuid: string | null;
  factNode: T;
}

export interface CollapsedGraph<T extends FactLikeNode> {
  /** Entities, plus (fallback, never dropped) any fact whose reification
   *  could not be resolved to two real entities -- see `collapseFactNodes`. */
  nodes: T[];
  edges: CollapsedEdge<T>[];
}

/** A caption when a fact carries neither `predicate` nor a memory type --
 *  every fact observed from the server has had one of the two, but a blank
 *  edge caption would still be a worse failure mode than a generic one. */
export const UNKNOWN_PREDICATE = 'related to';

/**
 * Collapse every `entity --subject--> fact --object--> entity` chain in a
 * knowledge-graph payload into one `entity -> entity` edge captioned with the
 * fact's predicate.
 *
 * This is the fix for the canvas's worst readability defect: drawn literally,
 * every fact is its own circle captioned with a full sentence, and the only
 * edge labels left are the structural words "subject"/"object" -- a graph of
 * facts pretending to be a graph of entities. The caller's expected shape
 * after this runs is `Entity: A -- predicate --> Entity: B`, node count ==
 * entity count, edge count == fact count.
 *
 * A fact that cannot be walked back to two ENTITY endpoints -- no subject or
 * object edge in the payload (e.g. a filtered/partial fetch), or an endpoint
 * that is itself another fact (`superseded_by`/`derived_from` chains, which
 * are not part of this reification and belong to the lineage view instead) --
 * is not silently dropped: the fact is kept as an ordinary node, exactly as
 * the server sent it, so nothing un-inspectable disappears from the canvas.
 */
export function collapseFactNodes<T extends FactLikeNode>(
  nodes: ReadonlyArray<T>,
  edges: ReadonlyArray<GraphEdgeLike>
): CollapsedGraph<T> {
  const entityIds = new Set(nodes.filter((node) => node.node_type === 'entity').map((node) => node.id));
  const collapsedEdges: CollapsedEdge<T>[] = [];
  const keptNodes: T[] = [];

  for (const node of nodes) {
    if (node.node_type === 'entity') {
      keptNodes.push(node);
      continue;
    }
    const { subject, object } = resolveFactEndpoints(node.id, nodes, edges);
    if (!subject || !object || !entityIds.has(subject.id) || !entityIds.has(object.id)) {
      keptNodes.push(node);
      continue;
    }
    const predicate = buildTriple(node.properties).predicate || node.memory_type?.trim() || UNKNOWN_PREDICATE;
    collapsedEdges.push({
      id: node.id,
      source_id: subject.id,
      target_id: object.id,
      edge_type: predicate,
      label: predicate,
      relationship_uuid: node.relationship_uuid ?? null,
      factNode: node
    });
  }

  return { nodes: keptNodes, edges: collapsedEdges };
}
