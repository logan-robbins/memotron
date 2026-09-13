"""Row-decoding primitives shared by every Postgres plane.

Why this module exists
----------------------
``as_json`` was a ``@staticmethod`` on ``GraphPlaneMixin`` and was called from **three other
planes** -- ``_operational`` (14 sites), ``_governance`` (2) and ``_graph`` itself (3). Decoding a
``jsonb`` column has nothing to do with the graph, so every one of those calls was a plane reaching
into another plane for a utility.

That mattered beyond tidiness: ``mixin_dag`` measures calls *between mixins*, and this single
helper put ``_governance`` inside a three-module cycle with ``_epochs`` and ``_graph``. The other
edges in that cycle are genuine domain coupling and are declared as such in
``scripts/verify/mixin_dag.py``; this one was not, and it is the only one that had no domain
reason to exist.

A module-level function rather than a mixin method, deliberately: it leaves the mixin call graph
entirely, so it cannot re-create the same coupling from a new direction. This matches
``dreaming/_common.py``, ``client/_common.py`` and ``agent_memory/_common.py``, which are all
module-level helpers rather than mixins.

Known divergence, deliberately not resolved here
------------------------------------------------
``_policy.py`` carries its own ``_as_json(value, label)`` which raises ``RuntimeError`` naming the
column when the text is unparseable, mirroring the SQLite backend's corruption report. This one is
lenient and lets ``json.JSONDecodeError`` escape. Unifying them changes behaviour for 19 call
sites and needs a test that pins which error a caller should see -- that is a Phase 3 change, not
a move. Kept byte-identical to the original here so the move is provable.
"""

from __future__ import annotations

import json
from typing import Any


def as_json(value: Any) -> Any:
    """jsonb arrives already decoded; tolerate a text column too."""
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value
