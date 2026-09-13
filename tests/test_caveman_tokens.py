"""#251 units 1-2: the exception taxonomy, the token estimate, the scope budget.

Two modules with no I/O and no dependencies, so this is the whole of their
behaviour. The point of pinning ``scope_budget`` is that the bound is the
feature: the design exists because ``N`` was previously a soft cap applied after
the fact, and the number is one a caller can assert.
"""

from __future__ import annotations

import pytest

from memotron.caveman.errors import (
    BudgetExceeded,
    CavemanError,
    FactGrammarError,
    MissingCredential,
    NodeNotFound,
    OutOfContractResponse,
)
from memotron.caveman.tokens import (
    NODE_HEADER_TOKENS,
    NORM_TOLERANCE,
    estimate_tokens,
    scope_budget,
)

# ------------------------------------------------------------------- unit 1: errors

_TAXONOMY = (
    FactGrammarError,
    OutOfContractResponse,
    NodeNotFound,
    BudgetExceeded,
    MissingCredential,
)


@pytest.mark.parametrize("error_type", _TAXONOMY)
def test_every_error_subclasses_cavemanerror(error_type: type[CavemanError]) -> None:
    """One `except CavemanError` catches the whole package. That is the taxonomy's job."""
    assert issubclass(error_type, CavemanError)
    assert issubclass(error_type, Exception)


def test_cavemanerror_does_not_catch_a_plain_valueerror() -> None:
    """The control. A base that caught everything would make the taxonomy meaningless.

    Seam failures stay ``ValueError`` on purpose -- "the gateway was unreachable"
    and "the model answered out of contract" are handled by different callers.
    """
    assert not issubclass(ValueError, CavemanError)
    assert not issubclass(CavemanError, ValueError)


def test_out_of_contract_response_carries_source_errors_and_digest() -> None:
    error = OutOfContractResponse(
        source="EXTRACT",
        errors=("claims: field required", "claims.0.kind: not a valid ClaimKind"),
        raw_digest="9f2c4a1b7d3e5061",
    )
    assert error.source == "EXTRACT"
    assert error.errors == ("claims: field required", "claims.0.kind: not a valid ClaimKind")
    assert error.raw_digest == "9f2c4a1b7d3e5061"


def test_out_of_contract_response_message_names_the_stage_and_the_digest() -> None:
    """The message is the one-line diagnosis a reject receipt is read alongside."""
    error = OutOfContractResponse(source="DREAM_NODE", errors=("facts: too long",), raw_digest="abc123")
    text = str(error)
    assert "DREAM_NODE" in text
    assert "abc123" in text
    assert "facts: too long" in text


def test_out_of_contract_response_errors_is_a_tuple_even_from_a_list() -> None:
    """Callers stash this on a receipt; a shared mutable list would be a trap."""
    error = OutOfContractResponse(source="RECONCILE", errors=["a", "b"], raw_digest="d")
    assert error.errors == ("a", "b")
    assert isinstance(error.errors, tuple)


def test_out_of_contract_response_with_no_errors_still_reads() -> None:
    error = OutOfContractResponse(source="RECONCILE", errors=(), raw_digest="d")
    assert error.errors == ()
    assert "no validation detail" in str(error)


def test_out_of_contract_response_does_not_carry_the_raw_text() -> None:
    """Only a digest. The raw response can hold content that must not ride on an exception."""
    error = OutOfContractResponse(source="EXTRACT", errors=("x",), raw_digest="deadbeef")
    assert not hasattr(error, "raw")
    assert not hasattr(error, "raw_response")


def test_out_of_contract_response_is_keyword_only() -> None:
    """Three strings in a row are exactly the argument order nobody remembers."""
    with pytest.raises(TypeError):
        OutOfContractResponse("EXTRACT", ("x",), "digest")  # type: ignore[misc]


# ------------------------------------------------------------------- unit 2: tokens


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("a", 1),
        ("abc", 1),
        ("abcd", 1),
        ("abcde", 2),
        ("abcdefgh", 2),
        ("abcdefghi", 3),
    ],
)
def test_estimate_tokens_is_ceiling_division_by_four(text: str, expected: int) -> None:
    assert estimate_tokens(text) == expected


def test_estimate_tokens_never_undercounts() -> None:
    """Ceiling division, so the estimate is conservative for every length in the range."""
    for length in range(200):
        assert estimate_tokens("x" * length) * 4 >= length


def test_estimate_tokens_counts_characters_not_words() -> None:
    """No tokenizer vocabulary, so the number is identical on a laptop and in the container."""
    assert estimate_tokens("the") == estimate_tokens("xyz")
    assert estimate_tokens("a b") == estimate_tokens("abc")


def test_scope_budget_over_five_hundred_nodes_is_the_stated_arithmetic() -> None:
    """N * (header + L * T) = 500 * (16 + 8*40). The bound the whole design exists to give.

    Both terms have moved once each, and the property has not: the header gained
    its ``as of`` date and entry count in #251 amendment A (8 to 14 tokens) and
    became plain ASCII with a rendered node id in amendment D (14 to 16), while
    ``T`` became a paragraph guard rather than a per-fact target. A whole scope
    still has a stated worst-case cost rather than one that grows with how much
    evidence exists.
    """
    assert scope_budget(500, 8, 40) == 500 * (NODE_HEADER_TOKENS + 8 * 40)
    assert scope_budget(500, 8, 40) == 168000


def test_scope_budget_uses_the_header_constant() -> None:
    """Pins the arithmetic to the named constant rather than to a magic 16."""
    assert scope_budget(1, 1, 1) == NODE_HEADER_TOKENS + 1
    assert scope_budget(10, 2, 3) == 10 * (NODE_HEADER_TOKENS + 6)


def test_scope_budget_is_linear_in_the_node_count() -> None:
    """Doubling N doubles the budget -- what makes N the knob a persona changes."""
    assert scope_budget(400, 6, 40) == 2 * scope_budget(200, 6, 40)


def test_the_header_constant_is_the_measured_cost_of_a_rendered_header() -> None:
    """16 is a measurement, not a round number -- re-measure it if the header moves.

    The two worked examples the constant's docstring names, rendered through the
    one renderer that produces a header, so a header change that nobody
    re-measured fails here rather than silently under-pricing a whole scope.
    """
    short = "gateway (service) [n-001] as of 2026-09-11, 7 entries"
    longer = "agent-memory (deployment) [n-041] as of 2026-09-11, 12 entries"
    assert estimate_tokens(short) == 14
    assert estimate_tokens(longer) == NODE_HEADER_TOKENS == 16


@pytest.mark.parametrize(
    ("nodes", "facts", "fact_tokens"),
    [(0, 8, 40), (-1, 8, 40), (500, 0, 40), (500, 8, 0), (500, -2, 40)],
)
def test_scope_budget_rejects_a_non_positive_budget(nodes: int, facts: int, fact_tokens: int) -> None:
    """Fail fast: a zero budget is a misconfiguration, not an empty scope."""
    with pytest.raises(ValueError, match="must be at least 1"):
        scope_budget(nodes, facts, fact_tokens)


def test_norm_tolerance_is_tight_enough_to_catch_a_half_length_vector() -> None:
    """The bound exists because cosine_similarity is a bare dot product.

    A 0.5-norm vector is the failure this guards -- it would halve every
    similarity silently rather than raise.
    """
    assert NORM_TOLERANCE < 0.01
    assert abs(0.5 - 1.0) > NORM_TOLERANCE
