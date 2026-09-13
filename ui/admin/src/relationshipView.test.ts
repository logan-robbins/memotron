import { describe, expect, it } from 'vitest';
import {
  UNKNOWN_PREDICATE,
  buildTriple,
  collapseFactNodes,
  contentTokens,
  endpointLabels,
  isRedundantFactSentence,
  resolveFactEndpoints
} from './relationshipView';

describe('buildTriple', () => {
  it('prefers resolved subject/object over the extraction-time surface strings', () => {
    const triple = buildTriple({
      subject: 'Jedai Knowledge Base',
      subject_surface: 'jedai kb',
      predicate: 'ingests',
      object: 'Special Offers',
      object_surface: 'special offers',
      fact: 'Jedai Knowledge Base ingests Special Offers'
    });
    expect(triple).toEqual({
      subject: 'Jedai Knowledge Base',
      predicate: 'ingests',
      object: 'Special Offers',
      fact: 'Jedai Knowledge Base ingests Special Offers'
    });
  });

  it('falls back to surface strings and predicate_canonical when resolved fields are blank', () => {
    const triple = buildTriple({
      subject_surface: 'jedai kb',
      predicate_canonical: 'INGESTS',
      object_surface: 'special offers',
      fact: 'jedai kb ingests special offers'
    });
    expect(triple.subject).toBe('jedai kb');
    expect(triple.predicate).toBe('INGESTS');
    expect(triple.object).toBe('special offers');
  });

  it('reads a flat MemoryEvidence/TimelineEntry-shaped object the same way', () => {
    const triple = buildTriple({
      relationship_uuid: 'fact-1',
      subject: 'Special Offers',
      predicate: 'located at',
      object: '/content/preview/{locale}/{slug}/MarketingOffers/',
      fact: 'Special Offers is located at /content/preview/{locale}/{slug}/MarketingOffers/'
    });
    expect(triple.subject).toBe('Special Offers');
    expect(triple.object).toBe('/content/preview/{locale}/{slug}/MarketingOffers/');
  });

  it('returns blank strings, never throws, for null/undefined/empty input', () => {
    expect(buildTriple(null)).toEqual({ subject: '', predicate: '', object: '', fact: '' });
    expect(buildTriple(undefined)).toEqual({ subject: '', predicate: '', object: '', fact: '' });
    expect(buildTriple({})).toEqual({ subject: '', predicate: '', object: '', fact: '' });
  });
});

describe('contentTokens', () => {
  it('lowercases, strips punctuation, and drops stopwords', () => {
    expect(contentTokens('The Jedai Knowledge Base.')).toEqual(new Set(['jedai', 'knowledge', 'base']));
  });

  it('collapses repeats and ignores order (a set, not a sequence)', () => {
    expect(contentTokens('special special offers offers')).toEqual(new Set(['special', 'offers']));
  });
});

describe('isRedundantFactSentence', () => {
  it('flags the naive "subject predicate object" restatement as redundant', () => {
    expect(
      isRedundantFactSentence({
        subject: 'Jedai Knowledge Base',
        predicate: 'ingests',
        object: 'Special Offers',
        fact: 'Jedai Knowledge Base ingests Special Offers'
      })
    ).toBe(true);
  });

  it('stays redundant across case, punctuation, and stopword differences', () => {
    expect(
      isRedundantFactSentence({
        subject: 'Jedai Knowledge Base',
        predicate: 'ingests',
        object: 'Special Offers',
        fact: 'The Jedai Knowledge Base ingests the Special Offers.'
      })
    ).toBe(true);
  });

  it('is NOT redundant once the sentence carries a qualifier the triple lacks', () => {
    expect(
      isRedundantFactSentence({
        subject: 'Special Offers',
        predicate: 'located at',
        object: '/content/preview/{locale}/{slug}/MarketingOffers/',
        fact: 'Special Offers is located at /content/preview/{locale}/{slug}/MarketingOffers/ for the en_us locale'
      })
    ).toBe(false);
  });

  it('is NOT redundant when the sentence carries a number/date the triple lacks', () => {
    expect(
      isRedundantFactSentence({
        subject: 'Special Offers',
        predicate: 'observed',
        object: 'GCX',
        fact: 'Special Offers has been observed 3 times since 2026-01-01'
      })
    ).toBe(false);
  });

  it('treats a blank fact as redundant -- there is nothing extra to show', () => {
    expect(isRedundantFactSentence({ subject: 'A', predicate: 'p', object: 'B', fact: '' })).toBe(true);
  });
});

