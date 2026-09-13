import { describe, expect, it } from 'vitest';
import {
  DEFAULT_FORCE_PARAMS,
  EDGE_ARC_SPREAD,
  IDENTITY_VIEW,
  LABEL_PALETTE,
  MAX_NODE_RADIUS,
  MAX_ZOOM,
  MIN_NODE_RADIUS,
  MIN_ZOOM,
  buildAdjacency,
  contentBounds,
  coolAlpha,
  degreeMap,
  edgeGeometry,
  edgePairKey,
  edgeSiblings,
  fitToBounds,
  forceTick,
  hubIds,
  isInspectableProperty,
  isSettled,
  labelColor,
  labelColorIndex,
  labelCounts,
  neighborhood,
  nodeCaption,
  nodeColor,
  nodeLabels,
  nodeRadius,
  panBy,
  primaryLabelCounts,
  propertyKeyCounts,
  relationshipTypeCounts,
  seedPositions,
  settle,
  toGraphPoint,
  toViewportPoint,
  wheelZoomFactor,
  zoomAround
} from './graphView';
import type { SimLink, SimNode } from './graphView';

describe('graph view transform math (d3-zoom semantics)', () => {
  it('panBy translates content 1:1 with the pointer, never against it', () => {
    const view = { x: 10, y: -4, k: 2.5 };
    // Dragging right/down by (30, 12) must move content right/down by (30, 12)
    // in viewport units regardless of the zoom level.
    expect(panBy(view, 30, 12)).toEqual({ x: 40, y: 8, k: 2.5 });
    expect(panBy(view, -5, 0)).toEqual({ x: 5, y: -4, k: 2.5 });
  });

  it('zoomAround keeps the graph point under the anchor fixed', () => {
    const view = { x: 12, y: 34, k: 1.4 };
    const anchor = { x: 220, y: 90 };
    const before = toGraphPoint(view, anchor);
    const zoomed = zoomAround(view, 1.9, anchor);
    expect(zoomed.k).toBeCloseTo(1.4 * 1.9, 10);
    const after = toViewportPoint(zoomed, before);
    expect(after.x).toBeCloseTo(anchor.x, 8);
    expect(after.y).toBeCloseTo(anchor.y, 8);
  });

  it('zoomAround with factor > 1 grows the graph (zoom in)', () => {
    const zoomed = zoomAround(IDENTITY_VIEW, 2, { x: 100, y: 50 });
    expect(zoomed.k).toBe(2);
    // A graph point at the anchor stays put; one to its right moves further right.
    expect(toViewportPoint(zoomed, { x: 100, y: 50 })).toEqual({ x: 100, y: 50 });
    expect(toViewportPoint(zoomed, { x: 110, y: 50 }).x).toBeGreaterThan(110);
  });

  it('zoomAround clamps at the zoom range and returns the view unchanged at the rail', () => {
    const atMax = { x: 0, y: 0, k: MAX_ZOOM };
    expect(zoomAround(atMax, 2, { x: 10, y: 10 })).toBe(atMax);
    const atMin = { x: 0, y: 0, k: MIN_ZOOM };
    expect(zoomAround(atMin, 0.5, { x: 10, y: 10 })).toBe(atMin);
    expect(zoomAround(IDENTITY_VIEW, 1000, { x: 0, y: 0 }).k).toBe(MAX_ZOOM);
    expect(zoomAround(IDENTITY_VIEW, 0.0001, { x: 0, y: 0 }).k).toBe(MIN_ZOOM);
  });

  it('wheelZoomFactor: wheel-up zooms in, per-mode units, pinch is 10x', () => {
    // deltaY < 0 (wheel up) => factor > 1 => zoom in.
    expect(wheelZoomFactor({ deltaY: -100, deltaMode: 0, ctrlKey: false })).toBeGreaterThan(1);
    expect(wheelZoomFactor({ deltaY: 100, deltaMode: 0, ctrlKey: false })).toBeLessThan(1);
    // d3's exact wheelDelta: 2^(-deltaY * unit).
    expect(wheelZoomFactor({ deltaY: -100, deltaMode: 0, ctrlKey: false })).toBeCloseTo(Math.pow(2, 0.2), 10);
    expect(wheelZoomFactor({ deltaY: -1, deltaMode: 1, ctrlKey: false })).toBeCloseTo(Math.pow(2, 0.05), 10);
    // Pinch (ctrl+wheel) multiplies the exponent by 10.
    expect(wheelZoomFactor({ deltaY: -100, deltaMode: 0, ctrlKey: true })).toBeCloseTo(Math.pow(2, 2), 10);
  });

  it('contentBounds wraps points with a margin and is null when empty', () => {
    expect(contentBounds([])).toBeNull();
    const bounds = contentBounds(
      [
        { x: 10, y: 40 },
        { x: 110, y: 20 },
        { x: 60, y: 90 }
      ],
      24
    );
    expect(bounds).toEqual({ minX: -14, minY: -4, maxX: 134, maxY: 114 });
  });

  it('fitToBounds scales content into the viewport with padding and centers it', () => {
    const bounds = { minX: 0, minY: 0, maxX: 100, maxY: 50 };
    const viewport = { width: 800, height: 496 };
    const fitted = fitToBounds(bounds, viewport, 48);
    // k = min((800-96)/100, (496-96)/50) = min(7.04, 8) = 7.04
    expect(fitted.k).toBeCloseTo(7.04, 10);
    // The content center lands on the viewport center.
    const center = toViewportPoint(fitted, { x: 50, y: 25 });
    expect(center.x).toBeCloseTo(400, 8);
    expect(center.y).toBeCloseTo(248, 8);
    // All four corners are inside the padded viewport.
    for (const corner of [
      { x: 0, y: 0 },
      { x: 100, y: 0 },
      { x: 0, y: 50 },
      { x: 100, y: 50 }
    ]) {
      const mapped = toViewportPoint(fitted, corner);
      expect(mapped.x).toBeGreaterThanOrEqual(47.9);
      expect(mapped.x).toBeLessThanOrEqual(752.1);
      expect(mapped.y).toBeGreaterThanOrEqual(47.9);
      expect(mapped.y).toBeLessThanOrEqual(448.1);
    }
  });

  it('fitToBounds is a real fit, not a reset: it zooms IN on a small cluster', () => {
    const small = fitToBounds({ minX: 380, minY: 220, maxX: 420, maxY: 260 }, { width: 800, height: 496 });
    expect(small.k).toBeGreaterThan(1);
    expect(small.k).toBeLessThanOrEqual(MAX_ZOOM);
    const huge = fitToBounds({ minX: -5000, minY: -5000, maxX: 5000, maxY: 5000 }, { width: 800, height: 496 });
    expect(huge.k).toBe(MIN_ZOOM);
  });

  it('buildAdjacency + neighborhood produce the ego network per hop depth', () => {
    const adjacency = buildAdjacency([
      { source_id: 'a', target_id: 'b' },
      { source_id: 'b', target_id: 'c' },
      { source_id: 'c', target_id: 'd' },
      { source_id: 'x', target_id: 'y' }
    ]);
    expect(neighborhood(adjacency, 'b', 1)).toEqual(new Set(['b', 'a', 'c']));
    expect(neighborhood(adjacency, 'b', 2)).toEqual(new Set(['b', 'a', 'c', 'd']));
    // Disconnected islands never join the ego network at any depth.
    expect(neighborhood(adjacency, 'b', 99)).toEqual(new Set(['b', 'a', 'c', 'd']));
    // An unknown start is its own neighborhood.
    expect(neighborhood(adjacency, 'zz', 2)).toEqual(new Set(['zz']));
  });
});

