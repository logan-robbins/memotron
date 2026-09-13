"""#251 units 12-13: the sqlite3 append-only claim ledger.

Two integration cases carry real weight here, and both are about the property
the whole design rests on rather than about sqlite:

* **A second ``CavemanLedger`` on the same file path reads back every entry.**
  That is the *regenerability precondition*: the graph is rebuildable from the
  ledger, so the ledger has to actually be durable. ``":memory:"`` cannot prove
  it, which is why one test uses ``tmp_path``.
* **Erasing one of two episodes leaves the other's entries and co-occurrence
  byte-identical.** Erasure is "delete this episode's entries, then re-dream",
  so anything it touches beyond the named episode is data loss.

``reassign`` is tested against the corrected signature (with ``from_node_id``).
See ``seams.LedgerStore.reassign`` for why the design document's two-argument
version cannot express a split without losing data either way.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from memotron.caveman.ledger import CavemanLedger
from memotron.caveman.models import ClaimKind, ClaimMode, DreamEvent, DreamOp, LedgerEntry, NodeAlias
from memotron.caveman.seams import LedgerStore

NOW = datetime(2026, 9, 8, 16, 40, tzinfo=UTC)
SCOPE = "repo:jedai/memotron"
OTHER_SCOPE = "repo:jedai/other"

#: A non-UTC offset, so the round trip and the ordering are tested against a
#: timestamp that is NOT already normalised. `Clock` returns tz-aware datetimes
#: and nothing promises they are UTC.
PACIFIC_ISH = timezone(timedelta(hours=-7))


@pytest.fixture
def ledger() -> CavemanLedger:
    return CavemanLedger(":memory:")


def _entry(
    index: int,
    *,
    nodes: tuple[str, ...] = (),
    episode: str = "ep-251-01",
    scope: str = SCOPE,
    ts: datetime | None = None,
    supersedes: str | None = None,
    kind: ClaimKind = ClaimKind.ATTRIBUTE,
    claim_mode: ClaimMode = ClaimMode.DESCRIPTIVE,
    turns: tuple[int, ...] | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        entry_id=f"e-{index:02d}",
        ts=ts if ts is not None else NOW + timedelta(minutes=index),
        episode_id=episode,
        scope=scope,
        claim=f"This is claim number {index}, stated as a full sentence.",
        kind=kind,
        claim_mode=claim_mode,
        subjects=(f"subject {index}",),
        objects=(f"object {index}",),
        identifiers=(f"#{index}",),
        node_ids=nodes,
        motive="engineering",
        confidence=0.9,
        supersedes=supersedes,
        turns=turns if turns is not None else (index,),
        receipt_id=f"r-{index:02d}",
    )


def _append_ten(ledger: CavemanLedger, *, nodes: tuple[str, ...] = ("A",)) -> list[LedgerEntry]:
    entries = [_entry(index, nodes=nodes) for index in range(1, 11)]
    for entry in entries:
        ledger.append(entry)
    return entries


# ============================================== Protocol conformance (unit 6)


def test_the_ledger_satisfies_every_ledgerstore_member_by_signature() -> None:
    """Signatures, not just names -- `reassign` is exactly where that matters."""
    problems: list[str] = []
    for attribute in sorted(LedgerStore.__protocol_attrs__):
        implementation = getattr(CavemanLedger, attribute, None)
        if implementation is None:
            problems.append(f"missing: {attribute}")
            continue
        expected = inspect.signature(getattr(LedgerStore, attribute))
        actual = inspect.signature(implementation)
        if expected != actual:
            problems.append(f"{attribute}: protocol {expected} != impl {actual}")
    assert not problems, "\n".join(problems)


def test_the_protocol_declares_the_eighteen_designed_operations() -> None:
    """The control on the conformance test above.

    Thirteen as designed, plus ``identifiers`` and ``entry_counts`` from #251
    amendment A -- the exact-match index a read seeds from, and the entry count a
    node header renders -- plus ``turn_cooccurrence`` from amendment B, the
    provenance the forced merge ranks above embedding similarity, and
    ``record_event`` / ``events`` from amendment D, the journal that makes
    compression replayable rather than merely trusted.
    """
    assert len(LedgerStore.__protocol_attrs__) == 18


def test_the_ledger_is_typed_as_the_protocol() -> None:
    store: LedgerStore = CavemanLedger(":memory:")
    assert store.unbound(scope=SCOPE) == []
    store.close()


# ==================================================== unit 12: append and read


def test_append_returns_the_entry_id(ledger: CavemanLedger) -> None:
    assert ledger.append(_entry(1)) == "e-01"


def test_an_appended_entry_round_trips_exactly(ledger: CavemanLedger) -> None:
    """Every field, including the tz-aware timestamp and the enums."""
    original = _entry(1, nodes=("A", "B"), supersedes="e-00", kind=ClaimKind.RULE, claim_mode=ClaimMode.DIRECTIVE)
    ledger.append(original)
    assert ledger.get("e-01") == original


def test_a_timestamp_keeps_its_offset_through_a_round_trip(ledger: CavemanLedger) -> None:
    """Stored as ISO text, so a non-UTC offset survives rather than being normalised.

    The ordering column is a separate epoch REAL, which is why the text column
    can afford to be lossless: comparisons never depend on lexical ISO ordering
    across two different offsets.
    """
    stamped = datetime(2026, 9, 8, 16, 40, tzinfo=PACIFIC_ISH)
    ledger.append(_entry(1, ts=stamped))
    assert ledger.get("e-01").ts == stamped
    assert ledger.get("e-01").ts.utcoffset() == timedelta(hours=-7)


def test_ordering_is_by_instant_not_by_iso_text(ledger: CavemanLedger) -> None:
    """Two offsets, and the EARLIER instant is the one with the LATER wall clock.

    `2026-09-08T16:00-07:00` is 23:00Z; `2026-09-08T20:00+00:00` is 20:00Z. Sorted
    as text the first wins; sorted as instants the second does. The epoch column
    is what makes this right.
    """
    ledger.append(_entry(1, nodes=("A",), ts=datetime(2026, 9, 8, 16, 0, tzinfo=PACIFIC_ISH)))
    ledger.append(_entry(2, nodes=("A",), ts=datetime(2026, 9, 8, 20, 0, tzinfo=UTC)))
    assert [entry.entry_id for entry in ledger.for_node("A")] == ["e-02", "e-01"]


def test_get_of_an_unknown_entry_raises(ledger: CavemanLedger) -> None:
    with pytest.raises(ValueError, match="no such ledger entry: e-99"):
        ledger.get("e-99")


def test_appending_the_same_entry_id_twice_raises(ledger: CavemanLedger) -> None:
    """Append-only means append ONCE. A silent overwrite would rewrite history."""
    ledger.append(_entry(1))
    with pytest.raises(ValueError, match="already appended: e-01"):
        ledger.append(_entry(1))


def test_for_node_returns_the_nodes_entries_oldest_first(ledger: CavemanLedger) -> None:
    _append_ten(ledger)
    found = ledger.for_node("A")
    assert [entry.entry_id for entry in found] == [f"e-{index:02d}" for index in range(1, 11)]


def test_for_node_of_an_unbound_node_is_empty(ledger: CavemanLedger) -> None:
    _append_ten(ledger)
    assert ledger.for_node("Z") == []


def test_for_node_filters_by_since_inclusively(ledger: CavemanLedger) -> None:
    """INCLUSIVE (`ts >= since`) -- see `seams.LedgerStore.for_node` for why.

    The dreamer emits a complete line set, so re-seeing one entry is idempotent
    while missing one silently loses a claim.
    """
    entries = _append_ten(ledger)
    watermark = entries[4].ts
    found = ledger.for_node("A", since=watermark)
    assert [entry.entry_id for entry in found] == ["e-05", "e-06", "e-07", "e-08", "e-09", "e-10"]


def test_for_node_with_a_since_equal_to_every_timestamp_returns_everything(ledger: CavemanLedger) -> None:
    """The fixed-clock case. An exclusive bound would return nothing here.

    Which is exactly what a test that stamps `dreamed_at` and the entries from
    one clock reading does, and it would look like "the dream saw no evidence".
    """
    for index in range(1, 4):
        ledger.append(_entry(index, nodes=("A",), ts=NOW))
    assert len(ledger.for_node("A", since=NOW)) == 3


def test_for_node_honours_limit(ledger: CavemanLedger) -> None:
    _append_ten(ledger)
    found = ledger.for_node("A", limit=3)
    assert [entry.entry_id for entry in found] == ["e-01", "e-02", "e-03"]


def test_for_node_combines_since_and_limit(ledger: CavemanLedger) -> None:
    entries = _append_ten(ledger)
    found = ledger.for_node("A", since=entries[6].ts, limit=2)
    assert [entry.entry_id for entry in found] == ["e-07", "e-08"]


def test_entries_sharing_a_timestamp_keep_insertion_order(ledger: CavemanLedger) -> None:
    """A whole batch is stamped from one clock reading, so `ts` is not a total order.

    An unstable order would make a dream prompt vary between runs over identical
    data, which would make the run's own digest meaningless.
    """
    for index in (3, 1, 2):
        ledger.append(_entry(index, nodes=("A",), ts=NOW))
    assert [entry.entry_id for entry in ledger.for_node("A")] == ["e-03", "e-01", "e-02"]


def test_for_episode_returns_every_entry_of_that_episode(ledger: CavemanLedger) -> None:
    _append_ten(ledger)
    assert len(ledger.for_episode("ep-251-01")) == 10
    assert ledger.for_episode("ep-nothing") == []


def test_for_episode_does_not_leak_another_episode(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1, episode="ep-a"))
    ledger.append(_entry(2, episode="ep-b"))
    assert [entry.entry_id for entry in ledger.for_episode("ep-a")] == ["e-01"]


def test_unbound_shrinks_to_zero_as_bind_nodes_is_called(ledger: CavemanLedger) -> None:
    """Stage 2's input queue draining. `unbound` is how reconcile finds its work."""
    entries = _append_ten(ledger, nodes=())
    assert len(ledger.unbound(scope=SCOPE)) == 10
    for index, entry in enumerate(entries, start=1):
        ledger.bind_nodes(entry.entry_id, [f"n-{index:03d}"])
        assert len(ledger.unbound(scope=SCOPE)) == 10 - index
    assert ledger.unbound(scope=SCOPE) == []