describe('resolveFactEndpoints', () => {
  const nodes = [
    { id: 'entity:a', label: 'Jedai Knowledge Base', node_type: 'entity', labels: ['Component'] },
    { id: 'memory:fact-1', label: 'Jedai Knowledge Base ingests Special Offers', node_type: 'memory' },
    { id: 'entity:b', label: 'Special Offers', node_type: 'entity', labels: ['Resource'] }
  ];
  const edges = [
    { source_id: 'entity:a', target_id: 'memory:fact-1', edge_type: 'subject' },
    { source_id: 'memory:fact-1', target_id: 'entity:b', edge_type: 'object' }
  ];

  it('walks the subject/object reified edges back to the real entity nodes', () => {
    const { subject, object } = resolveFactEndpoints('memory:fact-1', nodes, edges);
    expect(subject?.id).toBe('entity:a');
    expect(object?.id).toBe('entity:b');
  });

  it('returns null endpoints when the payload carries no edges (e.g. /api/archive)', () => {
    const { subject, object } = resolveFactEndpoints('memory:fact-1', nodes, []);
    expect(subject).toBeNull();
    expect(object).toBeNull();
  });

  it('returns null for an unknown fact node id rather than a wrong match', () => {
    const { subject, object } = resolveFactEndpoints('memory:does-not-exist', nodes, edges);
    expect(subject).toBeNull();
    expect(object).toBeNull();
  });
});

describe('endpointLabels', () => {
  it('reads the entity-type labels through the same nodeLabels the graph view colours by', () => {
    expect(endpointLabels({ id: 'entity:a', label: 'X', node_type: 'entity', labels: ['Resource'] })).toEqual([
      'Resource'
    ]);
  });

  it('is null for an unresolved endpoint', () => {
    expect(endpointLabels(null)).toBeNull();
  });
});