describe('degree-based node sizing', () => {
  const adjacency = buildAdjacency([
    { source_id: 'hub', target_id: 'a' },
    { source_id: 'hub', target_id: 'b' },
    { source_id: 'hub', target_id: 'c' },
    { source_id: 'hub', target_id: 'd' },
    { source_id: 'a', target_id: 'b' }
  ]);

  it('counts distinct neighbors per node', () => {
    const degrees = degreeMap(adjacency);
    expect(degrees.get('hub')).toBe(4);
    expect(degrees.get('a')).toBe(2);
    expect(degrees.get('c')).toBe(1);
  });

  it('scales radius by area, not linearly, so hubs do not dominate', () => {
    // A 4x-degree node is 2x the radius (area 4x), never 4x the radius.
    const one = nodeRadius(1, 4, 0, 20);
    const four = nodeRadius(4, 4, 0, 20);
    expect(four).toBeCloseTo(20, 6);
    expect(one).toBeCloseTo(10, 6);
    expect(four / one).toBeCloseTo(2, 6);
  });

  it('clamps to the configured band and survives a degreeless graph', () => {
    expect(nodeRadius(0, 10)).toBe(MIN_NODE_RADIUS);
    expect(nodeRadius(10, 10)).toBe(MAX_NODE_RADIUS);
    expect(nodeRadius(99, 10)).toBe(MAX_NODE_RADIUS);
    expect(nodeRadius(-5, 10)).toBe(MIN_NODE_RADIUS);
    expect(nodeRadius(3, 0)).toBe(MIN_NODE_RADIUS);
  });

  it('picks the busiest nodes as hubs, breaking ties stably', () => {
    const degrees = degreeMap(adjacency);
    expect([...hubIds(degrees, 2)]).toEqual(['hub', 'a']);
    expect(hubIds(degrees, 0).size).toBe(0);
    // Ties resolve on id, so repeated renders label the same nodes.
    const tied = degreeMap(buildAdjacency([{ source_id: 'z', target_id: 'y' }]));
    expect([...hubIds(tied, 1)]).toEqual(['y']);
  });
});

