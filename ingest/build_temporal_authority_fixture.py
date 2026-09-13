"""WS-25 T5: build the temporal-authority gold-set fixture graph.

Writes a tiny, deterministic, hermetic SQLite graph exercising the exact
regression the ``temporal_authority`` section of
``ingest/actionability_goldset.yaml`` scores against:

  1. A ``preference`` fact ("user prefers dark mode") is observed three times
     -- reaching ``observed_count == SupersessionPolicy.corroboration_margin``
     (3 by default), i.e. corroboration-eligible.
  2. A later, equal-authority, contradictory observation ("user prefers not
     dark mode") arrives.

Pre-WS-25, step 2 PARKS (``insufficient_corroboration``): the corroborated
incumbent stays current truth and the fresher, equally-authoritative
correction is invisible to any reader.  WS-25 T2 (a ``preference`` is a
recency-authoritative type) auto-closes the incumbent instead, so the newer
statement becomes -- and stays -- current truth.  This is "two episodes" in
the sense the WS-25 plan names (a preference is SET, then CONTRADICTED); the
three initial writes are one logical "set" moment reaching the corroboration
threshold the regression needs to bite on, not three independent episodes of
narrative content.

Standalone, hermetic, and zero network calls: the default local embedding
transport and rule-based extraction fallback are used throughout via
``Memotron.add_memory`` (the client-managed path, which validates and
materializes immediately -- no LLM call, no dream job).

Usage
-----
    uv run ingest/build_temporal_authority_fixture.py

Rebuilds ``ingest/.memotron/temporal_authority_fixture.sqlite`` from
scratch every time (deletes any existing file first) so the fixture can never
silently drift from this script.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from memotron import Memotron, MemoryScope, ScopeKind

INGEST_DIR = Path(__file__).resolve().parent
GRAPH_DIR = INGEST_DIR / ".memotron"
FIXTURE_PATH = GRAPH_DIR / "temporal_authority_fixture.sqlite"

SCOPE = MemoryScope(kind=ScopeKind.USER, scope_id="temporal-authority-fixture")
T0 = datetime(2026, 8, 1, tzinfo=UTC)


async def build() -> None:
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    if FIXTURE_PATH.exists():
        FIXTURE_PATH.unlink()

    client = Memotron(graph_path=FIXTURE_PATH)

    # Step 1: set the preference, reinforced to corroboration_margin (3).
    incumbent_uuid: str | None = None
    for i in range(3):
        result = await client.add_memory(
            subject="user",
            predicate="prefers",
            object="dark mode",
            relationship_type="PREFERS",
            scope=SCOPE,
            reference_time=T0 + timedelta(hours=i),
        )
        incumbent_uuid = result.relationship_uuid

    incumbent = client.graph.get_relationship(incumbent_uuid)
    assert incumbent.properties["observed_count"] == 3, (
        "fixture invariant broken: expected the reinforced incumbent to reach "
        f"observed_count=3, got {incumbent.properties['observed_count']!r}"
    )

    # Step 2: the later, equal-authority contradiction.
    contradiction = await client.add_memory(
        subject="user",
        predicate="prefers",
        object="not dark mode",
        relationship_type="PREFERS",
        scope=SCOPE,
        reference_time=T0 + timedelta(days=1),
    )

    contradiction_row = client.graph.get_relationship(contradiction.relationship_uuid)
    incumbent_after = client.graph.get_relationship(incumbent_uuid)
    print(f"wrote {FIXTURE_PATH}")
    print(f"  incumbent  {incumbent_uuid}  status={incumbent_after.properties['status']!r}")
    print(f"  contradiction  {contradiction.relationship_uuid}  status={contradiction_row.properties['status']!r}")


def main() -> int:
    asyncio.run(build())
    return 0


if __name__ == "__main__":
    sys.exit(main())