def test_unbound_is_scoped(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1, scope=SCOPE))
    ledger.append(_entry(2, scope=OTHER_SCOPE))
    assert [entry.entry_id for entry in ledger.unbound(scope=SCOPE)] == ["e-01"]


def test_unbound_is_chronological(ledger: CavemanLedger) -> None:
    for index in (3, 1, 2):
        ledger.append(_entry(index))
    assert [entry.entry_id for entry in ledger.unbound(scope=SCOPE)] == ["e-01", "e-02", "e-03"]


def test_an_entry_appended_with_node_ids_is_bound_immediately(ledger: CavemanLedger) -> None:
    """Extract appends unbound, but the record supports pre-bound entries."""
    ledger.append(_entry(1, nodes=("A", "B")))
    assert ledger.get("e-01").node_ids == ("A", "B")
    assert ledger.unbound(scope=SCOPE) == []


def test_bind_nodes_is_additive_and_idempotent(ledger: CavemanLedger) -> None:
    """Two surface names in one batch can legitimately resolve to the same node."""
    ledger.append(_entry(1))
    ledger.bind_nodes("e-01", ["A"])
    ledger.bind_nodes("e-01", ["A", "B"])
    assert ledger.get("e-01").node_ids == ("A", "B")


def test_bind_nodes_on_an_unknown_entry_raises(ledger: CavemanLedger) -> None:
    with pytest.raises(ValueError, match="no such ledger entry"):
        ledger.bind_nodes("e-99", ["A"])