// The two node shapes the server actually returns (verified against
// /api/graph): entity nodes carry real multi-label sets, memory nodes come back
// with `labels` empty and carry memory_type / relationship_type instead.
const entityNode = {
  id: 'entity:1',
  label: 'Pinnacle Events',
  node_type: 'entity',
  labels: ['Entity', 'Customer'],
  properties: { name: 'Pinnacle Events', graph_key: 'customer:...', name_embedding: [0, 1, 2] }
};
const memoryNode = {
  id: 'memory:9',
  label: 'Group Sales decided to assign a dedicated compliance concierge',
  node_type: 'memory',
  labels: [] as string[],
  memory_type: 'decision',
  relationship_type: 'DECIDED',
  properties: { fact: 'Group Sales decided...', predicate: 'decided', confidence: 0.9 }
};

describe('labels, colour and captions (Neo4j "colour by label")', () => {
  it('uses declared graph labels for entities and the memory type for facts', () => {
    expect(nodeLabels(entityNode)).toEqual(['Entity', 'Customer']);
    expect(nodeLabels(memoryNode)).toEqual(['decision']);
  });

  it('falls back through relationship type to a structural label', () => {
    expect(nodeLabels({ ...memoryNode, memory_type: null })).toEqual(['DECIDED']);
    expect(nodeLabels({ ...memoryNode, memory_type: null, relationship_type: null })).toEqual(['Memory']);
    expect(nodeLabels({ ...entityNode, labels: [] })).toEqual(['Entity']);
    // Blank-but-present labels are not labels.
    expect(nodeLabels({ ...entityNode, labels: ['  ', ''] })).toEqual(['Entity']);
  });

  it('counts every label of every node, count desc then name asc', () => {
    const counts = labelCounts([entityNode, { ...entityNode, id: 'entity:2' }, memoryNode]);
    expect(counts).toEqual([
      { name: 'Customer', count: 2 },
      { name: 'Entity', count: 2 },
      { name: 'decision', count: 1 }
    ]);
  });

  describe('primaryLabelCounts', () => {
    it('counts only the colour label (nodeLabels(node)[0]) -- never a node\'s secondary labels', () => {
      // Contrast with labelCounts just above: the same three nodes, but
      // "Customer" (entityNode's second label) does not get a row at all.
      const counts = primaryLabelCounts([entityNode, { ...entityNode, id: 'entity:2' }, memoryNode]);
      expect(counts).toEqual([
        { name: 'Entity', count: 2 },
        { name: 'decision', count: 1 }
      ]);
    });

    it('drops a scope-kind marker such as "Tenant" from the legend, exactly like nodeColor already does', () => {
      // The server's real entity-node shape (verified against /api/graph):
      // `labels: [entityTypeLabel, scopeKindTitle]`, e.g. ["Resource",
      // "Tenant"] -- type first, scope marker second, every time. A legend
      // that promises "node KINDS" must not list the scope marker as one.
      const resource = { id: 'entity:r1', label: 'Special Offers', node_type: 'entity', labels: ['Resource', 'Tenant'] };
      const component = { id: 'entity:c1', label: 'Jedai KB', node_type: 'entity', labels: ['Component', 'Tenant'] };
      const counts = primaryLabelCounts([resource, component]);
      expect(counts).toEqual([
        { name: 'Component', count: 1 },
        { name: 'Resource', count: 1 }
      ]);
      expect(counts.find((row) => row.name === 'Tenant')).toBeUndefined();
    });

    it('is empty, not broken, for an empty graph', () => {
      expect(primaryLabelCounts([])).toEqual([]);
    });
  });

  it('assigns a label the same colour on every call and across node sets', () => {
    expect(labelColor('Customer')).toBe(labelColor('Customer'));
    // Determinism is the point: the colour is a pure function of the string,
    // never of position in the legend or of what else is on screen.
    expect(nodeColor(entityNode)).toBe(labelColor('Entity'));
    expect(nodeColor(memoryNode)).toBe(labelColor('decision'));
    expect(LABEL_PALETTE).toContain(labelColor('anything'));
    // A renamed vocabulary just gets a new stable colour, never a crash.
    expect(labelColor('rollup')).not.toBe(labelColor('theme'));
  });

  it('spreads distinct labels across the palette rather than collapsing them', () => {
    const labels = ['Entity', 'Customer', 'decision', 'rollup', 'anchor', 'preference', 'state', 'incident'];
    const used = new Set(labels.map((label) => labelColorIndex(label)));
    expect(used.size).toBeGreaterThanOrEqual(6);
    expect(labelColorIndex('x', 0)).toBe(0);
    for (const label of labels) {
      expect(labelColorIndex(label)).toBeGreaterThanOrEqual(0);
      expect(labelColorIndex(label)).toBeLessThan(LABEL_PALETTE.length);
    }
  });

  it('captions from the decrypted label, not the raw name property', () => {
    // The server reveals `label` but leaves `properties.name` as stored, so a
    // sealed scope would render ciphertext if the property won.
    const sealed = { ...entityNode, label: 'Pinnacle Events', properties: { name: 'gAAAAABm-ciphertext' } };
    expect(nodeCaption(sealed)).toBe('Pinnacle Events');
  });

  it('falls back to a property, then the id, and truncates with an ellipsis', () => {
    expect(nodeCaption({ ...entityNode, label: '   ' })).toBe('Pinnacle Events');
    expect(nodeCaption({ id: 'n1', label: '', node_type: 'entity' })).toBe('n1');
    expect(nodeCaption(memoryNode, 20)).toBe('Group Sales decided…');
    expect(nodeCaption(memoryNode, 20)).toHaveLength(20);
    expect(nodeCaption(entityNode, 0)).toBe('Pinnacle Events');
  });
});

