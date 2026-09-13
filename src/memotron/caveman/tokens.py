"""The deterministic token estimate and the N x L x T budget arithmetic.

``(len(text) + 3) // 4`` -- ceiling division, ~4 chars per token. Duplicated per
the house convention ``context.py:202-211`` states explicitly: the formula is
small enough to duplicate rather than share a cross-module import, and
``client/_profile.py:582`` carries its own copy for the same reason. No
``tiktoken`` dependency, so the number is identical on a laptop and in the
container, and it is conservative by construction.

The budget arithmetic lives with the estimator rather than with the renderer
because it is arithmetic over counts, not over text: ``scope_budget`` never
looks at a fact.
"""

from __future__ import annotations

NODE_HEADER_TOKENS = 16
"""Token cost of a rendered node header, aliases excluded.

``name (type) [id] as of YYYY-MM-DD, N entries`` -- measured against
:func:`estimate_tokens` over the same two worked examples the pipe-delimited
header was measured over: 53 characters (14 tokens) for ``gateway (service)
[n-001] as of 2026-09-11, 7 entries``, and 62 (16) for the same header on
``agent-memory`` with a longer type and a two-digit count. 16 is the wider of
the two, as 14 was for the header this replaces (#251 amendment D).

**The ``aka`` clause is deliberately outside this number.** An alias list is
unbounded -- reconcile records every surface name it routes, and a merge unions
what it absorbs -- so no constant can cover it, and a worst case that pretended
to would be the one number in this file a caller could not rely on. What
:func:`scope_budget` bounds is the fixed part of the header plus the facts.

The date and the count are rendered rather than derived because a reader cannot
derive either: staleness is not in the block, and the entry count lives in the
ledger.
"""

NORM_TOLERANCE = 1e-3
"""How far a stored embedding's L2 norm may deviate from 1.0.

``memotron.embedding.cosine_similarity`` is a bare dot product that assumes
unit-length inputs (``embedding.py:262-269``), so this is the fail-fast bound the
graph enforces on every write rather than a value anything clamps to. Both live
transports already return unit vectors; a vector that misses this bound came
from somewhere unexpected.
"""


def estimate_tokens(text: str) -> int:
    """Deterministic ~4-chars-per-token estimate, ceiling division.

    ``""`` is 0, a 4-character string is 1, a 5-character string is 2.
    """
    return (len(text) + 3) // 4


def scope_budget(max_nodes: int, max_facts_per_node: int, max_fact_tokens: int) -> int:
    """Worst-case token cost of rendering an entire scope at full occupancy.

    ``N * (header + L * T)``. At the field defaults (500 x 8 x 40) that is
    ``500 * (16 + 320)`` = **168,000 tokens** -- a number a caller can assert,
    which is the whole point of bounding the node count instead of soft-capping
    it after the fact. The header term moved from 14 to 16 with the plain-ASCII
    header (#251 amendment D); ``T`` has been a paragraph guard rather than a
    per-fact target since amendment A, so this worst case is reached only by a
    scope whose every fact is a full paragraph.

    Relations are outside this bound, as aliases are: both are per-scope
    quantities the node count does not determine.
    ``CavemanMotive.max_edge_types`` bounds how many KINDS of edge a scope may
    hold, not how many edges -- an edge is evidence about a pair, and bounding
    evidence is what the ledger exists to make unnecessary.

    Argument names match the :class:`~memotron.caveman.motive.CavemanMotive`
    fields they come from, so a caller reads
    ``scope_budget(m.max_nodes, m.max_facts_per_node, m.max_fact_tokens)``.
    """
    if max_nodes < 1:
        raise ValueError("max_nodes must be at least 1")
    if max_facts_per_node < 1:
        raise ValueError("max_facts_per_node must be at least 1")
    if max_fact_tokens < 1:
        raise ValueError("max_fact_tokens must be at least 1")
    return max_nodes * (NODE_HEADER_TOKENS + max_facts_per_node * max_fact_tokens)
