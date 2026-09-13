"""Redaction over sealed content, and the binding that stops proof substitution.

T3-10(b) and T3-10(c). Both were filed as "security-adjacent code with no tests", and
both are the shape where a regression is silent: the code keeps returning success and
the guarantee quietly stops holding.

T3-10(c) — `redact_relationship_version`
----------------------------------------
The only reveal -> mutate -> re-seal round-trip in the client, and it had **zero**
behavioural tests. Measured 2026-08-30: `_crypto.py` lines 254-310 — the whole body past
the scope checks — were uncovered. The scope-guard suites added earlier exercise its
argument handling and stop at `get_relationship`, so coverage looked like progress
without touching what the method does.

The invariant that matters is at `_crypto.py:276-278`. A crypto-shred scope stores its
fact SEALED. Redaction must reveal it, redact the plaintext, and **re-seal** before
writing back. Skip that branch and `stored_redacted_fact` stays plaintext, so the method
writes readable PII into a scope whose whole promise is ciphertext-at-rest — and returns
`{"redacted": True}` either way.

T3-10(b) — `verify_erasure`
---------------------------
`_crypto.py:223` is the only thing tying the caller's question to the certificate's
answer. `erasure.py` re-executes the certificate against `certificate.scope_key`, so
without that check `verify_erasure(scope=B, certificate=cert_for_A)` re-verifies A's
proof and returns True for B — a valid erasure proof for a scope that was never erased.

A NOTE ON WHAT "REDACTED" MEANS HERE, because it is narrower than it sounds
--------------------------------------------------------------------------
`Redactor(RedactionStrategy.REDACT)` is **pattern-based**. Measured: it removes emails,
phone numbers, SSNs and card numbers, and leaves a person's NAME untouched. So
``redact_relationship_version`` on a memory whose PII is "Priya Sharma" returns
``{"redacted": True}`` with the name still present and readable.

That is not a defect in this method — the redactor does what it does — but the gap
between the name and the behaviour is real, so it is pinned by a test below rather than
left to be rediscovered. Same shape as T3-7, where `crypto_shred`'s docstring promised
more than the system delivered.
"""

from __future__ import annotations

import pytest

from memotron.crypto import is_sealed_content
from test_erasure import _client, _ingest_and_form, _memory_rows, _scope

pytestmark = pytest.mark.asyncio

#: Patterned PII the redactor DOES recognise. The fixture body is written so the formed
#: fact carries it verbatim -- verified by the vacuity assertions in `_one_sealed_memory`.
EMAIL_NEEDLE = "priya.sharma@example.com"

#: Un-patterned PII the redactor does NOT recognise. Pinned, not fixed. See the docstring.
NAME_NEEDLE = "Priya Sharma"

REDACTABLE_BODY = (
    f"Memory: subject={NAME_NEEDLE}; predicate=prefers; "
    f"object=reports emailed to {EMAIL_NEEDLE}; "
    "relationship_type=PREFERS; confidence=0.93"
)


async def _one_sealed_memory(tmp_path, scope_id: str = "redaction-test"):
    """A formed memory in a crypto-shred scope, sealed at rest, carrying both needles."""
    scope = _scope(scope_id)
    client = _client(tmp_path)
    await _ingest_and_form(client, scope, body=REDACTABLE_BODY)
    rows = _memory_rows(client, scope)
    assert rows, "formation produced no memory -- every assertion below would be vacuous"
    row = rows[0]
    stored = row.properties.get("fact", "")
    assert is_sealed_content(stored), (
        "the fixture's fact is not sealed, so this file would test the plaintext path "
        "while claiming to test the crypto-shred one"
    )
    revealed = str(client.graph.reveal(scope.key, stored))
    assert EMAIL_NEEDLE in revealed, f"fixture lost the redactable needle: {revealed!r}"
    return client, scope, row


# --------------------------------------------------------------------- T3-10(c)


async def test_redacting_a_sealed_fact_writes_back_sealed_not_plaintext(tmp_path) -> None:
    """The one that matters: a redacted crypto-shred row must still be ciphertext.

    If `seal_content` is skipped the method writes readable PII into a scope whose entire
    promise is that plaintext never lands at rest -- and still reports success.
    """
    client, scope, row = await _one_sealed_memory(tmp_path)

    result = await client.redact_relationship_version(relationship_uuid=row.uuid, scope=scope)
    assert result["redacted"] is True

    stored = client.graph.get_relationship(row.uuid).properties.get("fact", "")
    assert is_sealed_content(stored), (
        "redaction wrote the fact back as PLAINTEXT into a crypto-shred scope. "
        "The re-seal branch at _crypto.py:276-278 did not run."
    )
    assert EMAIL_NEEDLE not in str(stored), "raw PII is readable in the stored value"