describe('database information panel tallies', () => {
  it('counts relationship types over the edge list', () => {
    expect(
      relationshipTypeCounts([
        { edge_type: 'subject' },
        { edge_type: 'object' },
        { edge_type: 'subject' },
        { edge_type: 'derived_from' }
      ])
    ).toEqual([
      { name: 'subject', count: 2 },
      { name: 'derived_from', count: 1 },
      { name: 'object', count: 1 }
    ]);
  });

  it('counts property keys but hides embedding vectors', () => {
    const counts = propertyKeyCounts([entityNode, memoryNode]);
    const names = counts.map((entry) => entry.name);
    expect(names).toContain('name');
    expect(names).toContain('predicate');
    expect(names).not.toContain('name_embedding');
  });

  it('rejects long numeric vectors whatever they are called', () => {
    expect(isInspectableProperty('name_embedding', [1, 2])).toBe(false);
    expect(isInspectableProperty('object_embedding', 'x')).toBe(false);
    expect(isInspectableProperty('vector', new Array(256).fill(0))).toBe(false);
    expect(isInspectableProperty('episode_uuids', ['a', 'b'])).toBe(true);
    expect(isInspectableProperty('confidence', 0.9)).toBe(true);
  });

  it('is empty, not broken, for an empty graph', () => {
    expect(labelCounts([])).toEqual([]);
    expect(relationshipTypeCounts([])).toEqual([]);
    expect(propertyKeyCounts([])).toEqual([]);
  });
});

