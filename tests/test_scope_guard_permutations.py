"""Call every scope-taking entry point with a scope it is not allowed to touch.

The behavioural half of T3-9. ``test_scope_guard_completeness.py`` proves a guard is
*called*; this proves it *refuses*. Both are needed and neither implies the other — a
guard can be called and still return without raising.

Why this is generated rather than written
-----------------------------------------
The suite already had negative tests for the scope guard. They were a literal list in
``tests/test_promotion.py`` — ``attempts = [...]`` — which stops at its first failure
and reports as a single test, and which nobody updated when ``_archive.py`` arrived.
That is T3-8's root cause. The population here is derived from the class, so a new
entry point is covered the moment it is written, and it is parametrized so every case
reports independently.

Two things measured while building this, both of which changed the test
----------------------------------------------------------------------
**A random UUID is not good enough.** Seven relationship-addressed methods
(``pin_memory``, ``unpin_memory``, ``forget_memory``, ``correct_memory``,
``memory_evidence``, ``set_memory_visibility``, ``redact_relationship_version``) check
that the relationship EXISTS before they check the scope, so a synthetic UUID gets
``relationship does not exist`` and the guard is never reached. Probing with one would
have "passed" seven methods while exercising nothing.

So the fixture seeds a **real relationship in the foreign scope** and passes its UUID.
Measured with that in place: all seven refuse the scope. That is a genuine negative
result for the IDOR shape T3-10(a) worries about, at the relationship-addressed
surface — the scope check wins over the row lookup.

**An empty collection makes the probe vacuous.** ``add_artifacts`` is a bulk loop over
``add_artifact``; called with ``artifacts=[]`` the loop body never runs, so no guard is
reached. Not a bypass — nothing is read or written — but a probe passing ``[]`` proves
nothing, so it is asserted through its singular form instead.

What this does NOT cover
------------------------
The unauthorized-scope axis and the mixed multi-scope case only. ``scope=None``,
read-only-scope interaction, and per-argument permutations are still owed on T3-9.
"""

from __future__ import annotations

import inspect
import pathlib
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from memotron import DreamJob, DreamJobKind, GrowthPolicy
from memotron.client import Memotron
from memotron.client._ingest import IngestMixin
from memotron.config import PruningPolicy, RetentionPolicy, default_config
from memotron.models import MemoryScope

# tests/ is not a package; pytest puts this directory on sys.path via its conftest.
from test_scope_guard_completeness import KNOWN_UNGUARDED, _public_scope_methods, _self_call_graph

AUTHORIZED = MemoryScope(kind="user", identifier="ok", scope_id="ok")
FOREIGN = MemoryScope(kind="user", identifier="nope", scope_id="nope")

#: Guarded only through its singular form -- see the module docstring.
BULK_DELEGATES = {"add_artifacts": "add_artifact"}


def _refuses_scope(exc: BaseException) -> bool:
    text = str(exc)
    return isinstance(exc, ValueError) and ("authorized_scope" in text or "outside this client" in text)


async def _invoke(client: Memotron, name: str, kwargs: dict[str, Any]) -> BaseException | None:
    """Call it and hand back whatever it raised, or None.

    Exists so no assertion ever sits inside an ``except`` block: the classification of
    the exception is the point of these tests, and burying it in a handler makes a
    failure report the handler rather than the method.
    """
    try:
        outcome = getattr(client, name)(**kwargs)
        if inspect.isawaitable(outcome):
            await outcome
    except BaseException as exc:
        return exc
    return None


@pytest.fixture(scope="module")
async def seeded(tmp_path_factory: pytest.TempPathFactory) -> tuple[Memotron, str]:
    """A client authorized for AUTHORIZED only, plus a REAL relationship UUID in FOREIGN.

    Module-scoped deliberately. Function scope costs 2.14s against 0.09s and would add
    ~3,500 storage calls and 81 receipts to the two goldens, for one property: stable
    per-test attribution.

    The cost of module scope is that the fixture's ~43 storage calls and its one receipt
    are attributed to whichever parametrized case pytest runs first (today
    ``[activate_policy_alias]``, because the population is sorted). Add or remove a
    scope-taking method and those calls MOVE to a different row, which
    ``storage_golden.py`` reports as a changed row rather than a new one.

    That is acceptable and not a wolf-cry, because it can only happen in a commit that
    adds or removes a scope-taking method -- which changes the set of generated test IDs
    and therefore requires re-blessing both goldens anyway. The noise lands inside a
    re-bless that was already necessary.
    """
    graph = str(tmp_path_factory.mktemp("scopeguard") / "graph.sqlite")
    omniscient = Memotron(graph_path=graph)  # deliberately no authorized_scope_keys
    result = await omniscient.add_memory(
        scope=FOREIGN,
        subject="bob",
        predicate="PREFERS",
        object="a value only bob should see",
        relationship_type="PREFERS",
    )
    relationship_uuid = getattr(result, "relationship_uuid", None) or getattr(result, "uuid", None)
    assert relationship_uuid, "fixture could not seed a relationship -- every probe would be vacuous"
    restricted = Memotron(graph_path=graph, authorized_scope_keys={AUTHORIZED.key})
    return restricted, str(relationship_uuid)


