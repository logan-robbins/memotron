"""One home for storage behaviour that is identical on SQLite and on Postgres.

What is in here, and what is not
--------------------------------
Everything here was TWO copies whose bodies compared equal after AST
normalisation and docstring stripping, AND that reaches the store only through
members both engines implement. Nothing here touches ``self._connection`` or
``self._engine``, and :mod:`._protocol` is written so that adding something which
does is a visible act rather than a quiet one.

The bar is deliberately narrower than "the copies look the same". Two candidates
that passed the body-equality test were left where they are:

* ``_active_epoch_ancestry`` — three lines, and one of the two edges in the
  DECLARED domain cycle ``_graph <-> _epochs`` in ``scripts/verify/mixin_dag.py``.
  Moving it out of ``postgres/_epochs`` deletes that edge from the call graph, so
  the accepted cycle silently collapses, the justification recorded against it
  becomes dead text, and the layer check flips from SKIP to enforcing an empty
  ``LAYERS`` entry. Three lines is not worth destabilising a deliberate
  architectural declaration.
* ``visible`` — not a method at all. It is a closure defined inside
  ``registry_state_digest`` on both engines that captures the local ``ancestry``;
  it appears in the parity gate's population only because that gate walks every
  function node in the module. There is nothing to extract.

How it composes
---------------
Mixins, inherited by both ``SQLiteStorageBackend`` and ``PostgresStorageBackend``,
so every method still resolves on the composed class and the public surface of
both backends is unchanged. Pure row decoders are module-level functions in
:mod:`._rows` instead, following :mod:`memotron.storage.postgres._common`:
a helper that leaves the mixin call graph cannot re-create plane coupling from a
new direction.

Relationship to #150
--------------------
#150 proposes moving off the mixin idiom. This matches the architecture that
exists today rather than pre-empting a proposal — but it does not make that move
harder: the planes here are already free of engine state, so whatever replaces
the mixins (composition, delegation, a plain object) can take these as-is. They
are the easiest members in the tree to re-home precisely because they depend on a
declared contract and not on a backend.
"""

from __future__ import annotations

from memotron.storage._shared._claims import DREAM_CLAIM_STALE_SECONDS as DREAM_CLAIM_STALE_SECONDS
from memotron.storage._shared._claims import DreamClaimPlaneMixin as DreamClaimPlaneMixin
from memotron.storage._shared._governance import SharedGovernancePlaneMixin as SharedGovernancePlaneMixin
from memotron.storage._shared._graph import SharedGraphPlaneMixin as SharedGraphPlaneMixin
from memotron.storage._shared._projection import UtilityProjectionPlaneMixin as UtilityProjectionPlaneMixin
from memotron.storage._shared._protocol import Row as Row
from memotron.storage._shared._protocol import SharedPlaneBackend as SharedPlaneBackend
from memotron.storage._shared._rows import canonical_visibility_agents as canonical_visibility_agents
from memotron.storage._shared._rows import entity_alias_row_to_dict as entity_alias_row_to_dict
from memotron.storage._shared._rows import epoch_row_to_dict as epoch_row_to_dict
from memotron.storage._shared._rows import predicate_alias_row_to_dict as predicate_alias_row_to_dict
from memotron.storage._shared._rows import tenant_agent_from_row as tenant_agent_from_row
from memotron.storage._shared._rows import tenant_prompt_version_from_row as tenant_prompt_version_from_row
