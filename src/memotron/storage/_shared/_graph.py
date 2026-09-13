"""Graph-plane behaviour that is defined by the contract, not the engine.

One method. ``export`` is a pure re-serialisation of two contract-level reads:
whatever ``nodes()`` and ``relationships()`` return, dumped through pydantic. It
never sees a row, a cursor, or a connection, so the two copies could only ever
have been identical — and if they had drifted, the drift would have been a bug in
one of them rather than a legitimate engine difference.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from memotron.storage._shared._protocol import SharedPlaneBackend

    _Base = SharedPlaneBackend
else:
    _Base = object


class SharedGraphPlaneMixin(_Base):
    """Graph behaviour with no engine-specific part left in it."""

    def export(self) -> dict[str, Any]:
        return {
            "nodes": [node.model_dump(mode="json") for node in self.nodes()],
            "relationships": [relationship.model_dump(mode="json") for relationship in self.relationships()],
        }