_ANNOTATION_DEFAULTS: tuple[tuple[str, Any], ...] = (
    ("MemoryScope", FOREIGN),
    ("bool", False),
    ("int", 1),
    ("float", 1.0),
    ("str", "x"),
    ("list", []),
    ("Sequence", []),
    ("tuple", ()),
    ("dict", {}),
    ("Mapping", {}),
    ("set", set()),
)


def _arguments(name: str, relationship_uuid: str, *, scopes_value: list[MemoryScope]) -> dict[str, Any]:
    """Minimal keyword arguments: the scope under test, plus a filler for each required param."""
    signature = inspect.signature(inspect.getattr_static(Memotron, name))
    out: dict[str, Any] = {}
    for param, spec in signature.parameters.items():
        if param == "self":
            continue
        if param == "scope":
            out[param] = FOREIGN
            continue
        if param == "scopes":
            out[param] = scopes_value
            continue
        if spec.default is not inspect.Parameter.empty:
            continue
        if param == "relationship_uuid":
            out[param] = relationship_uuid
            continue
        if param.endswith("uuid"):
            out[param] = str(uuid.uuid4())
            continue
        annotation = str(spec.annotation)
        out[param] = next((v for probe, v in _ANNOTATION_DEFAULTS if probe in annotation), None)
    return out


def _takes_scopes(name: str) -> bool:
    return "scopes" in inspect.signature(inspect.getattr_static(Memotron, name)).parameters


GUARDED = [m for m in _public_scope_methods() if m not in KNOWN_UNGUARDED and m not in BULK_DELEGATES]
MULTI_SCOPE = [m for m in GUARDED if _takes_scopes(m)]


def test_the_generated_population_is_substantial() -> None:
    """A table that silently generates zero cases would pass every parametrized test below."""
    assert len(GUARDED) > 50, f"only {len(GUARDED)} cases generated -- derivation broke"
    assert MULTI_SCOPE, "no multi-scope methods found -- the laundering test would be vacuous"


@pytest.mark.parametrize("method_name", GUARDED)
async def test_refuses_an_unauthorized_scope(seeded: tuple[Memotron, str], method_name: str) -> None:
    client, relationship_uuid = seeded
    kwargs = _arguments(method_name, relationship_uuid, scopes_value=[FOREIGN])

    exc = await _invoke(client, method_name, kwargs)

    assert exc is not None, (
        f"{method_name} accepted scope {FOREIGN.key!r} while the client is authorized only "
        f"for {AUTHORIZED.key!r}. That is a scope bypass."
    )
    assert _refuses_scope(exc), (
        f"{method_name} raised {type(exc).__name__} instead of refusing the scope: {exc}. "
        "If it validates arguments before it checks the scope, give the fixture what it "
        "needs -- do not weaken this assertion."
    )


@pytest.mark.parametrize("method_name", MULTI_SCOPE)
async def test_refuses_a_mixed_scope_list(seeded: tuple[Memotron, str], method_name: str) -> None:
    """One authorized scope must not launder an unauthorized one alongside it.

    A guard that checks only ``scopes[0]``, or that passes when *any* scope is allowed,
    satisfies the test above and fails here.
    """
    client, relationship_uuid = seeded
    kwargs = _arguments(method_name, relationship_uuid, scopes_value=[AUTHORIZED, FOREIGN])

    exc = await _invoke(client, method_name, kwargs)

    assert exc is not None, (
        f"{method_name} accepted [{AUTHORIZED.key}, {FOREIGN.key}] -- an authorized scope laundered a foreign one."
    )
    assert _refuses_scope(exc), f"{method_name} raised {type(exc).__name__} instead of refusing: {exc}"