def test_node_ids_come_back_in_a_stable_order(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1))
    ledger.bind_nodes("e-01", ["Z", "A", "M"])
    assert ledger.get("e-01").node_ids == ("A", "M", "Z")


# -- unit 12's integration case ------------------------------------------------


def test_a_second_ledger_on_the_same_file_reads_back_every_entry(tmp_path: Path) -> None:
    """The regenerability precondition. `":memory:"` cannot prove this.

    The graph is rebuildable from the ledger, so if the ledger were not durable
    the whole erasure story would be fiction.
    """
    path = str(tmp_path / "caveman.sqlite")
    first = CavemanLedger(path)
    written = _append_ten(first, nodes=("A", "B"))
    first.close()

    second = CavemanLedger(path)
    try:
        assert [entry.entry_id for entry in second.for_node("A")] == [entry.entry_id for entry in written]
        assert second.get("e-05") == written[4]
        assert second.cooccurrence("A") == {"B": 10}
    finally:
        second.close()


def test_the_schema_is_created_on_construction(tmp_path: Path) -> None:
    """No migration step, no first-write lazy init: constructing is enough."""
    path = str(tmp_path / "fresh.sqlite")
    ledger = CavemanLedger(path)
    try:
        assert ledger.unbound(scope=SCOPE) == []
        assert ledger.for_episode("nothing") == []
        assert ledger.cooccurrence("nothing") == {}
    finally:
        ledger.close()


def test_close_is_idempotent(tmp_path: Path) -> None:
    ledger = CavemanLedger(str(tmp_path / "x.sqlite"))
    ledger.close()
    ledger.close()


# ============================================ unit 13: derivation and erasure


def _abc_fixture(ledger: CavemanLedger) -> None:
    """Three entries over {A,B}, {A,B}, {A,C} -- the design's own worked example."""
    ledger.append(_entry(1, nodes=("A", "B")))
    ledger.append(_entry(2, nodes=("A", "B")))
    ledger.append(_entry(3, nodes=("A", "C")))


def test_cooccurrence_counts_shared_entries(ledger: CavemanLedger) -> None:
    _abc_fixture(ledger)
    assert ledger.cooccurrence("A") == {"B": 2, "C": 1}


def test_cooccurrence_is_symmetric(ledger: CavemanLedger) -> None:
    """Which is what makes `set_link_weights` coherent as a one-sided operation."""
    _abc_fixture(ledger)
    assert ledger.cooccurrence("B") == {"A": 2}
    assert ledger.cooccurrence("C") == {"A": 1}
    assert ledger.cooccurrence("A")["B"] == ledger.cooccurrence("B")["A"]


def test_cooccurrence_never_names_the_node_itself(ledger: CavemanLedger) -> None:
    """`Link` forbids a self-loop, so a self-count would make `set_link_weights` raise."""
    _abc_fixture(ledger)
    assert "A" not in ledger.cooccurrence("A")


def test_cooccurrence_of_an_unlinked_node_is_empty(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1, nodes=("A",)))
    assert ledger.cooccurrence("A") == {}


def test_cooccurrence_is_ordered_by_node_id(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1, nodes=("A", "Z", "M")))
    assert list(ledger.cooccurrence("A")) == ["M", "Z"]


# ------------------------------------------- the two search-side derivations


