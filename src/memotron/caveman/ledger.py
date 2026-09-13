"""The sqlite3 append-only claim ledger and event journal. The one durable store.

Two append-only streams in one database, and the second is what makes
compression provable rather than merely trusted:

* **claims** (``caveman_entries``) -- what was said, with its provenance. The
  graph is fully regenerable from these, which is what makes erasure "delete the
  episode's entries, then re-dream" rather than a graph-surgery problem.
* **events** (``caveman_events``) -- every graph mutation, with the node and
  relation content before and after (#251 amendment D). A scope's readable state
  is therefore rebuildable by replaying a recorded sequence with no model call,
  and comparable to the live graph by digest. "The dreamer compressed this" stops
  being a claim about a past run and becomes a thing a caller can re-execute.

Events live beside the claims rather than in the graph because the journal has
to outlive and out-size the thing it describes: the graph is bounded at ``N``
nodes and held in memory, and a history bounded by the state it explains is not
a history.

.. rubric:: Why stdlib ``sqlite3`` and not a JSON dump

Three of the four operations this store exists for are indexed queries —
``for_node(node, since=)``, ``for_episode``, ``delete_episode`` — and a JSON
dump answers each by rewriting the whole file, which makes erasure O(ledger).
Zero new dependencies (``pyproject.toml`` declares no ``numpy`` and no
``httpx``; ``sqlite3`` is stdlib on the required ``>=3.12``), and it is the
engine the repo already runs its hermetic lane on. ``":memory:"`` and a file
path are the *same* implementation with a different DSN, not two code paths.

.. rubric:: Why node bindings are their own table

``for_node`` is the read the whole incremental dream is built on, and a
``node_ids`` JSON column would make it a full scan with a ``LIKE``. Normalising
the binding into ``caveman_entry_nodes`` makes it an index seek, and makes
``cooccurrence`` a self-join instead of a Python loop over every entry — which
is the difference the storage decision above was actually about.

.. rubric:: Own tables, own file, ``caveman_`` prefix

Convergence with the WS-11 receipt ledger is a named follow-up, not this issue.
They share a shape — append-only, episode-keyed, hash-chainable — but not a key
space: WS-11's is a ledger of *decisions*, this is a ledger of *claims*.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from memotron.caveman.models import ClaimKind, ClaimMode, DreamEvent, DreamOp, LedgerEntry, NodeAlias

_SCHEMA = """
CREATE TABLE IF NOT EXISTS caveman_entries (
    entry_id    TEXT PRIMARY KEY,
    seq         INTEGER NOT NULL,
    ts_iso      TEXT    NOT NULL,
    ts_epoch    REAL    NOT NULL,
    episode_id  TEXT    NOT NULL,
    scope       TEXT    NOT NULL,
    claim       TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    claim_mode  TEXT    NOT NULL,
    subjects    TEXT    NOT NULL,
    objects     TEXT    NOT NULL,
    identifiers TEXT    NOT NULL,
    motive      TEXT    NOT NULL,
    confidence  REAL    NOT NULL,
    supersedes  TEXT,
    turns       TEXT    NOT NULL,
    receipt_id  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS caveman_entry_nodes (
    entry_id TEXT NOT NULL REFERENCES caveman_entries(entry_id) ON DELETE CASCADE,
    node_id  TEXT NOT NULL,
    PRIMARY KEY (entry_id, node_id)
);

CREATE TABLE IF NOT EXISTS caveman_events (
    event_id   TEXT PRIMARY KEY,
    seq        INTEGER NOT NULL,
    ts_iso     TEXT    NOT NULL,
    ts_epoch   REAL    NOT NULL,
    scope      TEXT    NOT NULL,
    op         TEXT    NOT NULL,
    node_ids   TEXT    NOT NULL,
    before_json TEXT   NOT NULL,
    after_json  TEXT   NOT NULL,
    receipt_id TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS caveman_aliases (
    rowid_alias      INTEGER PRIMARY KEY AUTOINCREMENT,
    alias_node_id    TEXT NOT NULL,
    survivor_node_id TEXT NOT NULL,
    moved_entry_ids  TEXT NOT NULL,
    receipt_id       TEXT NOT NULL,
    ts_iso           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS caveman_entries_episode ON caveman_entries(episode_id);
CREATE INDEX IF NOT EXISTS caveman_entries_scope   ON caveman_entries(scope);
CREATE INDEX IF NOT EXISTS caveman_entries_order   ON caveman_entries(ts_epoch, seq);
CREATE INDEX IF NOT EXISTS caveman_nodes_node      ON caveman_entry_nodes(node_id);
CREATE INDEX IF NOT EXISTS caveman_aliases_surv    ON caveman_aliases(survivor_node_id);
CREATE INDEX IF NOT EXISTS caveman_events_scope    ON caveman_events(scope);
CREATE INDEX IF NOT EXISTS caveman_events_order    ON caveman_events(ts_epoch, seq);
"""

#: Chronological order, with the insertion sequence as the tie-break. Two entries
#: from one episode routinely share a timestamp -- the whole batch is stamped from
#: one clock reading -- so ``ts`` alone is not a total order, and an unstable
#: order would make a dream prompt vary between runs over identical data.
_ORDER = "ORDER BY ts_epoch, seq"


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """Canonical JSON for a journal payload: sorted keys, compact separators.

    The same shape ``receipts.canonical_digest`` digests, so a row and a digest
    over it describe the same bytes -- which is what lets a replay proof compare
    a rebuilt graph to a recorded one without agreeing on a second encoding.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


_ENTRY_COLUMNS = (
    "entry_id, ts_iso, episode_id, scope, claim, kind, claim_mode, "
    "subjects, objects, identifiers, motive, confidence, supersedes, turns, receipt_id"
)


class CavemanLedger:
    """Append-only claim storage over one sqlite3 database.

    ``CavemanLedger(":memory:")`` and ``CavemanLedger("/path/to.sqlite")`` are the
    same implementation; the schema is created on construction either way, so a
    second instance on the same path reads back every entry. That is the
    regenerability precondition the whole design rests on.
    """

    def __init__(self, path: str) -> None:
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()
        self._closed = False

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> LedgerEntry:
        """Rebuild one entry. Every caller goes through :meth:`_entries`, which
        always selects the correlated ``node_ids`` array, so the binding is
        never absent -- an empty array is an unbound entry, not a missing column."""
        return LedgerEntry(
            entry_id=row["entry_id"],
            ts=datetime.fromisoformat(row["ts_iso"]),
            episode_id=row["episode_id"],
            scope=row["scope"],
            claim=row["claim"],
            kind=ClaimKind(row["kind"]),
            claim_mode=ClaimMode(row["claim_mode"]),
            subjects=tuple(json.loads(row["subjects"])),
            objects=tuple(json.loads(row["objects"])),
            identifiers=tuple(json.loads(row["identifiers"])),
            node_ids=tuple(json.loads(row["node_ids"])),
            motive=row["motive"],
            confidence=row["confidence"],
            supersedes=row["supersedes"],
            turns=tuple(json.loads(row["turns"])),
            receipt_id=row["receipt_id"],
        )

    def _entries(self, where: str, parameters: Sequence[Any], *, limit: int | None = None) -> list[LedgerEntry]:
        """Read entries with their bindings attached, in chronological order.

        The bindings come back as a correlated ``json_group_array`` rather than
        as a second round trip per entry, so reading a node's 40 entries is one
        statement and not 41.
        """
        clause = f" LIMIT {int(limit)}" if limit is not None else ""
        rows = self._connection.execute(
            f"SELECT {_ENTRY_COLUMNS}, "
            "  (SELECT json_group_array(node_id) FROM ("
            "     SELECT node_id FROM caveman_entry_nodes n"
            "     WHERE n.entry_id = caveman_entries.entry_id ORDER BY node_id"
            "  )) AS node_ids "
            f"FROM caveman_entries WHERE {where} {_ORDER}{clause}",
            tuple(parameters),
        ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def _next_sequence(self) -> int:
        row = self._connection.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM caveman_entries").fetchone()
        return int(row["next"])

    def _next_event_sequence(self) -> int:
        row = self._connection.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM caveman_events").fetchone()
        return int(row["next"])

    # -- append and read ---------------------------------------------------

    def append(self, entry: LedgerEntry) -> str:
        """Append one entry. A repeated ``entry_id`` is a defect, so it raises."""
        try:
            self._connection.execute(
                "INSERT INTO caveman_entries ("
                "  entry_id, seq, ts_iso, ts_epoch, episode_id, scope, claim, kind, claim_mode,"
                "  subjects, objects, identifiers, motive, confidence, supersedes, turns, receipt_id"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.entry_id,
                    self._next_sequence(),
                    entry.ts.isoformat(),
                    entry.ts.timestamp(),
                    entry.episode_id,
                    entry.scope,
                    entry.claim,
                    entry.kind.value,
                    entry.claim_mode.value,
                    json.dumps(list(entry.subjects)),
                    json.dumps(list(entry.objects)),
                    json.dumps(list(entry.identifiers)),
                    entry.motive,
                    entry.confidence,
                    entry.supersedes,
                    json.dumps(list(entry.turns)),
                    entry.receipt_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"ledger entry already appended: {entry.entry_id}") from exc
        if entry.node_ids:
            self._bind(entry.entry_id, entry.node_ids)
        self._connection.commit()
        return entry.entry_id

    def get(self, entry_id: str) -> LedgerEntry:
        """One entry, or raise ``ValueError``.

        Raises rather than returning ``None``: a caller holding an entry id that
        does not resolve has stale state. ``ValueError`` rather than a new
        ``CavemanError`` because the taxonomy is a published six and this is the
        repo's own convention for a caller defect.
        """
        found = self._entries("entry_id = ?", (entry_id,))
        if not found:
            raise ValueError(f"no such ledger entry: {entry_id}")
        return found[0]

    def for_node(self, node_id: str, *, since: datetime | None = None, limit: int | None = None) -> list[LedgerEntry]:
        """This node's entries, oldest first. ``since`` is INCLUSIVE (``ts >= since``).

        See ``seams.LedgerStore.for_node`` for why inclusive: the dreamer emits a
        complete line set, so re-seeing an entry is idempotent while missing one
        silently loses a claim.
        """
        where = "entry_id IN (SELECT entry_id FROM caveman_entry_nodes WHERE node_id = ?)"
        parameters: list[Any] = [node_id]
        if since is not None:
            where += " AND ts_epoch >= ?"
            parameters.append(since.timestamp())
        return self._entries(where, parameters, limit=limit)

    def for_episode(self, episode_id: str) -> list[LedgerEntry]:
        """Every entry this episode produced. The erasure unit."""
        return self._entries("episode_id = ?", (episode_id,))

    def unbound(self, *, scope: str) -> list[LedgerEntry]:
        """Entries with no node binding yet. Stage 2's input queue."""
        return self._entries(
            "scope = ? AND entry_id NOT IN (SELECT entry_id FROM caveman_entry_nodes)",
            (scope,),
        )

    def identifiers(self, *, scope: str) -> dict[str, tuple[str, ...]]:
        """Every identifier claimed in *scope* to the node ids that carry it.

        The exact-match index the read path seeds from. Identifiers are the
        highest-value tokens in the whole system and the ones embedding
        similarity is worst at: ``#245`` and ``#246`` are one character apart in
        a 3072-dimensional space, so a query for one retrieves the other.

        Bound entries only. An unbound entry has no node to seed, and offering
        its identifier would return an empty hit that looks like a miss.

        Keys are the identifier VERBATIM, as extract recorded it -- the matching
        policy (case, tokenisation) belongs to the reader, and folding here
        would merge two spellings the ledger deliberately kept apart. Sorted, so
        the mapping and anything digested from it are stable.
        """
        rows = self._connection.execute(
            "SELECT e.identifiers AS identifiers, n.node_id AS node_id "
            "FROM caveman_entries e JOIN caveman_entry_nodes n ON n.entry_id = e.entry_id "
            "WHERE e.scope = ?",
            (scope,),
        ).fetchall()
        found: dict[str, set[str]] = {}
        for row in rows:
            for identifier in json.loads(row["identifiers"]):
                found.setdefault(identifier, set()).add(row["node_id"])
        return {identifier: tuple(sorted(found[identifier])) for identifier in sorted(found)}

    def entry_counts(self, *, scope: str) -> dict[str, int]:
        """Entries per node id in *scope*. The ``N entries`` a node header renders.

        How well-evidenced a node is, which is the second thing a reader needs
        after how stale it is. One statement rather than one ``for_node`` call
        per emitted node: a read emits ~24 nodes, and 24 round trips to render
        24 headers is the shape this method exists to avoid.
        """
        rows = self._connection.execute(
            "SELECT n.node_id AS node_id, COUNT(*) AS entries "
            "FROM caveman_entry_nodes n JOIN caveman_entries e ON e.entry_id = n.entry_id "
            "WHERE e.scope = ? GROUP BY n.node_id ORDER BY n.node_id",
            (scope,),
        ).fetchall()
        return {row["node_id"]: int(row["entries"]) for row in rows}

    # -- binding and derivation -------------------------------------------

    def _bind(self, entry_id: str, node_ids: Sequence[str]) -> None:
        self._connection.executemany(
            "INSERT OR IGNORE INTO caveman_entry_nodes (entry_id, node_id) VALUES (?, ?)",
            [(entry_id, node_id) for node_id in node_ids],
        )

    def bind_nodes(self, entry_id: str, node_ids: Sequence[str]) -> None:
        """Attach resolved node ids. **Only ``reconcile`` may call this.**

        Additive and idempotent: re-binding an id the entry already has is a
        no-op rather than an error, because reconcile groups surface names and
        two names in one batch can legitimately resolve to the same node.
        """
        self.get(entry_id)
        self._bind(entry_id, node_ids)
        self._connection.commit()

    def cooccurrence(self, node_id: str) -> dict[str, int]:
        """Other nodes sharing an entry with this one, counted. The LINK weights.

        Derived on demand and never cached: a cached derivation is a second
        source of truth, and the whole reason the weight is an integer the
        ledger determines is so a later store can keep it as a plain edge
        property without owning it.
        """
        rows = self._connection.execute(
            "SELECT other.node_id AS node_id, COUNT(*) AS weight "
            "FROM caveman_entry_nodes mine "
            "JOIN caveman_entry_nodes other ON other.entry_id = mine.entry_id "
            "WHERE mine.node_id = ? AND other.node_id != mine.node_id "
            "GROUP BY other.node_id ORDER BY other.node_id",
            (node_id,),
        ).fetchall()
        return {row["node_id"]: int(row["weight"]) for row in rows}

    def turn_cooccurrence(self, *, scope: str) -> dict[tuple[str, str], int]:
        """Unordered node pairs to the number of conversational turns they share.

        Keyed ``(a, b)`` with ``a < b``, which is :func:`pressure.pair_key`'s
        orientation, so the slate cannot key a pair one way and this the other. The value counts distinct ``(episode_id, turn)`` pairs at
        which both nodes have a bound entry -- so an entry bound to both nodes
        contributes each of its turns once, not twice, and a pair sharing nothing
        is absent rather than present at zero.

        See ``seams.LedgerStore.turn_cooccurrence`` for why the graph needs a
        weaker signal than ``cooccurrence``: two facts stated in one breath are
        two claims and share no entry, so the ledger scores them 0 and the forced
        merge fell through to embedding similarity.

        .. rubric:: Why the pass is inverted

        Asking "how many turns do a and b share" for every pair is the square of
        the node count, and a global dream pass would ask it for a whole scope.
        Inverting it -- one row per (entry, node) binding, bucketed by
        ``(episode_id, turn)``, then the pairs WITHIN each bucket -- costs the
        entries and the turns they name, which is the shape the caller can afford
        once per pass. The buckets are iterated over sorted node ids and the
        result is sorted, so two runs over the same ledger are byte-identical.
        """
        rows = self._connection.execute(
            "SELECT e.episode_id AS episode_id, e.turns AS turns, n.node_id AS node_id "
            "FROM caveman_entries e JOIN caveman_entry_nodes n ON n.entry_id = e.entry_id "
            "WHERE e.scope = ?",
            (scope,),
        ).fetchall()
        at_turn: dict[tuple[str, int], set[str]] = {}
        for row in rows:
            for turn in json.loads(row["turns"]):
                at_turn.setdefault((row["episode_id"], int(turn)), set()).add(row["node_id"])
        shared: dict[tuple[str, str], int] = {}
        for present in at_turn.values():
            for pair in itertools.combinations(sorted(present), 2):
                shared[pair] = shared.get(pair, 0) + 1
        return {pair: shared[pair] for pair in sorted(shared)}

    def _move(self, *, entry_ids: Sequence[str], from_node_id: str, to_node_id: str) -> None:
        """Move a binding from one node to another for the given entries.

        ``INSERT OR IGNORE`` then ``DELETE``: an entry already bound to the
        destination must not fail the insert, which happens whenever a merge
        absorbs a node that shared an entry with its survivor -- the common case,
        not an edge case, since sharing entries is why the dreamer merged them.
        """
        if not entry_ids:
            return
        placeholders = ",".join("?" * len(entry_ids))
        self._connection.execute(
            f"INSERT OR IGNORE INTO caveman_entry_nodes (entry_id, node_id) "
            f"SELECT entry_id, ? FROM caveman_entry_nodes "
            f"WHERE node_id = ? AND entry_id IN ({placeholders})",
            (to_node_id, from_node_id, *entry_ids),
        )
        self._connection.execute(
            f"DELETE FROM caveman_entry_nodes WHERE node_id = ? AND entry_id IN ({placeholders})",
            (from_node_id, *entry_ids),
        )

    def rekey_node(self, *, from_node_id: str, to_node_id: str) -> list[str]:
        """Move EVERY entry from one node to another; returns the moved entry ids.

        The return value is what a :class:`~memotron.caveman.models.NodeAlias`
        records, so an un-merge is a replay rather than a guess about which
        entries came from where. Chronological, matching :meth:`for_node`, so the
        recorded list is stable.
        """
        if from_node_id == to_node_id:
            raise ValueError(f"cannot re-key a node onto itself: {from_node_id}")
        moved = [entry.entry_id for entry in self.for_node(from_node_id)]
        self._move(entry_ids=moved, from_node_id=from_node_id, to_node_id=to_node_id)
        self._connection.commit()
        return moved

    def reassign(self, *, entry_ids: Sequence[str], from_node_id: str, node_id: str) -> None:
        """Move a SUBSET of one node's entries to another. The split half of re-keying.

        ``from_node_id`` is an addition to the design document's signature; see
        ``seams.LedgerStore.reassign`` for the two ways the documented signature
        loses data. Entries in *entry_ids* that are not bound to *from_node_id*
        are silently skipped rather than raising, because a split's partition is
        expressed over the parent's entries and re-running it must converge.
        """
        if from_node_id == node_id:
            raise ValueError(f"cannot reassign a node onto itself: {from_node_id}")
        self._move(entry_ids=entry_ids, from_node_id=from_node_id, to_node_id=node_id)
        self._connection.commit()

    # -- the event journal -------------------------------------------------

    def record_event(self, event: DreamEvent) -> str:
        """Append one graph mutation. A repeated ``event_id`` is a defect, so it raises.

        ``before``/``after`` are stored as canonical JSON -- ``sort_keys`` and
        compact separators -- so two journals of the same mutation are
        byte-identical and a digest over the row means something. The dicts hold
        node and relation CONTENT and never an embedding; see
        :class:`~memotron.caveman.models.DreamEvent`.
        """
        try:
            self._connection.execute(
                "INSERT INTO caveman_events ("
                "  event_id, seq, ts_iso, ts_epoch, scope, op, node_ids, before_json, after_json, receipt_id"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    self._next_event_sequence(),
                    event.ts.isoformat(),
                    event.ts.timestamp(),
                    event.scope,
                    event.op.value,
                    json.dumps(list(event.node_ids)),
                    _canonical_json(event.before),
                    _canonical_json(event.after),
                    event.receipt_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"journal event already recorded: {event.event_id}") from exc
        self._connection.commit()
        return event.event_id

    def events(self, *, scope: str, since: datetime | None = None) -> list[DreamEvent]:
        """This scope's mutations, oldest first, in the order they were applied.

        Ordered by ``(ts_epoch, seq)`` -- the same total order :meth:`for_node`
        uses, and for the same reason: a whole dream pass is routinely stamped
        from one clock reading, so the timestamp alone is not an order, and a
        replay that applied a merge before the node it merges would not be a
        replay. ``since`` is inclusive, matching :meth:`for_node`.
        """
        where = "scope = ?"
        parameters: list[Any] = [scope]
        if since is not None:
            where += " AND ts_epoch >= ?"
            parameters.append(since.timestamp())
        rows = self._connection.execute(
            "SELECT event_id, ts_iso, scope, op, node_ids, before_json, after_json, receipt_id "
            f"FROM caveman_events WHERE {where} {_ORDER}",
            tuple(parameters),
        ).fetchall()
        return [
            DreamEvent(
                event_id=row["event_id"],
                ts=datetime.fromisoformat(row["ts_iso"]),
                scope=row["scope"],
                op=DreamOp(row["op"]),
                node_ids=tuple(json.loads(row["node_ids"])),
                before=json.loads(row["before_json"]),
                after=json.loads(row["after_json"]),
                receipt_id=row["receipt_id"],
            )
            for row in rows
        ]

    # -- aliases -----------------------------------------------------------

    def record_alias(self, alias: NodeAlias) -> None:
        """Record a merge so the survivor's history names what it absorbed."""
        self._connection.execute(
            "INSERT INTO caveman_aliases (alias_node_id, survivor_node_id, moved_entry_ids, receipt_id, ts_iso) "
            "VALUES (?,?,?,?,?)",
            (
                alias.alias_node_id,
                alias.survivor_node_id,
                json.dumps(list(alias.moved_entry_ids)),
                alias.receipt_id,
                alias.ts.isoformat(),
            ),
        )
        self._connection.commit()

    def aliases(self, survivor_node_id: str) -> list[NodeAlias]:
        """Everything this node has absorbed, oldest first."""
        rows = self._connection.execute(
            "SELECT alias_node_id, survivor_node_id, moved_entry_ids, receipt_id, ts_iso "
            "FROM caveman_aliases WHERE survivor_node_id = ? ORDER BY rowid_alias",
            (survivor_node_id,),
        ).fetchall()
        return [
            NodeAlias(
                alias_node_id=row["alias_node_id"],
                survivor_node_id=row["survivor_node_id"],
                moved_entry_ids=tuple(json.loads(row["moved_entry_ids"])),
                receipt_id=row["receipt_id"],
                ts=datetime.fromisoformat(row["ts_iso"]),
            )
            for row in rows
        ]

    # -- erasure -----------------------------------------------------------

    def delete_episode(self, episode_id: str) -> list[str]:
        """Erase an episode's entries; returns the node ids that need re-dreaming.

        The union of the deleted entries' ``node_ids``, sorted so the caller's
        own receipt is reproducible. Bindings go with the entries through the
        ``ON DELETE CASCADE``, so there is no second delete to forget.

        A node whose every entry is gone is the caller's problem to notice and
        DELETE rather than re-dream: re-dreaming from nothing would invent
        content. This returns the ids either way, because the ledger cannot tell
        which nodes still have support without the caller asking.
        """
        touched = sorted({node_id for entry in self.for_episode(episode_id) for node_id in entry.node_ids})
        self._connection.execute("DELETE FROM caveman_entries WHERE episode_id = ?", (episode_id,))
        self._connection.commit()
        return touched

    def close(self) -> None:
        """Release the connection. Idempotent, so a context manager and an
        explicit close in a test do not fight."""
        if not self._closed:
            self._connection.close()
            self._closed = True