describe('force-directed layout', () => {
  function nodesAt(points: Array<[string, number, number]>, radius = 10): SimNode[] {
    return points.map(([id, x, y]) => ({ id, x, y, vx: 0, vy: 0, fx: null, fy: null, radius }));
  }

  it('seeds a deterministic phyllotaxis disc centred on the requested point', () => {
    const a = seedPositions([{ id: 'a' }, { id: 'b' }, { id: 'c' }], 100, 50);
    const b = seedPositions([{ id: 'a' }, { id: 'b' }, { id: 'c' }], 100, 50);
    expect(a).toEqual(b);
    // Every node distinct, and radius grows with index.
    const radii = a.map((node) => Math.hypot(node.x - 100, node.y - 50));
    expect(radii[0]).toBeLessThan(radii[1]);
    expect(radii[1]).toBeLessThan(radii[2]);
    expect(new Set(a.map((node) => `${node.x},${node.y}`)).size).toBe(3);
  });

  it('pulls two far-apart linked nodes toward the link distance', () => {
    const links: SimLink[] = [{ source: 'a', target: 'b' }];
    const settled = settle(nodesAt([['a', -400, 0], ['b', 400, 0]]), links);
    const distance = Math.hypot(settled[0].x - settled[1].x, settled[0].y - settled[1].y);
    // Springs contract 800px down toward the 64px rest length; repulsion and
    // collision hold it off zero.
    expect(distance).toBeLessThan(400);
    expect(distance).toBeGreaterThan(DEFAULT_FORCE_PARAMS.linkDistance * 0.5);
  });

  it('pushes two unlinked nodes apart instead of letting them sit on top', () => {
    const before = nodesAt([['a', 0, 0], ['b', 4, 0]]);
    const after = settle(before, []);
    const distance = Math.hypot(after[0].x - after[1].x, after[0].y - after[1].y);
    // Collision alone guarantees at least the two radii plus padding.
    expect(distance).toBeGreaterThanOrEqual(10 + 10 + DEFAULT_FORCE_PARAMS.collidePadding - 1e-6);
  });

  it('separates perfectly coincident nodes deterministically (no Math.random)', () => {
    const first = settle(nodesAt([['a', 7, 7], ['b', 7, 7]]), []);
    const second = settle(nodesAt([['a', 7, 7], ['b', 7, 7]]), []);
    expect(first).toEqual(second);
    expect(Math.hypot(first[0].x - first[1].x, first[0].y - first[1].y)).toBeGreaterThan(1);
    expect(Number.isFinite(first[0].x)).toBe(true);
  });

  it('never moves a pinned node, but still lets it move its neighbours', () => {
    const nodes = nodesAt([['pin', 0, 0], ['free', 300, 300]]);
    nodes[0].fx = 0;
    nodes[0].fy = 0;
    const after = settle(nodes, [{ source: 'pin', target: 'free' }]);
    expect(after[0].x).toBe(0);
    expect(after[0].y).toBe(0);
    expect(after[0].vx).toBe(0);
    // The unpinned end is the one that travelled.
    expect(Math.hypot(after[1].x - 300, after[1].y - 300)).toBeGreaterThan(1);
  });

  it('is a pure function of its inputs -- same in, same out, input untouched', () => {
    const nodes = nodesAt([['a', 10, 20], ['b', 90, 40], ['c', -30, 70]]);
    const links: SimLink[] = [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }];
    const snapshot = JSON.stringify(nodes);
    const first = forceTick(nodes, links, 0.5);
    const second = forceTick(nodes, links, 0.5);
    expect(first).toEqual(second);
    expect(first).not.toBe(nodes);
    expect(JSON.stringify(nodes)).toBe(snapshot);
  });

  it('spreads a small graph across hundreds of units, not into a ball', () => {
    // The regression this pins: centring is a SPRING, so if it is too strong it
    // competes with repulsion and fixes the layout radius by a cube-root
    // balance -- at which point linkDistance stops having any effect and every
    // graph collapses into an unreadable clump. Observed at centerStrength=0.05.
    const nodes = seedPositions(
      Array.from({ length: 19 }, (_, index) => ({ id: `n${index}`, radius: 12 })),
      0,
      0
    );
    const links: SimLink[] = Array.from({ length: 16 }, (_, index) => ({
      source: `n${index}`,
      target: `n${index + 1}`
    }));
    const after = settle(nodes, links);
    const extent = Math.max(...after.map((node) => Math.hypot(node.x, node.y)));
    expect(extent).toBeGreaterThan(120);
    expect(extent).toBeLessThan(900);
    // Neighbours end up near the spring's rest length, not crushed on top of
    // each other.
    const spans = links.map((link) => {
      const a = after.find((node) => node.id === link.source)!;
      const b = after.find((node) => node.id === link.target)!;
      return Math.hypot(a.x - b.x, a.y - b.y);
    });
    const median = spans.sort((a, b) => a - b)[Math.floor(spans.length / 2)];
    expect(median).toBeGreaterThan(DEFAULT_FORCE_PARAMS.linkDistance * 0.45);
  });

  it('pulls a drifting island back toward the configured centre', () => {
    const params = { ...DEFAULT_FORCE_PARAMS, centerX: 0, centerY: 0 };
    const after = settle(nodesAt([['lonely', 900, 900]]), [], params);
    expect(Math.hypot(after[0].x, after[0].y)).toBeLessThan(Math.hypot(900, 900));
  });

  it('cools to rest on d3\'s schedule and reports settled', () => {
    let alpha = 1;
    let ticks = 0;
    while (!isSettled(alpha) && ticks < 10_000) {
      alpha = coolAlpha(alpha);
      ticks += 1;
    }
    expect(ticks).toBe(300);
    expect(isSettled(0.0009)).toBe(true);
    expect(isSettled(0.5)).toBe(false);
  });

  it('handles the empty and single-node graphs without dividing by zero', () => {
    expect(forceTick([], [], 1)).toEqual([]);
    expect(settle([], [])).toEqual([]);
    const one = settle(nodesAt([['only', 5, 5]]), []);
    expect(one).toHaveLength(1);
    expect(Number.isFinite(one[0].x)).toBe(true);
  });

  it('ignores links whose endpoints are not in the node set', () => {
    const after = forceTick(nodesAt([['a', 0, 0]]), [{ source: 'a', target: 'ghost' }], 1);
    expect(Number.isFinite(after[0].x)).toBe(true);
  });

  it('stays finite and non-overlapping at 500 nodes', () => {
    const nodes = seedPositions(
      Array.from({ length: 500 }, (_, index) => ({ id: `n${index}`, radius: 8 })),
      0,
      0
    );
    const links: SimLink[] = Array.from({ length: 499 }, (_, index) => ({
      source: `n${index}`,
      target: `n${index + 1}`
    }));
    const after = settle(nodes, links, DEFAULT_FORCE_PARAMS, 60);
    expect(after).toHaveLength(500);
    expect(after.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y))).toBe(true);
    // Spot-check that collision actually held across a crowded run.
    for (let i = 0; i < 40; i += 1) {
      for (let j = i + 1; j < 40; j += 1) {
        expect(Math.hypot(after[i].x - after[j].x, after[i].y - after[j].y)).toBeGreaterThan(1);
      }
    }
  });
});