def test_identifiers_maps_every_identifier_to_the_nodes_carrying_it(ledger: CavemanLedger) -> None:
    """`_entry(n)` carries the identifier `#n`, which is the case this exists for."""
    _abc_fixture(ledger)
    assert ledger.identifiers(scope=SCOPE) == {"#1": ("A", "B"), "#2": ("A", "B"), "#3": ("A", "C")}


def test_identifiers_unions_the_nodes_of_two_entries_sharing_one_identifier(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1, nodes=("A",)))
    ledger.append(_entry(1, nodes=("C",)).model_copy(update={"entry_id": "e-99"}))
    assert ledger.identifiers(scope=SCOPE)["#1"] == ("A", "C")


def test_identifiers_ignores_an_unbound_entry(ledger: CavemanLedger) -> None:
    """An unbound entry has no node to seed; offering it would return a miss."""
    ledger.append(_entry(1))
    ledger.append(_entry(2, nodes=("A",)))
    assert ledger.identifiers(scope=SCOPE) == {"#2": ("A",)}


def test_identifiers_stays_inside_its_scope(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1, nodes=("A",), scope=OTHER_SCOPE))
    ledger.append(_entry(2, nodes=("B",)))
    assert ledger.identifiers(scope=SCOPE) == {"#2": ("B",)}


def test_identifiers_of_an_empty_scope_is_empty(ledger: CavemanLedger) -> None:
    assert ledger.identifiers(scope=SCOPE) == {}


def test_identifiers_is_sorted_on_both_sides(ledger: CavemanLedger) -> None:
    """Read seeds are digested; an unstable mapping would make the digest meaningless."""
    ledger.append(_entry(9, nodes=("Z", "A")))
    ledger.append(_entry(1, nodes=("M",)))
    mapping = ledger.identifiers(scope=SCOPE)
    assert list(mapping) == ["#1", "#9"]
    assert mapping["#9"] == ("A", "Z")


def test_identifiers_keeps_a_spelling_verbatim(ledger: CavemanLedger) -> None:
    """The matching policy belongs to the reader; folding here would merge two claims."""
    ledger.append(_entry(1, nodes=("A",)).model_copy(update={"identifiers": ("C4", "c4")}))
    assert set(ledger.identifiers(scope=SCOPE)) == {"C4", "c4"}


def test_entry_counts_counts_entries_per_node(ledger: CavemanLedger) -> None:
    _abc_fixture(ledger)
    assert ledger.entry_counts(scope=SCOPE) == {"A": 3, "B": 2, "C": 1}


def test_entry_counts_omits_a_node_with_no_entries(ledger: CavemanLedger) -> None:
    """A node can outlive its evidence -- erasure deletes entries, not nodes."""
    ledger.append(_entry(1, nodes=("A",)))
    assert "B" not in ledger.entry_counts(scope=SCOPE)


def test_entry_counts_stays_inside_its_scope(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1, nodes=("A",)))
    ledger.append(_entry(2, nodes=("A",), scope=OTHER_SCOPE))
    assert ledger.entry_counts(scope=SCOPE) == {"A": 1}


def test_entry_counts_of_an_empty_scope_is_empty(ledger: CavemanLedger) -> None:
    assert ledger.entry_counts(scope=SCOPE) == {}


def test_entry_counts_agrees_with_for_node(ledger: CavemanLedger) -> None:
    """The control: one statement for the whole scope must equal N calls to for_node."""
    _abc_fixture(ledger)
    counts = ledger.entry_counts(scope=SCOPE)
    for node_id in ("A", "B", "C"):
        assert counts[node_id] == len(ledger.for_node(node_id))


def test_erasing_an_episode_drops_its_identifiers_and_counts(ledger: CavemanLedger) -> None:
    """Both derivations read through the entries, so erasure needs no second delete."""
    ledger.append(_entry(1, nodes=("A",), episode="ep-1"))
    ledger.append(_entry(2, nodes=("A",), episode="ep-2"))
    ledger.delete_episode("ep-1")
    assert ledger.identifiers(scope=SCOPE) == {"#2": ("A",)}
    assert ledger.entry_counts(scope=SCOPE) == {"A": 1}


# ---------------------------------------- the provenance tiebreak (amendment B)


def test_turn_cooccurrence_counts_the_turns_two_nodes_share(ledger: CavemanLedger) -> None:
    """Three entries across two turns -- the exact dict, both keys and values.

    ``e-01`` is turn 1 and binds A and B; ``e-02`` is turn 2 and binds B; ``e-03``
    is turn 2 and binds C. So A and B share exactly turn 1, B and C share exactly
    turn 2, and A and C share nothing at all -- which is the asymmetry the forced
    merge needs and ``cooccurrence`` cannot see, because no single entry binds B
    and C.
    """
    ledger.append(_entry(1, nodes=("A", "B"), turns=(1,)))
    ledger.append(_entry(2, nodes=("B",), turns=(2,)))
    ledger.append(_entry(3, nodes=("C",), turns=(2,)))
    assert ledger.turn_cooccurrence(scope=SCOPE) == {("A", "B"): 1, ("B", "C"): 1}
    assert ledger.cooccurrence("B") == {"A": 1}


