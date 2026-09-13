"""T1-18: does `remediate_coherence` report anti-windup holds it never applied?

Filed claim (AGENT-SOURCED): the REPORT predicate (`client/_lifecycle.py:465` `remediate_coherence`, reporting at `:559`) omits the
attribution-confidence conjunct that the WRITER applies (`dreaming/_formation.py:176` (the `min_attribution_confidence` conjunct)), so incidents
below `min_attribution_confidence` are counted as held while nothing is written.

Confirmed by arithmetic before running it. `attribution_confidence = 0.65*cos + 0.35*min(1,
gap/precedence)` (`coherence.py:436-439`); with default class precedence skill=30 memory=10 the
second term is a constant 0.2333, so confidence crosses the 0.70 default gate at cosine 0.7179.
The detector admits anything above `identity_threshold` 0.50. **Phantom window: cosine in
[0.500, 0.718).**

This probe drives one incident INTO that window and one ABOVE it, and compares what the report
says against what the graph actually holds. The above-window arm is the control: without it, a
report/graph mismatch could just mean holds never get written at all.

    uv run python scripts/verify/probe_phantom_hold.py

exit 0 = the report matches the graph  ·  1 = the report counts a hold that was never applied
exit 2 = the control arm did not write a hold either, so no arm is interpretable

CITATIONS REPAIRED 2026-08-31: every `file.py:NNN` below was rewritten after the module
split dissolved the god files they named. Each now carries the SYMBOL as well as the line,
so the next move makes them findable by grep rather than silently wrong.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from memotron import Memotron, MemoryScope, ScopeKind
from memotron.coherence import StaticArtifactSource
from memotron.config import CoherencePolicy
from memotron.models import ArtifactClass, ArtifactDirective, PersistentArtifact

NOW = datetime(2026, 8, 27, tzinfo=UTC)
MEMORY_AT = NOW - timedelta(days=30)
MEMORY_SUBJECT = "retry policy for the payment worker"

# escalation_cycles_threshold=1 because add_memory forces observed_count=1 (coherence.py:326
# would otherwise gate every arm out). Held identical across both arms.
POLICY = CoherencePolicy(escalation_cycles_threshold=1)

# Measured, not guessed: cos~0.675 -> confidence 0.672 (below the 0.70 gate, inside the window)
IN_WINDOW = "retry behaviour of the payment worker"
# cos~1.000 -> confidence 0.883 (above the gate) -- the control that proves holds DO get written
ABOVE_WINDOW = MEMORY_SUBJECT


async def _confidence_only(artifact_subject: str) -> float:
    """Read attribution_confidence on a disposable graph, so the arm under test stays clean."""
    tmp = Path(tempfile.mkdtemp(prefix="dwhold-c-"))
    dw = Memotron(graph_path=str(tmp / "g.sqlite"))
    scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="probe")
    await dw.add_memory(
        subject="the payment worker",
        predicate="requires",
        object=MEMORY_SUBJECT,
        relationship_type="REQUIRES",
        scope=scope,
        valid_from=MEMORY_AT,
        metadata={"directive_stance": "require"},
    )
    dw.register_artifact_source(
        StaticArtifactSource(
            [
                PersistentArtifact(
                    artifact_id="payment-retry-skill",
                    artifact_class=ArtifactClass.SKILL,
                    scope=scope,
                    author="platform-team",
                    updated_at=NOW - timedelta(days=200),
                    directives=[
                        ArtifactDirective(subject=artifact_subject, instruction="retry the worker three times")
                    ],
                )
            ]
        )
    )
    scan = await dw.run_coherence_scan(scope=scope, now=NOW, policy=POLICY)
    return scan.incidents[0].attribution_confidence if scan.incidents else 0.0


async def arm(label: str, artifact_subject: str) -> tuple[float, int, int]:
    """Return (confidence, holds the report claims, holds actually in the graph)."""
    tmp = Path(tempfile.mkdtemp(prefix="dwhold-"))
    dw = Memotron(graph_path=str(tmp / "g.sqlite"))
    scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="phantom")
    await dw.add_memory(
        subject="the payment worker",
        predicate="requires",
        object=MEMORY_SUBJECT,
        relationship_type="REQUIRES",
        scope=scope,
        valid_from=MEMORY_AT,
        metadata={"directive_stance": "require"},
    )
    dw.register_artifact_source(
        StaticArtifactSource(
            [
                PersistentArtifact(
                    artifact_id="payment-retry-skill",
                    artifact_class=ArtifactClass.SKILL,
                    scope=scope,
                    author="platform-team",
                    updated_at=NOW - timedelta(days=200),
                    directives=[
                        ArtifactDirective(subject=artifact_subject, instruction="retry the worker three times")
                    ],
                )
            ]
        )
    )
    # CONTAMINATION CAUGHT ON THE FIRST RUN: calling run_coherence_scan here to read the
    # confidence APPLIES the hold, and remediate_coherence then runs its own scan which skips
    # the now-held directive via the idempotency path (coherence.py:329). That made the control
    # arm report held=0 / graph=1 -- a mismatch manufactured entirely by the probe. The
    # confidence is therefore measured on a THROWAWAY instance, and the arm under test has
    # exactly one scan run against it: the one inside remediate_coherence.
    confidence = await _confidence_only(artifact_subject)

    report = await dw.remediate_coherence(scopes=[scope], dry_run=False, policy=POLICY, now=NOW)
    claimed = getattr(report, "total_held", None)
    if claimed is None:
        claimed = sum(len(getattr(s, "held_directives", []) or []) for s in getattr(report, "scopes", []) or [])
    actual = sum(
        1
        for r in dw.graph.active_relationships(scope=scope)
        if r.type != "MENTIONS" and r.properties.get("coherence_hold")
    )
    print(
        f"    {label:14s} confidence={confidence:.3f}  report says held={claimed}  "
        f"graph holds={actual}"
        f"{'   <- PHANTOM' if claimed and not actual else ''}"
    )
    return confidence, int(claimed or 0), actual


async def main() -> int:
    print("\n  gate is min_attribution_confidence=0.70; phantom window is cosine [0.500, 0.718)\n")
    _, _above_claimed, above_actual = await arm("above window", ABOVE_WINDOW)
    conf, in_claimed, in_actual = await arm("in window", IN_WINDOW)

    print()
    if above_actual == 0:
        print("  === INCONCLUSIVE ===")
        print("  The control arm wrote no hold either, so 'no hold in the window' means nothing.")
        return 2
    if in_claimed > 0 and in_actual == 0:
        print("  === CONFIRMED: the report counts a hold that was never applied ===")
        print(f"  At confidence {conf:.3f} the writer's conjunct (dreaming/_formation.py:176) fails, so no")
        print("  `coherence_hold` is written -- but client/_lifecycle.py:559 counts the incident anyway.")
        print("  Aggravator: with no hold written, the idempotency skip at coherence.py:329 never")
        print("  fires, so the same incident re-raises and re-reports this phantom every sweep.")
        return 1
    print("  === REFUTED: report and graph agree ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