async def test_redaction_removes_patterned_pii_from_the_revealed_content(tmp_path) -> None:
    """Sealed at rest is not enough -- the plaintext behind it must actually change.

    Without this, a method that re-sealed the ORIGINAL text would satisfy the test above.
    """
    client, scope, row = await _one_sealed_memory(tmp_path)

    await client.redact_relationship_version(relationship_uuid=row.uuid, scope=scope)

    after = client.graph.get_relationship(row.uuid)
    revealed = str(client.graph.reveal(scope.key, after.properties.get("fact", "")))
    assert EMAIL_NEEDLE not in revealed, f"PII survived redaction: {revealed!r}"
    assert "[REDACTED]" in revealed, f"nothing was substituted: {revealed!r}"


async def test_redaction_does_not_remove_a_name_which_is_a_documented_limitation(tmp_path) -> None:
    """Pins the gap between "redact PII" and what the redactor actually recognises.

    `RedactionStrategy.REDACT` is pattern-based: emails, phones, SSNs, card numbers. A
    person's name has no pattern and survives, while the call still returns
    `{"redacted": True}`. An operator asked to scrub a name would reasonably believe
    this did it.

    Asserted as CURRENT BEHAVIOUR, not as desirable behaviour. If name redaction is ever
    added, this test fails and should be deleted -- that is the intended signal.
    """
    client, scope, row = await _one_sealed_memory(tmp_path)

    result = await client.redact_relationship_version(relationship_uuid=row.uuid, scope=scope)
    assert result["redacted"] is True

    after = client.graph.get_relationship(row.uuid)
    revealed = str(client.graph.reveal(scope.key, after.properties.get("fact", "")))
    assert NAME_NEEDLE in revealed, (
        "a name is now redacted -- the limitation this test pins has been fixed. "
        "Delete this test and update T3-10 in TAKEOVER-BACKLOG.md."
    )


async def test_redaction_refreshes_the_fact_commitment(tmp_path) -> None:
    """The keyed commitment must track the REDACTED text.

    A stale commitment still verifies against the old plaintext, so tamper-evidence would
    be asserting something about content that no longer exists.
    """
    client, scope, row = await _one_sealed_memory(tmp_path)
    before = row.properties.get("fact_commitment")
    assert before, "fixture row carries no fact_commitment -- this test would prove nothing"

    await client.redact_relationship_version(relationship_uuid=row.uuid, scope=scope)

    after = client.graph.get_relationship(row.uuid).properties.get("fact_commitment")
    assert after is not None, "fact_commitment disappeared"
    assert after != before, "fact_commitment was not refreshed; it still commits to the pre-redaction text"


async def test_redaction_preserves_the_audit_trail(tmp_path) -> None:
    """Redact-a-version, not delete-a-version: who/when/lineage must survive."""
    client, scope, row = await _one_sealed_memory(tmp_path)
    audit_keys = [
        k
        for k in ("status", "valid_from", "valid_to", "created_by", "episode_uuids", "scope_key")
        if k in row.properties
    ]
    assert audit_keys, "fixture row carries no audit fields -- this test would prove nothing"
    before = {k: row.properties[k] for k in audit_keys}

    await client.redact_relationship_version(relationship_uuid=row.uuid, scope=scope)

    after_props = client.graph.get_relationship(row.uuid).properties
    for key, value in before.items():
        assert after_props.get(key) == value, f"redaction altered the audit field {key!r}"
    assert after_props.get("pii_redacted") is True
    assert after_props.get("pii_redact_reason")


async def test_a_shredded_scope_cannot_be_redacted(tmp_path) -> None:
    """Once the DEK is destroyed the content is unrecoverable, so redaction must refuse.

    Silently succeeding would write a redaction receipt asserting a before-digest for
    content nobody can read.
    """
    client, scope, row = await _one_sealed_memory(tmp_path)
    await client.crypto_shred(scope=scope)

    with pytest.raises(ValueError, match="already unrecoverable"):
        await client.redact_relationship_version(relationship_uuid=row.uuid, scope=scope)


# --------------------------------------------------------------------- T3-10(b)


async def test_an_erasure_certificate_cannot_be_replayed_against_another_scope(tmp_path) -> None:
    """Proof substitution: A's certificate must not verify an un-erased scope B.

    `erasure.py` re-executes the certificate against `certificate.scope_key`, so without
    the binding at `_crypto.py:223` this returns True for B -- a valid-looking erasure
    proof for a scope that still holds all of its data.
    """
    erased = _scope("erasure-proof-a")
    intact = _scope("erasure-proof-b")
    client = _client(tmp_path)

    await _ingest_and_form(client, erased, body=REDACTABLE_BODY, name="a1")
    await _ingest_and_form(client, intact, body=REDACTABLE_BODY, name="b1")
    assert _memory_rows(client, intact), "scope B has no data, so 'not erased' would be meaningless"

    await client.crypto_shred(scope=erased)
    certificate = await client.erasure_certificate(scope=erased)

    # Sanity: the certificate really is valid for the scope it was issued for, so a
    # failure below is the binding and not a broken certificate.
    assert await client.verify_erasure(scope=erased, certificate=certificate) is True

    with pytest.raises(ValueError, match="does not match requested scope"):
        await client.verify_erasure(scope=intact, certificate=certificate)