def test_turn_cooccurrence_keys_every_pair_a_before_b(ledger: CavemanLedger) -> None:
    """``Link``'s orientation, so the slate cannot key a pair the other way round."""
    ledger.append(_entry(1, nodes=("Z", "A"), turns=(1,)))
    assert list(ledger.turn_cooccurrence(scope=SCOPE)) == [("A", "Z")]


def test_an_entry_bound_to_both_nodes_counts_its_turn_once(ledger: CavemanLedger) -> None:
    """Otherwise one claim about two things would outweigh two turns of evidence."""
    ledger.append(_entry(1, nodes=("A", "B"), turns=(4,)))
    assert ledger.turn_cooccurrence(scope=SCOPE) == {("A", "B"): 1}


def test_two_entries_in_one_turn_are_one_shared_turn(ledger: CavemanLedger) -> None:
    """The unit is the turn, not the entry: what ``cooccurrence`` already counts."""
    ledger.append(_entry(1, nodes=("A",), turns=(7,)))
    ledger.append(_entry(2, nodes=("B",), turns=(7,)))
    ledger.append(_entry(3, nodes=("B",), turns=(7,)))
    assert ledger.turn_cooccurrence(scope=SCOPE) == {("A", "B"): 1}


def test_an_entry_spanning_two_turns_shares_both(ledger: CavemanLedger) -> None:
    """``LedgerEntry.turns`` is a tuple, and a claim restated across turns carries both."""
    ledger.append(_entry(1, nodes=("A",), turns=(1, 2)))
    ledger.append(_entry(2, nodes=("B",), turns=(1, 2)))
    assert ledger.turn_cooccurrence(scope=SCOPE) == {("A", "B"): 2}


def test_the_same_turn_number_in_two_episodes_is_two_different_turns(ledger: CavemanLedger) -> None:
    """Turn numbers restart per episode, so the key is the pair, not the number."""
    ledger.append(_entry(1, nodes=("A",), episode="ep-1", turns=(1,)))
    ledger.append(_entry(2, nodes=("B",), episode="ep-2", turns=(1,)))
    assert ledger.turn_cooccurrence(scope=SCOPE) == {}


def test_turn_cooccurrence_ignores_an_unbound_entry(ledger: CavemanLedger) -> None:
    """An unbound entry has no node id, so there is no pair to key."""
    ledger.append(_entry(1, turns=(1,)))
    ledger.append(_entry(2, nodes=("A",), turns=(1,)))
    assert ledger.turn_cooccurrence(scope=SCOPE) == {}


def test_turn_cooccurrence_never_pairs_a_node_with_itself(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1, nodes=("A",), turns=(1,)))
    ledger.append(_entry(2, nodes=("A",), turns=(1,)))
    assert ledger.turn_cooccurrence(scope=SCOPE) == {}


def test_turn_cooccurrence_stays_inside_its_scope(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1, nodes=("A",), turns=(1,)))
    ledger.append(_entry(2, nodes=("B",), scope=OTHER_SCOPE, turns=(1,)))
    assert ledger.turn_cooccurrence(scope=SCOPE) == {}


def test_turn_cooccurrence_of_an_empty_scope_is_empty(ledger: CavemanLedger) -> None:
    assert ledger.turn_cooccurrence(scope=SCOPE) == {}


def test_turn_cooccurrence_is_sorted_and_stable_across_two_calls(ledger: CavemanLedger) -> None:
    """The slate is byte-identical across runs, so its inputs have to be too."""
    ledger.append(_entry(1, nodes=("Z", "M", "A"), turns=(1,)))
    first = ledger.turn_cooccurrence(scope=SCOPE)
    assert list(first) == [("A", "M"), ("A", "Z"), ("M", "Z")]
    assert ledger.turn_cooccurrence(scope=SCOPE) == first


def test_erasing_an_episode_drops_the_turns_it_contributed(ledger: CavemanLedger) -> None:
    """Read through the entries, like the other two derivations: no second delete."""
    ledger.append(_entry(1, nodes=("A",), episode="ep-1", turns=(1,)))
    ledger.append(_entry(2, nodes=("B",), episode="ep-1", turns=(1,)))
    ledger.append(_entry(3, nodes=("A",), episode="ep-2", turns=(1,)))
    ledger.append(_entry(4, nodes=("B",), episode="ep-2", turns=(1,)))
    assert ledger.turn_cooccurrence(scope=SCOPE) == {("A", "B"): 2}
    ledger.delete_episode("ep-1")
    assert ledger.turn_cooccurrence(scope=SCOPE) == {("A", "B"): 1}


def test_rekey_node_moves_entries_and_collapses_cooccurrence(ledger: CavemanLedger) -> None:
    """B merges into A: the two entries they shared stop being an edge at all."""
    _abc_fixture(ledger)
    moved = ledger.rekey_node(from_node_id="B", to_node_id="A")
    assert moved == ["e-01", "e-02"]
    assert ledger.cooccurrence("A") == {"C": 1}
    assert ledger.cooccurrence("B") == {}
    assert ledger.for_node("B") == []


