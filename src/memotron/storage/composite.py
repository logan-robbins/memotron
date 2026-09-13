"""Composite backend for split deployments (two stores, two engines).

``SplitStorageBackend`` presents the full
:class:`~memotron.storage.base.StorageBackend` surface to callers while
routing each call to the store that owns it: Memory Graph surface methods go
to the graph backend, everything else to the operational backend.

Cross-plane operations follow the documented cross-store rule: they anchor in
the Operational Store, whose implementation holds a reference to the Memory
Graph plane (passed at construction by the factory) for its relationship
reads and idempotent graph writes.  There are no foreign keys across the
store boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from memotron.storage.base import (
    MemoryGraphStorage,
    OperationalStorage,
    StorageBackend,
)

_MEMORY_GRAPH_SURFACE = frozenset(MemoryGraphStorage.__abstractmethods__)
_OPERATIONAL_SURFACE = frozenset(OperationalStorage.__abstractmethods__)


class SplitStorageBackend:
    """Route the full store surface across two engine-specific backends.

    The operational backend must have been constructed with its
    ``memory_graph`` reference pointing at *memory_graph* so its cross-plane
    operations (use-event validation, prune-ghost restore, tenant purge) read
    and write the graph through the interface.
    """

    def __init__(
        self,
        *,
        operational: OperationalStorage,
        memory_graph: MemoryGraphStorage,
    ) -> None:
        if not isinstance(operational, OperationalStorage):
            raise TypeError(f"operational plane {type(operational).__name__} does not implement OperationalStorage")
        if not isinstance(memory_graph, MemoryGraphStorage):
            raise TypeError(f"memory-graph plane {type(memory_graph).__name__} does not implement MemoryGraphStorage")
        # Fail fast on the mis-wiring foot-gun: the operational plane's
        # cross-plane operations (use-event validation, prune-ghost restore,
        # tenant purge) must read and write the graph through THIS composite's
        # graph plane, not through the operational engine's own (empty) graph
        # surface.  Implementations expose that reference as ``_memory_graph``.
        wired = getattr(operational, "_memory_graph", None)
        if wired is not None and wired is not memory_graph:
            raise ValueError(
                "operational plane was not constructed with this composite's "
                "memory-graph plane; build split deployments through "
                "memotron.storage.create_storage_backend"
            )
        self._operational = operational
        self._memory_graph = memory_graph

    # Every interface method is resolved through the ownership map derived
    # from the two surface ABCs, so adding a method to either surface
    # automatically routes here without another edit.
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in _MEMORY_GRAPH_SURFACE:
            return getattr(self._memory_graph, name)
        if name in _OPERATIONAL_SURFACE or name in {"receipts", "key_manager"}:
            return getattr(self._operational, name)
        raise AttributeError(
            f"{type(self).__name__} has no attribute {name!r}; the StorageBackend "
            "surface is defined in memotron.storage.base"
        )

    # -- explicit overrides ----------------------------------------------------

    def close(self) -> None:
        """Close both stores (the graph plane last: operational anchors)."""
        try:
            self._operational.close()
        finally:
            self._memory_graph.close()

    def purge_tenant_state(
        self,
        tenant_id: str,
        *,
        agent_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Anchor the purge in the Operational Store.

        The operational backend drives the Memory Graph deletions through its
        graph-plane reference using the idempotent deletion primitives, so a
        partial failure is recovered by re-running the purge.
        """
        return self._operational.purge_tenant_state(tenant_id, agent_ids=agent_ids)


def _verify_routing_completeness() -> None:
    """Import-time guard: virtual subclassing skips ABC enforcement, so prove
    the ownership maps plus explicit overrides cover the whole contract.

    Fails the moment someone extends a surface ABC (or adds a method claimed
    by both surfaces) without teaching the composite how to route it — at
    import, not at first call in a split deployment.
    """
    explicit = {
        name for name, member in vars(SplitStorageBackend).items() if not name.startswith("_") and callable(member)
    }
    contract = set(StorageBackend.__abstractmethods__)
    unrouted = contract - _MEMORY_GRAPH_SURFACE - _OPERATIONAL_SURFACE - explicit
    if unrouted:
        raise TypeError(f"SplitStorageBackend cannot route these StorageBackend methods: {sorted(unrouted)}")
    ambiguous = (_MEMORY_GRAPH_SURFACE & _OPERATIONAL_SURFACE) - explicit
    if ambiguous:
        raise TypeError(
            "these methods are claimed by both storage surfaces and must be "
            f"explicitly overridden on SplitStorageBackend: {sorted(ambiguous)}"
        )


_verify_routing_completeness()

# A SplitStorageBackend satisfies the StorageBackend contract by delegation.
StorageBackend.register(SplitStorageBackend)