describe('edge geometry: curved multi-edges and arrowheads', () => {
  const left = { x: 0, y: 0, radius: 10 };
  const right = { x: 200, y: 0, radius: 10 };

  it('bundles edges by unordered node pair, stable on edge id', () => {
    expect(edgePairKey('b', 'a')).toBe(edgePairKey('a', 'b'));
    const siblings = edgeSiblings([
      { id: 'e2', source_id: 'b', target_id: 'a' },
      { id: 'e1', source_id: 'a', target_id: 'b' },
      { id: 'e3', source_id: 'a', target_id: 'c' }
    ]);
    expect(siblings.get('e1')).toEqual({ index: 0, count: 2, flipped: false });
    expect(siblings.get('e2')).toEqual({ index: 1, count: 2, flipped: true });
    expect(siblings.get('e3')).toEqual({ index: 0, count: 1, flipped: false });
  });

  it('draws a lone edge dead straight, trimmed to both rims', () => {
    const geometry = edgeGeometry(left, right, { index: 0, count: 1, flipped: false });
    expect(geometry.path).toBe('M 10 0 L 190 0');
    expect(geometry.path).not.toContain('Q');
    expect(geometry.labelX).toBe(100);
    expect(geometry.labelY).toBe(0);
  });

  it('leaves room for the arrowhead at the target end only', () => {
    const geometry = edgeGeometry(left, right, { index: 0, count: 1, flipped: false }, 8);
    expect(geometry.path).toBe('M 10 0 L 182 0');
  });

  it('bows a pair of edges to opposite sides so neither hides the other', () => {
    const a = edgeGeometry(left, right, { index: 0, count: 2, flipped: false });
    const b = edgeGeometry(left, right, { index: 1, count: 2, flipped: false });
    expect(a.path).toContain('Q');
    expect(Math.sign(a.labelY)).toBe(-Math.sign(b.labelY));
    expect(Math.abs(a.labelY)).toBeCloseTo(EDGE_ARC_SPREAD / 2, 6);
    // The apex is exactly `offset` off the chord: the control point is doubled
    // because a quadratic Bezier only reaches half way to its control.
    expect(a.path).toContain(`Q 100 ${-EDGE_ARC_SPREAD}`);
  });

  it('separates a bidirectional pair rather than folding both onto one side', () => {
    // A->B and B->A: the perpendicular flips with direction, so without the
    // `flipped` correction these two would bow to the same side and overlap.
    const forward = edgeGeometry(left, right, { index: 0, count: 2, flipped: false });
    const backward = edgeGeometry(right, left, { index: 1, count: 2, flipped: true });
    expect(Math.sign(forward.labelY)).toBe(-Math.sign(backward.labelY));
  });

  it('fans three or more edges outward around the straight chord', () => {
    const offsets = [0, 1, 2].map(
      (index) => edgeGeometry(left, right, { index, count: 3, flipped: false }).labelY
    );
    expect(offsets[1]).toBeCloseTo(0, 6);
    expect(offsets[0]).toBeCloseTo(-EDGE_ARC_SPREAD, 6);
    expect(offsets[2]).toBeCloseTo(EDGE_ARC_SPREAD, 6);
  });

  it('keeps the caption angle upright for every direction', () => {
    for (const target of [
      { x: 200, y: 0, radius: 10 },
      { x: -200, y: 0, radius: 10 },
      { x: 0, y: 200, radius: 10 },
      { x: -140, y: -140, radius: 10 },
      { x: 140, y: -140, radius: 10 }
    ]) {
      const { labelAngle } = edgeGeometry(left, target, { index: 0, count: 1, flipped: false });
      expect(labelAngle).toBeGreaterThanOrEqual(-90);
      expect(labelAngle).toBeLessThanOrEqual(90);
    }
    // A right-to-left edge reads left-to-right, not mirrored.
    expect(edgeGeometry(right, left, { index: 0, count: 1, flipped: true }).labelAngle).toBe(0);
  });

  it('draws a self-loop as a teardrop instead of a zero-length line', () => {
    const geometry = edgeGeometry(left, { ...left }, { index: 0, count: 1, flipped: false });
    expect(geometry.path).toContain('C');
    expect(geometry.path).not.toContain('NaN');
    expect(Number.isFinite(geometry.labelX)).toBe(true);
    // Stacked self-loops get progressively wider so they stay distinguishable.
    const second = edgeGeometry(left, { ...left }, { index: 1, count: 2, flipped: false });
    expect(second.labelX).toBeGreaterThan(geometry.labelX);
  });

  it('never emits NaN for degenerate radii or coincident-ish ends', () => {
    const geometry = edgeGeometry(
      { x: 0, y: 0, radius: 0 },
      { x: 0.0001, y: 0, radius: 0 },
      { index: 0, count: 2, flipped: false }
    );
    expect(geometry.path).not.toContain('NaN');
  });
});
