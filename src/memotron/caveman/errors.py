"""The caveman exception taxonomy.

One place a caller can ``except CavemanError``. Imported by everything in the
package; imports nothing from it.

Four of the six names do not end in ``Error``. That is deliberate and comes from
the design document, which names them in the contract every work package builds
against — so each carries a targeted ``# noqa: N818`` rather than being renamed
out from under its callers. ``N818`` is a naming convention; the names here are a
published contract.
"""

from __future__ import annotations

from collections.abc import Sequence


class CavemanError(Exception):
    """Base for every error this package raises on its own behalf.

    Errors raised by the seams it composes keep their own types: a gateway
    transport failure is a ``ValueError`` (see
    :class:`memotron.gateway.OpenAICompatibleTransport`), because the caller
    that has to handle "the model was unreachable" is not the caller that has to
    handle "the model answered out of contract".
    """


class FactGrammarError(CavemanError):
    """A string is not usable as the text of a fact.

    Raised by :func:`memotron.caveman.render.validate_fact_text` for a blank
    or padded string, an embedded newline, text over the motive's paragraph
    guard, or a hedge word that the ``unsure`` fact kind exists to express
    instead.

    Named for facts rather than for lines since #251 amendment D: there is no
    line grammar any more, only fact text under one policy. ``lines.py`` and its
    sigil alphabet are gone.
    """


class NodeNotFound(CavemanError):  # noqa: N818 - contract name, see the module docstring
    """A node id is not present in the graph.

    Raised by ``GraphStore.get_node``. Distinct from "the scope is empty": a
    caller holding an id that does not resolve has stale state, which is a
    defect rather than an empty result.
    """


class BudgetExceeded(CavemanError):  # noqa: N818 - contract name, see the module docstring
    """A hard numeric invariant of the graph would be broken by this write.

    Two uses, both fail-fast rather than clamp:

    * an embedding whose L2 norm is not 1.0 within tolerance —
      ``memotron.embedding.cosine_similarity`` is a bare dot product that
      *assumes* normalisation, so an unnormalised vector would silently corrupt
      every ranking rather than fail;
    * a node fact set over the motive's ``max_facts_per_node``.
    """


class MissingCredential(CavemanError):  # noqa: N818 - contract name, see the module docstring
    """A live run was attempted with no gateway key configured.

    The transports themselves raise ``ValueError`` naming only the environment
    variable (never a value); this exists for the composition root's own
    precondition check, so "you have not configured a key" is distinguishable
    from "the gateway refused the key you have".
    """


class OutOfContractResponse(CavemanError):  # noqa: N818 - contract name, see the module docstring
    """An LLM answered with something the stage's response model rejects.

    Carries what a prompt regression needs and nothing that could leak content:
    *source* names the stage, *errors* holds the first validation messages, and
    *raw_digest* is a truncated sha256 of the raw response, which is also the
    ``outputs_digest`` of the reject receipt emitted before this is raised. The
    raw text itself is deliberately absent.

    **There is no repair pass.** A contract violation is a defect in the prompt
    or the model choice and surfaces as one; transport-level retry is
    :meth:`memotron.gateway.OpenAICompatibleTransport._post_with_retry`'s job.
    """

    def __init__(self, *, source: str, errors: Sequence[str], raw_digest: str) -> None:
        self.source = source
        self.errors: tuple[str, ...] = tuple(errors)
        self.raw_digest = raw_digest
        detail = "; ".join(self.errors) if self.errors else "no validation detail"
        super().__init__(f"{source} answered out of contract [{raw_digest}]: {detail}")