def test_rekey_node_does_not_duplicate_a_shared_entry(ledger: CavemanLedger) -> None:
    """The common case, not an edge case: sharing entries is WHY they were merged."""
    _abc_fixture(ledger)
    ledger.rekey_node(from_node_id="B", to_node_id="A")
    assert ledger.get("e-01").node_ids == ("A",)
    assert len(ledger.for_node("A")) == 3


def test_rekey_node_returns_ids_in_chronological_order(ledger: CavemanLedger) -> None:
    """`NodeAlias.moved_entry_ids` records this, so an un-merge is a replay."""
    for index in (3, 1, 2):
        ledger.append(_entry(index, nodes=("B",), ts=NOW + timedelta(minutes=index)))
    assert ledger.rekey_node(from_node_id="B", to_node_id="A") == ["e-01", "e-02", "e-03"]


def test_rekey_node_of_an_unbound_node_moves_nothing(ledger: CavemanLedger) -> None:
    _abc_fixture(ledger)
    assert ledger.rekey_node(from_node_id="Z", to_node_id="A") == []
    assert ledger.cooccurrence("A") == {"B": 2, "C": 1}


def test_rekey_node_onto_itself_is_refused(ledger: CavemanLedger) -> None:
    with pytest.raises(ValueError, match="cannot re-key a node onto itself"):
        ledger.rekey_node(from_node_id="A", to_node_id="A")


def test_reassign_moves_only_the_named_subset(ledger: CavemanLedger) -> None:
    """The split half. `from_node_id` is what makes it lossless -- see the module docstring."""
    _abc_fixture(ledger)
    ledger.reassign(entry_ids=["e-01"], from_node_id="A", node_id="D")
    assert [entry.entry_id for entry in ledger.for_node("A")] == ["e-02", "e-03"]
    assert [entry.entry_id for entry in ledger.for_node("D")] == ["e-01"]


def test_reassign_preserves_the_entrys_other_bindings(ledger: CavemanLedger) -> None:
    """THE reason `from_node_id` was added.

    e-01 is bound to both A and B. Splitting A must not cost the entry its B
    binding -- every LINK weight is derived from co-occurrence, so losing the
    binding loses the edge with nothing reporting it.
    """
    _abc_fixture(ledger)
    ledger.reassign(entry_ids=["e-01"], from_node_id="A", node_id="D")
    assert ledger.get("e-01").node_ids == ("B", "D")
    assert ledger.cooccurrence("D") == {"B": 1}


def test_reassign_leaves_no_binding_to_the_split_parent(ledger: CavemanLedger) -> None:
    """The other failure the two-argument signature had: a phantom parent binding
    makes the next `set_link_weights` raise on a node that is not in the graph."""
    ledger.append(_entry(1, nodes=("P",)))
    ledger.append(_entry(2, nodes=("P",)))
    ledger.reassign(entry_ids=["e-01"], from_node_id="P", node_id="X")
    ledger.reassign(entry_ids=["e-02"], from_node_id="P", node_id="Y")
    assert ledger.for_node("P") == []
    assert ledger.get("e-01").node_ids == ("X",)
    assert ledger.get("e-02").node_ids == ("Y",)


def test_reassign_partitions_a_parent_exactly(ledger: CavemanLedger) -> None:
    """No entry dropped, none duplicated -- the design's own rule for a split."""
    for index in range(1, 6):
        ledger.append(_entry(index, nodes=("P",)))
    ledger.reassign(entry_ids=["e-01", "e-02"], from_node_id="P", node_id="X")
    ledger.reassign(entry_ids=["e-03", "e-04", "e-05"], from_node_id="P", node_id="Y")
    on_x = {entry.entry_id for entry in ledger.for_node("X")}
    on_y = {entry.entry_id for entry in ledger.for_node("Y")}
    assert on_x | on_y == {f"e-{i:02d}" for i in range(1, 6)}
    assert not on_x & on_y


def test_reassign_skips_an_entry_not_bound_to_the_parent(ledger: CavemanLedger) -> None:
    """Converges on a re-run rather than raising, since a partition is idempotent."""
    _abc_fixture(ledger)
    ledger.reassign(entry_ids=["e-01", "e-99"], from_node_id="A", node_id="D")
    assert [entry.entry_id for entry in ledger.for_node("D")] == ["e-01"]
    ledger.reassign(entry_ids=["e-01"], from_node_id="A", node_id="D")
    assert [entry.entry_id for entry in ledger.for_node("D")] == ["e-01"]


def test_reassign_with_no_entries_changes_nothing(ledger: CavemanLedger) -> None:
    _abc_fixture(ledger)
    ledger.reassign(entry_ids=[], from_node_id="A", node_id="D")
    assert ledger.cooccurrence("A") == {"B": 2, "C": 1}


def test_reassign_onto_itself_is_refused(ledger: CavemanLedger) -> None:
    with pytest.raises(ValueError, match="cannot reassign a node onto itself"):
        ledger.reassign(entry_ids=["e-01"], from_node_id="A", node_id="A")


# -- aliases -------------------------------------------------------------------


