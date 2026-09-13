"""T0-13: is an artifact with no `updated_at` treated as staler than every memory?

Filed claim (AGENT-SOURCED): `coherence.py:338` reads
`stale = other.updated_at is None or (...)`, so an artifact that never declared when it was
updated wins the staleness comparison against every memory unconditionally -- holding and
soft-retiring memory that is actually correct.

**The asymmetry is the finding, and it is sharper than filed.** Memory directives get
`updated_at` BACKFILLED three ways (`coherence.py:181-184`: reinforced_at -> last_seen_at ->
relationship.valid_from) so a memory directive can never be None. Artifact directives take
`artifact.updated_at` raw (`:248`). The durable registration path rejects None outright
(`storage/sqlite/_artifacts.py:45`) and the MCP tool requires `updated_at_iso` (`mcp_server.py:957`) -- but the
in-memory `register_artifact_source` path does not, so only that path can produce the state.

**The experiment holds everything constant but `updated_at`.** Same scope, same memory, same
artifact text, same engine, three arms:

    None                 -> if this raises a windup incident, the claim is real
    NEWER than memory    -> control: must NOT raise (the artifact is fresh, memory is stale)
    OLDER than memory    -> control: must raise (this is the detector working as designed)

Only the third arm proves the detector is alive at all; without it, an incident in arm 1 could
just be a detector that fires on everything.

    uv run python scripts/verify/probe_artifact_staleness.py

exit 0 = None behaves like a fresh artifact (claim refuted)
exit 1 = None is treated as unconditionally stale (claim confirmed)
exit 2 = the detector never fired on the OLDER control, so no arm is interpretable

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

SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id="staleness")
SUBJECT = "retry policy for the payment worker"
NOW = datetime(2026, 8, 27, tzinfo=UTC)
MEMORY_AT = NOW - timedelta(days=30)

# escalation_cycles_threshold gates the whole detector on observed_count (coherence.py:326),
# and add_memory forces observed_count=1 regardless of supplied metadata. Lowering the
# threshold to 1 is what lets any arm reach the staleness comparison at all -- it is held
# IDENTICAL across all three arms, so it cannot explain a difference between them.
POLICY = CoherencePolicy(escalation_cycles_threshold=1)


def artifact(updated_at: datetime | None) -> PersistentArtifact:
    return PersistentArtifact(
        artifact_id="payment-retry-skill",
        artifact_class=ArtifactClass.SKILL,
        scope=SCOPE,
        author="platform-team",
        updated_at=updated_at,
        directives=[ArtifactDirective(subject=SUBJECT, instruction="retry the payment worker three times")],
    )


async def arm(label: str, updated_at: datetime | None) -> int:
    """Return the escalation-windup incident count for one artifact staleness value."""
    tmp = Path(tempfile.mkdtemp(prefix="dwstale-"))
    dw = Memotron(graph_path=str(tmp / "g.sqlite"))
    # The memory directive. memory_type must be in participating_memory_types
    # (config/_policies.py:399 `participating_memory_types` -> "directive", "requirement") or it is never projected at all.
    await dw.add_memory(
        subject="the payment worker",
        predicate="requires",
        object=SUBJECT,
        relationship_type="REQUIRES",
        scope=SCOPE,
        # valid_from pinned: add_memory drops `reinforced_at`, so the projection falls back to
        # relationship.valid_from (coherence.py:184). Left unpinned that is the real wall clock,
        # which would make the "newer" control (NOW-1d) OLDER than the memory and silently
        # invert the experiment.
        valid_from=MEMORY_AT,
        metadata={"directive_stance": "require"},
    )
    dw.register_artifact_source(StaticArtifactSource([artifact(updated_at)]))
    report = await dw.run_coherence_scan(scope=SCOPE, now=NOW, policy=POLICY)
    n = report.escalation_windup_count
    shown = updated_at.date().isoformat() if updated_at else "None"
    print(f"    artifact updated_at={shown:12s} -> escalation_windup_count={n}")
    return n


async def consequence() -> None:
    """With updated_at=None, does the correct memory survive remediation?"""
    tmp = Path(tempfile.mkdtemp(prefix="dwstale-c-"))
    dw = Memotron(graph_path=str(tmp / "g.sqlite"))
    await dw.add_memory(
        subject="the payment worker",
        predicate="requires",
        object=SUBJECT,
        relationship_type="REQUIRES",
        scope=SCOPE,
        valid_from=MEMORY_AT,
        metadata={"directive_stance": "require"},
    )
    dw.register_artifact_source(StaticArtifactSource([artifact(None)]))
    before = [r for r in dw.graph.active_relationships(scope=SCOPE) if r.type != "MENTIONS"]
    report = await dw.run_coherence_scan(scope=SCOPE, now=NOW, policy=POLICY)
    inc = report.incidents[0]
    frag = inc.attribution_rationale
    i = frag.find("last updated")
    print(f"    attribution_confidence  {inc.attribution_confidence:.3f}  (gate is 0.70)")
    print(f"    rationale says          ...{frag[i : i + 62]}...")
    await dw.remediate_coherence(scopes=[SCOPE], dry_run=False, retire_held_directives=True, policy=POLICY, now=NOW)
    after = [r for r in dw.graph.active_relationships(scope=SCOPE) if r.type != "MENTIONS"]
    print(
        f"    active behavioral rows  before={len(before)} after={len(after)}"
        f"{'   <- the correct memory was retired' if len(after) < len(before) else ''}"
    )


async def main() -> int:
    print(f"\n  memory directive dated {MEMORY_AT.date()}; only artifact updated_at varies\n")
    none_n = await arm("none", None)
    newer_n = await arm("newer", NOW - timedelta(days=1))  # fresher than the memory
    older_n = await arm("older", NOW - timedelta(days=200))  # staler than the memory

    # Downstream: an incident is only a finding if it actually costs the user a memory.
    if none_n > 0:
        print()
        await consequence()

    print()
    if older_n == 0:
        print("  === INCONCLUSIVE ===")
        print("  The OLDER control raised no incident, so the windup detector never fired at all")
        print("  in this setup. Arm 1 returning 0 would then mean nothing. Not a result either way.")
        return 2
    if none_n > 0 and newer_n == 0:
        print("  === CONFIRMED: updated_at=None is treated as unconditionally stale ===")
        print(f"  None raised {none_n} incident(s); an artifact updated YESTERDAY raised {newer_n}.")
        print("  Same memory, same artifact text, same engine -- only the timestamp differed.")
        return 1
    if none_n == 0:
        print("  === REFUTED: None behaves like a fresh artifact, not a stale one ===")
        return 0
    print("  === UNEXPECTED: the NEWER control also raised an incident ===")
    print("  The detector may not be discriminating on staleness at all -- investigate before")
    print("  reporting either way.")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
