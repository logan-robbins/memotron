"""Shared primitives every dream-cycle concern depends on.

Holds the module logger, the content-plane crypto wrapper, and the fail-closed
decision set. Small on purpose: anything here is imported by most of the package,
so growth costs coupling everywhere."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from memotron.agents import (
    LocalDreamAgentTransport,
)
from memotron.crypto import (
    content_commitment,
    seal_content,
    seal_json,
)
from memotron.retrieval import (
    strip_negation_markers,
)

# A STRING LITERAL, not __name__. In this submodule __name__ is
# "memotron.dreaming._common", which would rename every record the package
# emits -- breaking any ops filter and, concretely,
# tests/test_dream_agent_transport.py:795, which does
# caplog.at_level(..., logger="memotron.dreaming"). R-S4 of the split contract.
_log = logging.getLogger("memotron.dreaming")


@dataclass(frozen=True)
class _ContentProtection:
    """WS-12: per-scope content-plane protection active under CRYPTO_SHRED governance.

    Bundles the scope DEK with the sealing/commitment operations the write path
    applies to every content-plane field.  Sealed fields (AES-256-GCM) are
    recoverable only while the DEK lives; committed fields (keyed HMAC blind
    indexes) support equality matching and index seeks over ciphertext and
    become unlinkable once the DEK is destroyed.
    """

    key: bytes

    def seal(self, text: str) -> str:
        return seal_content(text, self.key)

    def seal_optional(self, text: str | None) -> str | None:
        return None if text is None else seal_content(text, self.key)

    def seal_vector(self, vector: list[float]) -> str:
        return seal_json(vector, self.key)

    def commit(self, purpose: str, text: str) -> str:
        return content_commitment(self.key, purpose, text)


_DREAM_AGENT_FALLBACK_TRANSPORT = LocalDreamAgentTransport()


_FAIL_CLOSED_DECISION_TYPES: frozenset[str] = frozenset(
    {
        "formation_untrusted_write_gated",
        "pruning_relationship_pruned",
        "pruning_context_budget_exceeded",
        "pruning_single_active_repair",
    }
)


# WS-25 T1: negation/polarity primitives and the object-independent truth-slot
# helpers now live in ``retrieval.py`` (single source of truth for both the
# write-side polarity-conflict gate below and the read-side within-slot
# tiebreaker) — imported here under their historical private names so every
# existing call site is unchanged.  ``retrieval.py`` must stay free of any
# import of this module (``dreaming.py`` already imports ``use_need`` from
# it), so the primitives could not live here without a cycle.
_strip_negation_markers = strip_negation_markers