def test_an_alias_round_trips(ledger: CavemanLedger) -> None:
    alias = NodeAlias(
        alias_node_id="n-011",
        survivor_node_id="n-003",
        moved_entry_ids=("e-01", "e-02"),
        receipt_id="r-12",
        ts=NOW,
    )
    ledger.record_alias(alias)
    assert ledger.aliases("n-003") == [alias]


def test_aliases_are_oldest_first(ledger: CavemanLedger) -> None:
    for index in range(3):
        ledger.record_alias(
            NodeAlias(
                alias_node_id=f"n-{index:03d}",
                survivor_node_id="n-999",
                moved_entry_ids=(),
                receipt_id=f"r-{index}",
                ts=NOW,
            )
        )
    assert [alias.alias_node_id for alias in ledger.aliases("n-999")] == ["n-000", "n-001", "n-002"]


def test_aliases_of_a_node_that_absorbed_nothing_is_empty(ledger: CavemanLedger) -> None:
    assert ledger.aliases("n-003") == []


def test_an_alias_records_exactly_what_rekey_node_returned(ledger: CavemanLedger) -> None:
    """The un-merge-is-a-replay property, end to end."""
    _abc_fixture(ledger)
    moved = ledger.rekey_node(from_node_id="B", to_node_id="A")
    ledger.record_alias(
        NodeAlias(
            alias_node_id="B",
            survivor_node_id="A",
            moved_entry_ids=tuple(moved),
            receipt_id="r-1",
            ts=NOW,
        )
    )
    assert list(ledger.aliases("A")[0].moved_entry_ids) == moved


# -- erasure -------------------------------------------------------------------


def test_delete_episode_removes_exactly_that_episodes_entries(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1, nodes=("A",), episode="ep-a"))
    ledger.append(_entry(2, nodes=("A",), episode="ep-b"))
    ledger.delete_episode("ep-a")
    assert ledger.for_episode("ep-a") == []
    assert [entry.entry_id for entry in ledger.for_episode("ep-b")] == ["e-02"]


def test_delete_episode_returns_the_union_of_touched_node_ids(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1, nodes=("A", "B"), episode="ep-a"))
    ledger.append(_entry(2, nodes=("B", "C"), episode="ep-a"))
    assert ledger.delete_episode("ep-a") == ["A", "B", "C"]


def test_delete_episode_returns_a_sorted_list(ledger: CavemanLedger) -> None:
    """So the caller's own erasure receipt is reproducible."""
    ledger.append(_entry(1, nodes=("Z", "A", "M"), episode="ep-a"))
    assert ledger.delete_episode("ep-a") == ["A", "M", "Z"]


def test_delete_episode_takes_the_bindings_with_the_entries(ledger: CavemanLedger) -> None:
    """`ON DELETE CASCADE`, so there is no second delete to forget."""
    ledger.append(_entry(1, nodes=("A", "B"), episode="ep-a"))
    ledger.delete_episode("ep-a")
    assert ledger.for_node("A") == []
    assert ledger.cooccurrence("A") == {}


def test_deleting_an_unknown_episode_is_a_no_op(ledger: CavemanLedger) -> None:
    _append_ten(ledger)
    assert ledger.delete_episode("ep-nothing") == []
    assert len(ledger.for_node("A")) == 10


def test_delete_episode_of_entries_with_no_bindings_returns_nothing(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1, episode="ep-a"))
    assert ledger.delete_episode("ep-a") == []
    assert ledger.for_episode("ep-a") == []


# -- unit 13's integration case ------------------------------------------------


def test_erasing_one_episode_leaves_the_other_byte_identical(ledger: CavemanLedger) -> None:
    """Erasure is scoped to the named episode. Anything else it touches is data loss.

    Compared before and after on both the entries themselves and on the DERIVED
    co-occurrence, because the derivation is what the graph is rebuilt from.
    """
    for index in (1, 2, 3):
        ledger.append(_entry(index, nodes=("A", "B"), episode="ep-doomed"))
    for index in (4, 5, 6):
        ledger.append(_entry(index, nodes=("C", "D"), episode="ep-survivor"))

    before_entries = ledger.for_episode("ep-survivor")
    before_cooccurrence = (ledger.cooccurrence("C"), ledger.cooccurrence("D"))
    before_for_node = (ledger.for_node("C"), ledger.for_node("D"))

    touched = ledger.delete_episode("ep-doomed")

    assert touched == ["A", "B"]
    assert ledger.for_episode("ep-survivor") == before_entries
    assert (ledger.cooccurrence("C"), ledger.cooccurrence("D")) == before_cooccurrence
    assert (ledger.for_node("C"), ledger.for_node("D")) == before_for_node
    assert ledger.cooccurrence("A") == {}
    assert ledger.for_node("A") == []


def test_erasing_an_episode_that_shares_a_node_leaves_the_survivors_support(ledger: CavemanLedger) -> None:
    """The re-dream case: the node still exists because it still has evidence.

    A node whose every entry is gone is the caller's cue to DELETE rather than
    re-dream, and this is the other branch -- support remains, so a re-dream has
    something to read.
    """
    ledger.append(_entry(1, nodes=("A",), episode="ep-doomed"))
    ledger.append(_entry(2, nodes=("A",), episode="ep-survivor"))
    assert ledger.delete_episode("ep-doomed") == ["A"]
    assert [entry.entry_id for entry in ledger.for_node("A")] == ["e-02"]