async def test_bulk_delegates_are_guarded_through_their_singular_form(
    seeded: tuple[Memotron, str],
) -> None:
    """``add_artifacts([])`` reaches no guard because the loop body never runs."""
    client, relationship_uuid = seeded
    population = _public_scope_methods()
    for bulk, singular in BULK_DELEGATES.items():
        assert bulk in population, f"{bulk} no longer exists -- drop it from BULK_DELEGATES"
        exc = await _invoke(client, singular, _arguments(singular, relationship_uuid, scopes_value=[FOREIGN]))
        assert exc is not None, f"{singular} accepted a foreign scope"
        assert _refuses_scope(exc), f"{singular} did not refuse the scope: {exc}"


# ---------------------------------------------------------------------------------
# Axis 2: omitting the scope argument entirely.
#
# `_require_authorized_scope` skips None entries, so a method whose only guard takes
# the REQUESTED scope is unguarded whenever the caller omits it. The guard's own
# docstring claims row-addressed APIs avoid this by guarding the RESOLVED row scope --
# measured 2026-08-30, that was true for four of the six and false for
# `restore_archived_memory`, which was a live privilege escalation: a scope-guarded
# client could un-archive a row in a scope it was never granted, while passing that
# same scope explicitly was refused. Fixed in the same commit as this test.
# ---------------------------------------------------------------------------------

#: Row-addressed and takes an optional scope, so "omit it" is expressible.
ROW_ADDRESSED_OPTIONAL_SCOPE = [
    "correct_memory",
    "forget_memory",
    "memory_evidence",
    "redact_relationship_version",
]

#: Not reachable by this axis, for reasons that are not about authorization:
#: `memory_receipts` refuses with "requires either run_uuid or scope" before any guard,
#: so omitting the scope yields no operation at all. `restore_archived_memory` needs a
#: real prune ghost and gets its own test below.
OMIT_AXIS_NOT_APPLICABLE = {"memory_receipts": "requires run_uuid or scope; omitting both is a no-op"}


@pytest.mark.parametrize("method_name", ROW_ADDRESSED_OPTIONAL_SCOPE)
async def test_omitting_the_scope_still_guards_the_resolved_row(
    seeded: tuple[Memotron, str],
    method_name: str,
) -> None:
    """Naming a foreign row without naming its scope must not reach it."""
    client, relationship_uuid = seeded
    kwargs = _arguments(method_name, relationship_uuid, scopes_value=[FOREIGN])
    kwargs.pop("scope", None)  # the point of this axis

    exc = await _invoke(client, method_name, kwargs)

    assert exc is not None, (
        f"{method_name} reached a row in {FOREIGN.key!r} when the scope argument was omitted. "
        "The guard skips None, so a guard on the REQUESTED scope alone is no guard at all -- "
        "resolve the row's scope and guard that."
    )
    assert _refuses_scope(exc), f"{method_name} raised {type(exc).__name__} instead of refusing: {exc}"


async def test_restore_archived_memory_guards_the_resolved_ghost_scope(
    tmp_path: pathlib.Path,
) -> None:
    """Regression: omitting `scope` was a WRITE into a scope the client never had.

    Needs a real prune ghost, so it builds its own graph rather than using the shared
    fixture: a pruning job with soft_cap=1 archives the older of two memories.
    """
    pruning_config = default_config().model_copy(
        update={
            "jobs": (DreamJob(name="prune", kind=DreamJobKind.PRUNING, cadence_seconds=1),),
            "pruning": PruningPolicy(
                min_confidence=0.0,
                growth=GrowthPolicy(soft_cap=1),
                retention=RetentionPolicy(protected_memory_types=()),
            ),
        }
    )
    graph = str(tmp_path / "ghosts.sqlite")
    omniscient = Memotron(graph_path=graph, config=pruning_config)
    first = await omniscient.add_memory(
        scope=FOREIGN,
        subject="Vendor",
        predicate="PREFERS",
        object="a value only bob should see",
        relationship_type="PREFERS",
        valid_from=datetime(2026, 7, 1, tzinfo=UTC),
    )
    await omniscient.add_memory(
        scope=FOREIGN,
        subject="Runbook",
        predicate="PREFERS",
        object="summarize each morning",
        relationship_type="PREFERS",
        valid_from=datetime(2026, 7, 14, tzinfo=UTC),
    )
    await omniscient.run_dream_job(job_name="prune", now=datetime(2026, 7, 15, tzinfo=UTC))
    ghost_uuid = str(first.relationship_uuid)
    assert omniscient.archived_matches(query="value", scope=FOREIGN).count == 1, (
        "fixture did not archive anything -- the regression below would be vacuous"
    )

    restricted = Memotron(graph_path=graph, config=pruning_config, authorized_scope_keys={AUTHORIZED.key})

    omitted = await _invoke(restricted, "restore_archived_memory", {"relationship_uuid": ghost_uuid})
    assert omitted is not None, (
        "restore_archived_memory un-archived a row in a scope this client was never granted, "
        "because the scope argument was omitted. This is the T3-8 follow-on bypass."
    )
    assert _refuses_scope(omitted), f"raised {type(omitted).__name__} instead of refusing: {omitted}"

    explicit = await _invoke(restricted, "restore_archived_memory", {"relationship_uuid": ghost_uuid, "scope": FOREIGN})
    assert explicit is not None and _refuses_scope(explicit), "naming the scope explicitly must also refuse"

    # ...and an unrestricted client must still be able to do the legitimate thing.
    result = await omniscient.restore_archived_memory(relationship_uuid=ghost_uuid)
    assert result is not None


