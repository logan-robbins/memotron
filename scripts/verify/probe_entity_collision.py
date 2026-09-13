"""Do two DISTINCT real-world entities that share a proper name collapse onto one node?

Raised in design discussion (2026-08-31): ingesting email threads that mention two different
people both called "Matt" should not fuse them. Node identity is name-keyed --
``dreaming/_identity.py:142`` builds ``f"{scope_key}:{label}:{name}"`` -- so the concern is
structural, not a small-model extraction artifact.

Calling that key function twice with the same name obviously collides, and proves nothing about
the system. This probe asks the harder question: does anything in the WRITE PATH -- node upsert,
the alias registry, truth-slot computation -- put the two apart again? It drives the public SDK
(``add_memory``) rather than the key function, and deliberately avoids the LLM extractor so the
answer is a fact about identity rather than about a model's naming.

Two consequences are measured separately; they differ in severity:

  1. NODE FUSION      -- both people become one graph node.
  2. TRUTH-SLOT LOSS  -- the truth key for a single-active predicate is
                         ``scope:subject:predicate`` with no object, so the second person's
                         fact SUPERSEDES the first person's. Silent data loss, not a wart.

``REQUIRES`` is used because the default instruction set declares it ``single_active``
(memory_type ``requirement``); ``PREFERS`` is ``multi_active`` and is the control predicate.

The label plane contributes nothing by default -- see `_report_label_vocabulary`. The default
instruction set permits exactly ONE node label, so ``{label}`` is constant across every entity
and the identity key reduces to scope + name. That widens the collision from "two people who
share a name" to "any two THINGS that share a name", regardless of kind.

Why the two control arms are load-bearing
-----------------------------------------
An arm reporting "1 node" is worthless unless the probe can report 2 at all, and unless one node
is known to be CORRECT for a genuine single entity. So:

  * ``distinct-names`` must yield 2 nodes -- proves the counter is not hardwired to 1.
  * ``same-entity``   must yield 1 node and keep BOTH facts -- proves the store does not simply
                      drop concurrent writes, so arm 3's collapse is about identity.

If either control misbehaves the probe reports INCONCLUSIVE (exit 2) rather than a verdict.
A probe that cannot fail is not evidence.

Each arm runs against its own fresh graph file, so no arm can seed another.

    uv run python scripts/verify/probe_entity_collision.py

exit 0 = distinct same-named entities stay distinct  ·  1 = they collapse  ·  2 = inconclusive
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("MEMOTRON_ALLOW_EPHEMERAL_KEK", "1")

from memotron import Memotron, MemoryScope, ScopeKind

SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id="probe-entity-collision")

#: The default instruction set allows exactly one label, so this is not a free choice --
#: passing anything else raises CandidateSchemaError. That constraint is the finding.
LABEL = "Entity"


def _new_client(tmp: str) -> Memotron:
    return Memotron(graph_path=Path(tmp) / "graph.sqlite")


def _report_label_vocabulary() -> int:
    """How much discrimination does the label plane actually provide by default?"""
    with tempfile.TemporaryDirectory() as tmp:
        dw = _new_client(tmp)
        iset = dw.config.instruction_sets[0]
        labels = [n.label for n in iset.node_instructions]
        rels = [(r.type, str(r.cardinality)) for r in iset.relationship_instructions]
    print(f"  instruction set {iset.name!r}: {len(labels)} node label(s) {labels}")
    for rel_type, card in rels:
        print(f"    {rel_type:9s} {card}")
    if len(labels) == 1:
        print(f"  -> {{label}} is constant ({labels[0]!r}), so identity is effectively scope + name.")
        print("     Any two things sharing a name collide, not only two people.")
    return len(labels)


async def _arm(name: str, writes: list[tuple[str, str, str, str]]) -> tuple[int, int, list[str]]:
    """Apply `writes` to a fresh graph; return (node count, active rel count, active facts).

    A write is (subject, predicate, object, relationship_type).
    """
    with tempfile.TemporaryDirectory() as tmp:
        dw = _new_client(tmp)
        for subject, predicate, obj, rel_type in writes:
            await dw.add_memory(
                subject=subject,
                predicate=predicate,
                object=obj,
                relationship_type=rel_type,
                scope=SCOPE,
                subject_label=LABEL,
                object_label=LABEL,
            )
        subjects = {w[0] for w in writes}
        nodes = [n for n in dw.graph.nodes_for_scope(SCOPE.key) if n.properties.get("name") in subjects]
        rels = dw.graph.relationships_for_scope(SCOPE.key)
        # valid_to is None => still true. A superseded fact is closed, not deleted.
        active = [r for r in rels if r.valid_to is None]
        facts = sorted(str(r.properties.get("fact", ""))[:70] for r in active)
        print(f"\n  arm {name}:")
        print(f"    subject nodes: {len(nodes)}  {sorted(str(n.properties.get('name', '?')) for n in nodes)}")
        print(f"    active relationships: {len(active)} of {len(rels)} written")
        for f in facts:
            print(f"      · {f}")
        return len(nodes), len(active), facts


async def main() -> int:
    print("  ---- label vocabulary ----")
    _report_label_vocabulary()

    print("\n  ---- controls ----")
    # Two entities with DIFFERENT names must produce TWO nodes.
    ctl_nodes, _, _ = await _arm(
        "distinct-names (control)",
        [
            ("Matthew", "requires", "design review", "REQUIRES"),
            ("Roberta", "requires", "food safety certification", "REQUIRES"),
        ],
    )
    if ctl_nodes != 2:
        print(f"\n  INCONCLUSIVE: control produced {ctl_nodes} node(s) for two distinct names, expected 2.")
        print("  The counter cannot distinguish entities at all, so the collision arm proves nothing.")
        return 2

    # ONE genuine entity, two different predicates -> one node, both facts retained.
    same_nodes, same_active, _ = await _arm(
        "same-entity (control)",
        [
            ("Matt", "requires", "design review", "REQUIRES"),
            ("Matt", "prefers", "dark mode", "PREFERS"),
        ],
    )
    if same_nodes != 1 or same_active != 2:
        print(f"\n  INCONCLUSIVE: one entity gave {same_nodes} node(s) and {same_active} active fact(s);")
        print("  expected 1 and 2. Cannot separate an identity collapse from a dropped write.")
        return 2

    print("\n  ---- the question ----")
    # Two DIFFERENT people, same proper name, same single-active predicate.
    col_nodes, col_active, col_facts = await _arm(
        "same-name-different-people",
        [
            ("Matt", "requires", "design review", "REQUIRES"),  # Matt the architect
            ("Matt", "requires", "food safety certification", "REQUIRES"),  # a different Matt
        ],
    )

    print("\n  ---- verdict ----")
    fused = col_nodes == 1
    lost = col_active < 2
    print(f"  node fusion:     {'YES' if fused else 'no'} ({col_nodes} node(s) for two distinct people)")
    print(f"  truth-slot loss: {'YES' if lost else 'no'} ({col_active} active fact(s) of 2 written)")
    if lost:
        print("    the survivor is the LAST writer; the other person's requirement was superseded:")
        for f in col_facts:
            print(f"      · {f}")

    if fused or lost:
        print("\n  COLLISION CONFIRMED. Identity is the surface name")
        print("  (dreaming/_identity.py:142 -> f'{scope_key}:{label}:{name}'), and nothing in the")
        print("  write path re-separates two entities that share one. The alias registry")
        print("  (config/_policies.py:210) only ever MERGES surface forms, and no split/unmerge")
        print("  operation exists.")
        print("  Scope of the damage, stated precisely: the superseded row is CLOSED, not")
        print("  deleted (invalidate-don't-delete), so the history survives and the loss is to")
        print("  CURRENT TRUTH -- a reader asking what Matt requires gets one person's answer.")
        print("  The NODE fusion is the unrecoverable half: one node, two entities, no split.")
        return 1

    print("\n  No collision: something in the write path distinguished them. Find it before")
    print("  designing a disambiguator -- this probe's premise would be wrong.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        sys.exit(2)