def test_erasing_every_episode_of_a_node_leaves_it_with_no_support(ledger: CavemanLedger) -> None:
    ledger.append(_entry(1, nodes=("A",), episode="ep-only"))
    assert ledger.delete_episode("ep-only") == ["A"]
    assert ledger.for_node("A") == []


# ==================== the event journal (#251 amendment D)


def _event(
    index: int,
    *,
    op: DreamOp = DreamOp.NODE_CREATED,
    scope: str = SCOPE,
    nodes: tuple[str, ...] = ("n-001",),
    before: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
    at: datetime = NOW,
) -> DreamEvent:
    return DreamEvent(
        event_id=f"ev-{index:04d}",
        ts=at,
        scope=scope,
        op=op,
        node_ids=nodes,
        before=before or {},
        after=after or {"node": {"name": "gateway"}},
        receipt_id=f"rc-{index}",
    )


def test_an_event_round_trips_through_the_journal(ledger: CavemanLedger) -> None:
    """Every field, because a replay reads exactly these and nothing else."""
    ledger.record_event(_event(1, op=DreamOp.NODE_MERGED, nodes=("n-001", "n-002")))
    (stored,) = ledger.events(scope=SCOPE)
    assert stored == _event(1, op=DreamOp.NODE_MERGED, nodes=("n-001", "n-002"))


def test_the_journal_is_ordered_by_time_then_insertion_sequence(ledger: CavemanLedger) -> None:
    """A whole dream pass is stamped from one clock reading, so ``ts`` alone is not
    an order -- and a replay that applied a merge before the node it merges would
    not be a replay."""
    for index in range(5):
        ledger.record_event(_event(index + 1))
    assert [event.event_id for event in ledger.events(scope=SCOPE)] == [f"ev-{i:04d}" for i in range(1, 6)]


def test_the_journal_is_per_scope(ledger: CavemanLedger) -> None:
    ledger.record_event(_event(1, scope=SCOPE))
    ledger.record_event(_event(2, scope="repo:jedai/other"))
    assert [event.event_id for event in ledger.events(scope=SCOPE)] == ["ev-0001"]
    assert [event.event_id for event in ledger.events(scope="repo:jedai/other")] == ["ev-0002"]


def test_the_journals_since_bound_is_inclusive_like_for_node(ledger: CavemanLedger) -> None:
    """One rule for both streams, so a caller cannot read the two differently."""
    ledger.record_event(_event(1, at=NOW))
    ledger.record_event(_event(2, at=NOW + timedelta(hours=1)))
    assert len(ledger.events(scope=SCOPE, since=NOW)) == 2
    assert len(ledger.events(scope=SCOPE, since=NOW + timedelta(hours=1))) == 1


def test_the_journal_of_an_untouched_scope_is_empty(ledger: CavemanLedger) -> None:
    assert ledger.events(scope=SCOPE) == []


def test_recording_the_same_event_id_twice_is_a_defect(ledger: CavemanLedger) -> None:
    """Append-only: a history that can be edited proves nothing."""
    ledger.record_event(_event(1))
    with pytest.raises(ValueError, match="already recorded"):
        ledger.record_event(_event(1))


def test_the_journal_preserves_nested_content_verbatim(ledger: CavemanLedger) -> None:
    """The payload is a node's or a relation's content, so a replay has to get back
    exactly what was written -- nested lists and all."""
    payload = {
        "node": {
            "node_id": "n-001",
            "name": "gateway",
            "aliases": ["JedAI Gateway", "LiteLLM proxy"],
            "facts": [{"kind": "rule", "text": "never hermetic", "entry_ids": ["e-01"]}],
        }
    }
    ledger.record_event(_event(1, after=payload))
    assert ledger.events(scope=SCOPE)[0].after == payload


def test_the_journal_survives_a_second_connection_on_one_file(tmp_path: Path) -> None:
    """The replay precondition: events outlive the process that recorded them."""
    path = str(tmp_path / "journal.sqlite")
    first = CavemanLedger(path)
    first.record_event(_event(1, op=DreamOp.RELATION_UPSERTED))
    first.close()

    second = CavemanLedger(path)
    try:
        assert [event.op for event in second.events(scope=SCOPE)] == [DreamOp.RELATION_UPSERTED]
    finally:
        second.close()


def test_erasing_an_episode_leaves_the_journal_alone(ledger: CavemanLedger) -> None:
    """Two append-only streams, and the claims are the only thing erasure deletes.

    A deletion is itself a decision worth a history: an erasure that also erased
    the record of what the graph used to hold would leave nothing to replay
    against.
    """
    ledger.append(_entry(1, episode="ep-1", nodes=("A",)))
    ledger.record_event(_event(1))
    ledger.delete_episode("ep-1")
    assert ledger.for_episode("ep-1") == []
    assert len(ledger.events(scope=SCOPE)) == 1