# ---------------------------------------------------------------------------------
# Axis 3: read-only scopes.
#
# `_check_scope_not_read_only` is the write-plane choke point. It calls
# `_require_authorized_scope` FIRST and only then consults `config.read_only_scopes`,
# which is the right precedence: someone not authorized for a scope should not learn
# whether it happens to be read-only.
# ---------------------------------------------------------------------------------

WRITE_PLANE = sorted(n for n, v in vars(IngestMixin).items() if not n.startswith("_") and callable(v))


def test_every_write_plane_method_funnels_through_the_read_only_check() -> None:
    """`_check_scope_not_read_only`'s docstring claims every ingestion write funnels
    through it. Derived from IngestMixin rather than listed, so a new write method is
    covered the moment it is written -- the same reason the completeness test exists.
    """
    calls, _ = _self_call_graph()

    def reaches(name: str, seen: set[str] | None = None) -> bool:
        seen = seen or set()
        if name in seen:
            return False
        seen.add(name)
        if "_check_scope_not_read_only" in calls.get(name, ()):
            return True
        return any(reaches(c, seen) for c in calls.get(name, ()))

    assert WRITE_PLANE, "no public IngestMixin methods found -- derivation broke"
    missing = [n for n in WRITE_PLANE if not reaches(n)]
    assert not missing, (
        f"{missing} are public writes that never reach _check_scope_not_read_only. "
        "Every ingestion write must funnel through it -- it is both the read-only check "
        "and the write plane's scope-guard choke point."
    )


@pytest.fixture
async def read_only_client(tmp_path: pathlib.Path) -> Memotron:
    """Authorized for AUTHORIZED only; BOTH scopes are configured read-only.

    FOREIGN must be read-only too, or the precedence test below cannot distinguish the
    two orderings -- with only AUTHORIZED read-only, a call against FOREIGN raises the
    authorization error whichever check runs first. Found by control-testing: swapping
    the order in `_check_scope_not_read_only` left the test green.
    """
    config = default_config().model_copy(update={"read_only_scopes": (AUTHORIZED.key, FOREIGN.key)})
    return Memotron(
        graph_path=str(tmp_path / "readonly.sqlite"),
        config=config,
        authorized_scope_keys={AUTHORIZED.key},
    )


async def test_a_write_to_a_read_only_scope_is_refused(read_only_client: Memotron) -> None:
    exc = await _invoke(
        read_only_client,
        "add_memory",
        {
            "scope": AUTHORIZED,
            "subject": "s",
            "predicate": "PREFERS",
            "object": "v",
            "relationship_type": "PREFERS",
        },
    )
    assert exc is not None, "add_memory wrote to a read-only scope"
    assert isinstance(exc, ValueError) and "read-only" in str(exc), (
        f"expected a read-only refusal, got {type(exc).__name__}: {exc}"
    )


async def test_authorization_is_checked_before_read_only(read_only_client: Memotron) -> None:
    """A scope that is BOTH unauthorized and read-only must report the authorization failure.

    Reporting "read-only" first would tell an unauthorized caller something true about a
    scope they have no business knowing exists.
    """
    exc = await _invoke(
        read_only_client,
        "add_memory",
        {
            "scope": FOREIGN,
            "subject": "s",
            "predicate": "PREFERS",
            "object": "v",
            "relationship_type": "PREFERS",
        },
    )
    assert exc is not None
    assert _refuses_scope(exc), f"expected the authorization refusal first, got: {exc}"
    assert "read-only" not in str(exc), "an unauthorized caller was told the scope is read-only"