describe('collapseFactNodes', () => {
  // Verified against the live 7-entity / 6-fact portal-kb scope: entity
  // labels are always `[entityType, "Tenant"]`, second element the scope
  // marker -- irrelevant here, `collapseFactNodes` only cares that
  // `node_type === 'entity'`.
  const entityA = {
    id: 'entity:a',
    label: 'Jedai Knowledge Base',
    node_type: 'entity',
    labels: ['Component', 'Tenant']
  };
  const entityB = { id: 'entity:b', label: 'Special Offers', node_type: 'entity', labels: ['Resource', 'Tenant'] };
  const fact = {
    id: 'memory:fact-1',
    label: 'Jedai Knowledge Base ingests Special Offers',
    node_type: 'memory',
    relationship_uuid: 'fact-1',
    memory_type: 'anchor',
    properties: {
      predicate: 'ingests',
      subject: 'Jedai Knowledge Base',
      object: 'Special Offers',
      fact: 'Jedai Knowledge Base ingests Special Offers'
    }
  };
  const reificationEdges = [
    { source_id: 'entity:a', target_id: 'memory:fact-1', edge_type: 'subject' },
    { source_id: 'memory:fact-1', target_id: 'entity:b', edge_type: 'object' }
  ];

  it('collapses one entity--subject-->fact--object-->entity chain into one entity->entity edge captioned with its predicate', () => {
    const result = collapseFactNodes([entityA, fact, entityB], reificationEdges);
    expect(result.nodes).toEqual([entityA, entityB]);
    expect(result.edges).toHaveLength(1);
    expect(result.edges[0]).toMatchObject({
      id: 'memory:fact-1',
      source_id: 'entity:a',
      target_id: 'entity:b',
      edge_type: 'ingests',
      label: 'ingests',
      relationship_uuid: 'fact-1'
    });
    // The fact stays fully inspectable -- its own GraphNode, unmutated, rides
    // along on the edge; clicking the edge can still open the Inspector on it.
    expect(result.edges[0].factNode).toBe(fact);
  });

  it('mirrors the live dataset shape: 7 entities / 6 facts collapse to 7 nodes / 6 edges', () => {
    const entities = Array.from({ length: 7 }, (_, index) => ({
      id: `entity:${index}`,
      label: `Entity ${index}`,
      node_type: 'entity',
      labels: ['Resource', 'Tenant']
    }));
    const facts = Array.from({ length: 6 }, (_, index) => ({
      id: `memory:${index}`,
      label: `fact ${index}`,
      node_type: 'memory',
      relationship_uuid: `fact-${index}`,
      memory_type: 'anchor',
      properties: { predicate: `predicate-${index}` }
    }));
    const edges = facts.flatMap((fact, index) => [
      { source_id: `entity:${index % 7}`, target_id: fact.id, edge_type: 'subject' },
      { source_id: fact.id, target_id: `entity:${(index + 1) % 7}`, edge_type: 'object' }
    ]);
    const result = collapseFactNodes([...entities, ...facts], edges);
    expect(result.nodes).toHaveLength(7);
    expect(result.edges).toHaveLength(6);
    expect(result.nodes.every((node) => node.node_type === 'entity')).toBe(true);
  });

  it('falls back to the memory type when predicate is absent', () => {
    const blankPredicateFact = { ...fact, properties: { subject: 'Jedai Knowledge Base', object: 'Special Offers' } };
    const result = collapseFactNodes([entityA, blankPredicateFact, entityB], reificationEdges);
    expect(result.edges[0].edge_type).toBe('anchor');
    expect(result.edges[0].label).toBe('anchor');
  });

  it('falls back to a generic caption when the fact has neither a predicate nor a memory type', () => {
    const bareFact = { id: 'memory:bare', label: 'bare', node_type: 'memory', relationship_uuid: 'bare', properties: {} };
    const bareEdges = [
      { source_id: 'entity:a', target_id: 'memory:bare', edge_type: 'subject' },
      { source_id: 'memory:bare', target_id: 'entity:b', edge_type: 'object' }
    ];
    const result = collapseFactNodes([entityA, bareFact, entityB], bareEdges);
    expect(result.edges[0].label).toBe(UNKNOWN_PREDICATE);
  });

  it('keeps an unresolvable fact as an ordinary node rather than dropping it -- e.g. a payload with no edges at all', () => {
    const result = collapseFactNodes([entityA, fact, entityB], []);
    expect(result.nodes).toContainEqual(fact);
    expect(result.edges).toHaveLength(0);
  });

  it('does not walk past an entity into another fact -- a superseded_by/derived_from chain is not this reification', () => {
    const otherFact = { id: 'memory:fact-2', label: 'other', node_type: 'memory', relationship_uuid: 'fact-2', properties: {} };
    const edgesWithLineage = [
      ...reificationEdges,
      { source_id: 'memory:fact-1', target_id: 'memory:fact-2', edge_type: 'derived_from' }
    ];
    const result = collapseFactNodes([entityA, fact, entityB, otherFact], edgesWithLineage);
    // fact-1 still collapses normally; fact-2 has no subject/object edges of
    // its own, so it is kept rather than mistaken for part of fact-1's chain.
    expect(result.edges).toHaveLength(1);
    expect(result.nodes).toContainEqual(otherFact);
  });

  it('is empty, not broken, for an empty graph', () => {
    expect(collapseFactNodes([], [])).toEqual({ nodes: [], edges: [] });
  });
});
