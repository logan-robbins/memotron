"""T0-11: ``default_config()`` plus a real LLM extractor forms nothing.

The defect
----------
``NodeInstruction.strict_properties`` defaults to ``True``
(``config/_instructions.py:168``) and ``default_config()`` does not override it
(``config/_dream_config.py:346-350``), whose only node instruction allows exactly
five property keys: ``kind, role, tier, region, system`` (plus the universal
``description``/``aliases``).  Any other key quarantines the WHOLE candidate
(``extraction.py:1041-1047``).

Two things make that fire constantly against a real model rather than rarely:

1. The field's own docstring says the opposite of what the default does — *"Set
   False for LLM transports where the model may invent valid-sounding property
   keys that are not in the schema"*.  ``default_config()`` IS the LLM-transport
   path and leaves it ``True``.  ``agent_memory/_config.py:60`` sets ``False``,
   which is why the agent-memory path forms memory and the default SDK path does
   not — the two configs disagree about strictness and only one works with a model.

2. **The allow-list is no longer rendered into the extraction prompt**
   (``config/_instructions.py:816-828``), so the model is never told which five
   keys are legal.  The comment justifying that removal says omission "costs
   nothing" because unknown keys are "silently strip[ped]" — which is true only
   when ``strict_properties=False``.  Under ``default_config()`` it is ``True``,
   so omission costs the entire candidate.

Measured against a live gateway (``TAKEOVER-BACKLOG.md`` T0-11): one four-sentence
factual episode gave ``created_relationships=0, admitted_candidates=0,
quarantined_candidates=3``, every one reading ``property_key_not_allowed`` —
**exit 0, no error, no warning**.

Not "silent", precisely
-----------------------
A quarantine row, a receipt, a negative-space entry and an INFO log all record it,
and quarantine is deliberate: ``extraction.py:292-299`` keeps the row "stored,
receipted, and promotable" so a widened whitelist can re-govern it later without
re-extraction.  That design is sound.  The defect is that **the caller sees
success** — ``run_due_dreams()`` returns cleanly with ``created_relationships=0``,
so an integrator gets a system that queues episodes, reports success, and
remembers nothing.

The fake, and why it is not a tautology
---------------------------------------
:class:`InventsAPropertyKeyExtractionTransport` emits ONE plausible property key
outside the allow-list, which is exactly what a model does — the register's own
reproduction used ``environment``-shaped keys.  A fake that emitted a *reserved*
key would trip a different violation (``PROPERTY_KEY_RESERVED``) and a fake that
emitted no properties at all would pass for the wrong reason.
:func:`test_the_fake_emits_a_key_that_is_genuinely_outside_the_allow_list` pins
that the chosen key is neither allowed nor reserved, derived from
``default_config()`` itself rather than hardcoded, so widening the real allow-list
cannot silently make these tests vacuous.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memotron import (
    Memotron,
    EpisodeType,
    MemoryScope,
    ScopeKind,
)
from memotron.config import default_config

SCOPE = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="t0-11")

#: A property key a model would plausibly emit for an Entity and which
#: ``default_config()`` does not allow. Asserted below rather than assumed.
INVENTED_KEY = "environment"

CANDIDATE: dict[str, Any] = {
    "subject": "Acme Parks",
    "predicate": "requires",
    "object": "SOC2 report",
    "relationship_type": "REQUIRES",
    "confidence": 0.93,
    "subject_properties": {"description": "a support customer", INVENTED_KEY: "production"},
}


class InventsAPropertyKeyExtractionTransport:
    """Returns one good candidate carrying one out-of-allow-list property key."""

    def __init__(self, *, include_invented_key: bool = True) -> None:
        self.include_invented_key = include_invented_key
        self.calls = 0

    async def extract_memories(self, request: Any) -> list[dict[str, Any]]:
        self.calls += 1
        candidate = json.loads(json.dumps(CANDIDATE))
        if not self.include_invented_key:
            candidate["subject_properties"].pop(INVENTED_KEY, None)
        return [candidate]


def allowed_keys() -> set[str]:
    """The keys ``default_config()`` actually permits on an Entity, derived."""
    sets = default_config().instruction_sets
    instruction = next(i for s in sets for i in s.node_instructions if i.label == "Entity")
    return set(instruction.properties) | set(instruction.aliases) | {"description", "aliases"}


def client(tmp_path: Path, transport: Any) -> Memotron:
    return Memotron(
        graph_path=tmp_path / "t0-11.sqlite",
        config=default_config(),
        extraction_transport=transport,
    )


async def ingest(dw: Memotron) -> Any:
    await dw.add_episode(
        name="soc2-requirement",
        episode_body="Acme Parks requires a SOC2 report before the production rollout.",
        source=EpisodeType.MESSAGE,
        scope=SCOPE,
    )
    return await dw.run_due_dreams()


def formed(dw: Memotron) -> list[dict]:
    return [r for r in dw.export_graph()["relationships"] if r["type"] == "REQUIRES"]


# --------------------------------------------------------------------------- the fake
def test_the_fake_emits_a_key_that_is_genuinely_outside_the_allow_list() -> None:
    """Derived from default_config(), so widening the real list makes this fail
    loudly rather than making the tests below quietly vacuous."""
    allowed = allowed_keys()
    assert INVENTED_KEY not in allowed, (
        f"{INVENTED_KEY!r} is now an allowed Entity property ({sorted(allowed)}); "
        "pick another out-of-list key or these tests prove nothing"
    )
    assert "description" in allowed, "the control key must be allowed, or the control proves nothing"


# ------------------------------------------------------------------- the defect (T0-11)
@pytest.mark.asyncio
async def test_an_invented_property_key_does_not_cost_the_whole_memory(tmp_path: Path) -> None:
    """T0-11. RED until ``default_config()`` stops being strict for an LLM transport.

    A model invents one plausible property key on an otherwise perfect candidate.
    The memory must still form; the unknown key may be dropped.
    """
    transport = InventsAPropertyKeyExtractionTransport()
    dw = client(tmp_path, transport)

    await ingest(dw)

    assert transport.calls, "the extractor never ran — the test proves nothing"
    assert formed(dw), (
        "one invented property key quarantined the entire candidate. This is T0-11: "
        "default_config() is strict while the prompt no longer tells the model which "
        "five keys are legal, so a real extractor forms nothing."
    )


@pytest.mark.asyncio
async def test_the_same_candidate_forms_when_every_key_is_allowed(tmp_path: Path) -> None:
    """The control, and it must pass BEFORE the fix as well as after.

    If this ever goes red the failure is in the harness or in formation generally,
    not in T0-11 — the distinction a bare red test cannot make.
    """
    transport = InventsAPropertyKeyExtractionTransport(include_invented_key=False)
    dw = client(tmp_path, transport)

    await ingest(dw)

    assert formed(dw), "formation failed with only ALLOWED keys — the harness is broken, not T0-11"


@pytest.mark.asyncio
async def test_a_caller_can_tell_that_nothing_was_admitted(tmp_path: Path) -> None:
    """The corrected statement of the defect: not that it is unlogged, but that
    ``run_due_dreams()`` returns cleanly and a caller cannot tell.

    Kept separate from the fix above because it stays true either way — whatever
    strictness is set to, a run that admits nothing must be distinguishable from a
    run that had nothing to do.
    """
    transport = InventsAPropertyKeyExtractionTransport()
    dw = client(tmp_path, transport)

    result = await ingest(dw)

    admitted = sum(getattr(run, "created_relationships", 0) for run in result.job_runs)
    quarantined = sum(getattr(run, "quarantined_candidates", 0) for run in result.job_runs)
    assert admitted or quarantined, (
        "the run reported neither an admitted nor a quarantined candidate, so a caller "
        "cannot distinguish 'nothing to do' from 'everything refused'"
    )