# ---------------------------------------------------------------------------------
# Axis 4: an AUTHORIZED scope paired with a row that belongs to a different one.
#
# The scope guard cannot help here, by design -- the scope really is authorized. The
# only thing standing between the caller and another tenant's row is a per-method
# cross-check ("relationship scope X does not match requested scope Y"). Measured
# 2026-08-30: every one of them is present and fires. Nothing tested them, which is
# what T3-10(a) is actually about -- not a live bug, an unguarded invariant. Delete any
# one of those checks and, before this axis existed, no test would have noticed.
# ---------------------------------------------------------------------------------

ROW_ADDRESSED = [
    "correct_memory",
    "forget_memory",
    "memory_evidence",
    "pin_memory",
    "redact_relationship_version",
    "set_memory_visibility",
    "unpin_memory",
]

OWN_SCOPE = MemoryScope(kind="user", identifier="mine", scope_id="mine")


def _is_scope_mismatch(exc: BaseException) -> bool:
    text = str(exc)
    return isinstance(exc, ValueError) and ("does not match" in text or "belongs to scope" in text)


@pytest.fixture(scope="module")
async def two_scope_client(tmp_path_factory: pytest.TempPathFactory) -> tuple[Memotron, str]:
    """Authorized for BOTH scopes, so the scope guard can never be what refuses."""
    graph = str(tmp_path_factory.mktemp("crossscope") / "graph.sqlite")
    omniscient = Memotron(graph_path=graph)
    theirs = await omniscient.add_memory(
        scope=FOREIGN,
        subject="bob",
        predicate="PREFERS",
        object="a value only bob should see",
        relationship_type="PREFERS",
    )
    await omniscient.add_memory(
        scope=OWN_SCOPE, subject="me", predicate="PREFERS", object="mine", relationship_type="PREFERS"
    )
    client = Memotron(graph_path=graph, authorized_scope_keys={OWN_SCOPE.key, FOREIGN.key})
    return client, str(theirs.relationship_uuid)


@pytest.mark.parametrize("method_name", ROW_ADDRESSED)
async def test_an_authorized_scope_cannot_reach_another_scopes_row(
    two_scope_client: tuple[Memotron, str],
    method_name: str,
) -> None:
    client, foreign_row = two_scope_client
    kwargs = _arguments(method_name, foreign_row, scopes_value=[OWN_SCOPE])
    kwargs["scope"] = OWN_SCOPE  # authorized -- so only the cross-check can refuse

    exc = await _invoke(client, method_name, kwargs)

    assert exc is not None, (
        f"{method_name} operated on a row in {FOREIGN.key!r} while asked for {OWN_SCOPE.key!r}. "
        "The scope guard passes here by design -- the per-method scope cross-check is the "
        "only thing preventing this, and it is missing or was removed."
    )
    assert _is_scope_mismatch(exc), f"{method_name} raised {type(exc).__name__} instead of a scope mismatch: {exc}"


async def test_quarantine_adjudication_cannot_reach_another_scopes_candidate(
    two_scope_client: tuple[Memotron, str],
) -> None:
    """T3-10(a) directly: the quarantine store lookup is a scope-free global.

    `quarantined_candidates WHERE candidate_uuid = ?` has no scope predicate, and the
    row is decrypted under its OWN scope key -- so the cross-check in
    `resolve_quarantined_candidate` is the entire barrier. Promoting a foreign candidate
    would write another tenant's decrypted content into the caller's scope through an
    otherwise perfectly legal `add_memory`.
    """
    client, _ = two_scope_client
    candidate_uuid = "11111111-2222-3333-4444-555555555555"
    client.graph.quarantine_candidate(
        candidate_uuid=candidate_uuid,
        scope_key=FOREIGN.key,  # belongs to THEM
        episode_uuid=None,
        reason="test",
        detail="test",
        saves_step=None,
        subject="bob",
        predicate="PREFERS",
        object_text="a value only bob should see",
        proposed_relationship_type="PREFERS",
        proposed_memory_type=None,
        candidate_payload="{}",
        candidate_digest="d",
        instruction_set=None,
        motive_name=None,
        quarantined_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    exc = await _invoke(
        client,
        "resolve_quarantined_candidate",
        {
            "candidate_uuid": candidate_uuid,
            "scope": OWN_SCOPE,  # authorized, and NOT the candidate's scope
            "decision": "promote",
            "reason": "attempting a cross-scope promote",
            "resolved_by": "test",
        },
    )

    assert exc is not None, (
        "resolve_quarantined_candidate promoted a candidate belonging to another scope. "
        "The storage lookup is scope-free, so the client-layer cross-check is the only barrier."
    )
    assert _is_scope_mismatch(exc), f"raised {type(exc).__name__} instead of a scope mismatch: {exc}"
