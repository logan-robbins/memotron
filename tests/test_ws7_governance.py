"""WS-7: Enterprise governance, multi-tenancy & RTBF acceptance tests.

All four tests exercise opt-in behaviour only.  Nothing in these tests requires
changes to existing test infrastructure; every test builds its own isolated
tmp_path client.

Test matrix
-----------
1. crypto_shred       — key destruction erases content; audit skeleton survives.
2. pii_redaction      — high-sensitivity policy removes PII before materialization.
3. untrusted_gate     — untrusted directive writes are auditably gated.
4. read_only_scope    — read-only scope rejects writes; reads still work.
"""

from __future__ import annotations

import pytest

from memotron import (
    SHREDDED_CONTENT_PLACEHOLDER,
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    Memotron,
    EpisodeType,
    ErasureBehavior,
    GovernancePolicy,
    MemoryScope,
    NodeInstruction,
    PiiSensitivity,
    RelationshipInstruction,
    ScopeKind,
    is_sealed_content,
    open_content,
)
from memotron.models import DreamJobKind, RelationshipCardinality

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scope(kind: str = "user", scope_id: str = "ws7-test") -> MemoryScope:
    return MemoryScope(kind=ScopeKind(kind), scope_id=scope_id)


def _make_config(
    *,
    governance: GovernancePolicy | None = None,
    read_only_scopes: set[str] | None = None,
    require_untrusted_gate: bool = False,
) -> DreamConfig:
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Find durable entities worth remembering.",
                properties=("kind", "role"),
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="Remember stable preferences.",
            ),
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="Remember explicit requirements.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
            ),
            RelationshipInstruction(
                type="SHOULD",
                source_label="Entity",
                target_label="Entity",
                query="Remember lessons that improve agent behaviour.",
            ),
        ),
    )
    return DreamConfig(
        instruction_sets=(instructions,),
        jobs=(
            DreamJob(
                name="formation-default",
                kind=DreamJobKind.FORMATION,
                cadence_seconds=1,
            ),
        ),
        governance=governance,
        read_only_scopes=read_only_scopes or set(),
        require_dream_agent_approval_for_untrusted_directives=require_untrusted_gate,
    )


def _make_client(tmp_path, config: DreamConfig) -> Memotron:
    return Memotron(config=config, graph_path=tmp_path / "graph.sqlite")


# ---------------------------------------------------------------------------
# Test 1: Crypto-shred erases content but keeps audit skeleton
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crypto_shred_erases_scope_content_but_keeps_audit(tmp_path):
    """Destroy the encryption key → ciphertext unrecoverable; row audit intact.

    We go through the full formation path (add_episode + run_dream_job) because
    the governance parameter is threaded through _run_formation → _materialize_episode.
    The episode body uses the Memory: line format so the RuleBasedExtractionTransport
    can deterministically extract the fact we want.
    """
    scope = _make_scope(scope_id="customer-001")
    gov = GovernancePolicy(
        pii_sensitivity=PiiSensitivity.NONE,
        erasure_behavior=ErasureBehavior.CRYPTO_SHRED,
    )
    client = _make_client(tmp_path, _make_config(governance=gov))

    # Step 1: provision a governance key for this scope before any content is ingested.
    # This ensures _materialize_episode finds the key and stores encrypted_content.
    key_before_shred = client.provision_governance_key(scope=scope)
    assert len(key_before_shred) == 32

    # Step 2: ingest an episode using Memory: line format for deterministic extraction.
    await client.add_episode(
        name="crypto-shred-test-episode",
        episode_body=(
            "Memory: subject=Alice; predicate=requires; object=two-factor authentication; "
            "relationship_type=REQUIRES; confidence=0.95"
        ),
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="governance test",
    )

    # Step 3: run formation — _run_formation passes governance to _materialize_episode
    # which will see the governance key and store encrypted_content.
    await client.run_dream_job(job_name="formation-default")

    # WS-12: find the materialized relationship by revealing the sealed fact —
    # the STORED fact is ciphertext-only for a crypto-shred scope.
    target_rel = None
    for rel in client.graph.relationships():
        if rel.type == "MENTIONS":
            continue
        if rel.properties.get("scope_key") != scope.key:
            continue
        fact = str(client.graph.reveal(scope.key, rel.properties.get("fact", "")))
        if "alice" in fact.lower() or "two-factor" in fact.lower():
            target_rel = rel
            break

    assert target_rel is not None, (
        "Expected a relationship to be materialized for Alice's requirement. "
        f"Relationships found: {[r.properties.get('fact') for r in client.graph.relationships() if r.type != 'MENTIONS']}"
    )

    # WS-12: the content plane is sealed at rest — no plaintext fact column exists.
    stored_fact = target_rel.properties.get("fact")
    assert is_sealed_content(stored_fact), (
        f"Expected the stored fact to be a sealed AES-256-GCM token under crypto_shred governance; got: {stored_fact!r}"
    )

    # Decrypt with the key we captured before shredding — should work.
    decrypted = open_content(stored_fact, key_before_shred)
    assert "alice" in decrypted.lower() or "two-factor" in decrypted.lower(), (
        f"Decrypted content should contain the original fact; got: {decrypted!r}"
    )

    # Reads decrypt-on-read while the key lives: search returns plaintext.
    live_results = await client.search(query="two-factor authentication", scope=scope)
    assert any("two-factor" in result.fact.lower() for result in live_results), (
        "search should reveal sealed content while the scope DEK lives"
    )

    # Step 4: shred the key.
    shred_result = await client.crypto_shred(scope=scope)
    assert shred_result["shredded"] is True
    assert shred_result["scope_key"] == scope.key
    assert shred_result["relationships_affected"] >= 1

    # Step 5: assert the key is gone.
    assert client.graph.get_governance_key(scope.key) is None, (
        "After crypto_shred, get_governance_key should return None"
    )

    # Step 6: the relationship ROW still exists (audit skeleton preserved).
    rel_after = client.graph.get_relationship(target_rel.uuid)
    assert rel_after is not None, "Relationship row must survive crypto-shred (audit skeleton)"
    assert rel_after.properties.get("status") is not None, "status field must be present in audit row"

    # Step 7: prove content is unrecoverable — the stored ciphertext survives but
    # every read surface resolves to the shredded placeholder, never plaintext.
    assert client.graph.get_governance_key(scope.key) is None
    assert client.graph.reveal(scope.key, rel_after.properties["fact"]) == SHREDDED_CONTENT_PLACEHOLDER
    post_shred_results = await client.search(query="two-factor authentication", scope=scope)
    assert not any("two-factor" in result.fact.lower() for result in post_shred_results), (
        "post-shred search must never return the erased plaintext"
    )


# ---------------------------------------------------------------------------
# Test 2: PII redacted before materialization for high sensitivity type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pii_redacted_before_materialization_for_high_sensitivity_type(tmp_path):
    """Episode body with PII is redacted before extraction under pii_sensitivity=high."""
    scope = _make_scope(scope_id="pii-customer-001")
    gov = GovernancePolicy(pii_sensitivity=PiiSensitivity.HIGH)
    client = _make_client(tmp_path, _make_config(governance=gov))

    # Build an episode body containing both an email and a phone number.
    pii_body = (
        "Agent preference log: agent-7 prefers not to share personal data.\n"
        "Contact: reach Bob at bob@example.com or call 555-867-5309 for support.\n"
        "Policy: agent-7 should mask PII in responses."
    )

    # Ingest and queue for formation.
    await client.add_episode(
        name="pii-test-episode",
        episode_body=pii_body,
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="governance test",
    )

    # Run the formation job to materialize memories from the episode.
    await client.run_dream_job(job_name="formation-default")

    # Examine persisted relationships for PII leakage.
    for rel in client.graph.relationships():
        if rel.type == "MENTIONS":
            continue
        if rel.properties.get("scope_key") != scope.key:
            continue
        fact = str(rel.properties.get("fact", ""))
        predicate = str(rel.properties.get("predicate", ""))
        obj = str(client.graph.get_node(rel.target_uuid).properties.get("name", ""))

        # None of the materialized text fields should contain raw PII.
        assert "bob@example.com" not in fact, f"Raw email PII found in fact: {fact!r}"
        assert "bob@example.com" not in predicate, f"Raw email PII found in predicate: {predicate!r}"
        assert "bob@example.com" not in obj, f"Raw email PII found in object: {obj!r}"
        assert "555-867-5309" not in fact, f"Raw phone PII found in fact: {fact!r}"
        assert "555-867-5309" not in predicate, f"Raw phone PII found in predicate: {predicate!r}"
        assert "555-867-5309" not in obj, f"Raw phone PII found in object: {obj!r}"


# ---------------------------------------------------------------------------
# Test 3: Untrusted directive write is gated and auditable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_untrusted_directive_write_is_gated(tmp_path):
    """Episodes with trusted=False trigger the untrusted-write gate for directives.

    We use the Memory: line format so the RuleBasedExtractionTransport deterministically
    produces a SHOULD (directive) memory, which the gate must inspect.
    """
    scope = _make_scope(scope_id="gate-test")
    client = _make_client(
        tmp_path,
        _make_config(require_untrusted_gate=True),
    )

    # Ingest an untrusted episode that proposes a SHOULD (directive) memory.
    # Use the Memory: line format so extraction is deterministic.
    await client.add_episode(
        name="external-directive-episode",
        episode_body=(
            "Memory: subject=agent-7; predicate=should always escalate; "
            "object=compliance issues to legal team; relationship_type=SHOULD; confidence=0.9"
        ),
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="external-source",
        trusted=False,  # WS-7: mark this episode untrusted
    )

    # Run formation — the gate should fire for the SHOULD (directive) memory.
    result = await client.run_dream_job(job_name="formation-default")
    assert len(result.job_runs) == 1

    # The gate decision must have been recorded in the dream_decisions log.
    decisions = await client.dream_decisions(limit=100)
    gate_decisions = [d for d in decisions if d.decision_type == "formation_untrusted_write_gated"]
    assert len(gate_decisions) >= 1, (
        "Expected at least one 'formation_untrusted_write_gated' decision to be recorded. "
        f"Recorded decision types: {[d.decision_type for d in decisions]}"
    )
    # Each gate decision should record the episode's untrusted status.
    for gd in gate_decisions:
        assert gd.details.get("episode_trusted") is False


# ---------------------------------------------------------------------------
# Test 4: Read-only scope rejects writes but allows reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_only_scope_rejects_writes(tmp_path):
    """Scope in read_only_scopes raises ValueError on write; reads still work."""
    ro_scope = _make_scope(kind="agent", scope_id="shared-kb")
    rw_scope = _make_scope(kind="user", scope_id="regular-user")

    config = _make_config(read_only_scopes={ro_scope.key})
    client = _make_client(tmp_path, config)

    # --- Write to the RW scope works fine (control case). ---
    rw_result = await client.add_memory(
        subject="agent-7",
        predicate="prefers",
        object="concise responses",
        relationship_type="PREFERS",
        scope=rw_scope,
        confidence=0.9,
    )
    assert rw_result.relationship_uuid

    # --- Write to the RO scope must fail fast with ValueError. ---
    with pytest.raises(ValueError, match="read-only"):
        await client.add_memory(
            subject="agent-7",
            predicate="requires",
            object="some policy",
            relationship_type="REQUIRES",
            scope=ro_scope,
            confidence=0.9,
        )

    with pytest.raises(ValueError, match="read-only"):
        await client.add_episode(
            name="blocked-episode",
            episode_body="This should be blocked.",
            source=EpisodeType.TEXT,
            scope=ro_scope,
        )

    with pytest.raises(ValueError, match="read-only"):
        await client.add_context(
            name="blocked-context",
            content="Some content that should not be ingested.",
            scopes=[ro_scope],
        )

    # --- Reads on the read-only scope still work (no restriction on reads). ---
    # (The scope has no data but search/profile should not raise.)
    search_results = await client.search(
        query="some policy",
        scope=ro_scope,
    )
    assert isinstance(search_results, list)

    profile_result = await client.profile(scope=ro_scope)
    assert profile_result.scope == ro_scope


# ---------------------------------------------------------------------------
# WS-21 T27 — operator-configurable redaction
# ---------------------------------------------------------------------------


def _scope_facts(client: Memotron, scope: MemoryScope) -> list[str]:
    return [
        str(rel.properties.get("fact", ""))
        for rel in client.graph.relationships()
        if rel.type != "MENTIONS" and rel.properties.get("scope_key") == scope.key
    ]


@pytest.mark.asyncio
async def test_custom_pattern_redacts_episode_body_and_receipt_names_pattern(tmp_path):
    """An operator regex fires on the episode body pre-extraction; the redaction
    receipt names the strategy and the matched pattern NAMES, never content."""
    import json as _json

    from memotron.receipts import ReceiptDecisionType

    scope = _make_scope(scope_id="custom-pattern-001")
    gov = GovernancePolicy(
        pii_sensitivity=PiiSensitivity.HIGH,
        custom_pii_patterns={"employee_badge": r"BADGE-\d{6}"},
    )
    client = _make_client(tmp_path, _make_config(governance=gov))

    await client.add_episode(
        name="badge-episode",
        episode_body=(
            "Memory: subject=agent-7; predicate=prefers; object=escalation via BADGE-123456; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="governance test",
    )
    await client.run_dream_job(job_name="formation-default")

    facts = _scope_facts(client, scope)
    assert facts, "expected a materialized fact"
    assert all("BADGE-123456" not in fact for fact in facts), facts
    assert any("[REDACTED]" in fact for fact in facts), facts

    redaction_receipts = [
        receipt
        for receipt in await client.memory_receipts(scope=scope)
        if receipt.decision_type == ReceiptDecisionType.FORMATION_GOVERNANCE_REDACTED
    ]
    assert redaction_receipts, "expected a FORMATION_GOVERNANCE_REDACTED receipt"
    payload = _json.loads(redaction_receipts[0].event_payload)
    assert payload["redaction_strategy"] == "redact"
    assert "employee_badge" in payload["matched_patterns"]
    assert "BADGE-123456" not in redaction_receipts[0].event_payload


@pytest.mark.asyncio
async def test_custom_pattern_redacts_extracted_fields(tmp_path):
    """The belt-and-suspenders field pass uses the same operator patterns when
    the extractor itself carries PII through."""

    class _PiiEmittingTransport:
        async def extract_memories(self, request):
            return [
                {
                    "subject": "agent-7",
                    "predicate": "prefers",
                    "object": "callbacks via BADGE-654321",
                    "relationship_type": "PREFERS",
                    "confidence": 0.9,
                }
            ]

    scope = _make_scope(scope_id="custom-pattern-002")
    gov = GovernancePolicy(
        pii_sensitivity=PiiSensitivity.HIGH,
        custom_pii_patterns={"employee_badge": r"BADGE-\d{6}"},
    )
    client = Memotron(
        config=_make_config(governance=gov),
        graph_path=tmp_path / "fields.sqlite",
        extraction_transport=_PiiEmittingTransport(),
    )
    await client.add_episode(
        name="clean-body-episode",
        episode_body="No inline pii in this body.",
        source=EpisodeType.TEXT,
        scope=scope,
    )
    await client.run_dream_job(job_name="formation-default")

    facts = _scope_facts(client, scope)
    assert facts, "expected a materialized fact"
    assert all("BADGE-654321" not in fact for fact in facts), facts
    assert any("[REDACTED]" in fact for fact in facts), facts


@pytest.mark.asyncio
async def test_mask_last_4_and_sha256_strategies_via_config(tmp_path):
    """MASK_LAST_4 and SHA256_HASH are selectable through GovernancePolicy."""
    from memotron.redaction import RedactionStrategy

    body = (
        "Memory: subject=Bob; predicate=prefers; object=contact at bob@example.com; "
        "relationship_type=PREFERS; confidence=0.9"
    )

    mask_scope = _make_scope(scope_id="mask-001")
    mask_client = _make_client(
        tmp_path / "mask",
        _make_config(
            governance=GovernancePolicy(
                pii_sensitivity=PiiSensitivity.HIGH,
                redaction_strategy=RedactionStrategy.MASK_LAST_4,
            )
        ),
    )
    await mask_client.add_episode(name="mask-episode", episode_body=body, source=EpisodeType.TEXT, scope=mask_scope)
    await mask_client.run_dream_job(job_name="formation-default")
    mask_facts = _scope_facts(mask_client, mask_scope)
    assert mask_facts
    assert all("bob@example.com" not in fact for fact in mask_facts), mask_facts
    # MASK_LAST_4 keeps exactly the trailing 4 characters of the span.
    assert any("*" * (len("bob@example.com") - 4) + ".com" in fact for fact in mask_facts), mask_facts

    hash_scope = _make_scope(scope_id="hash-001")
    hash_client = _make_client(
        tmp_path / "hash",
        _make_config(
            governance=GovernancePolicy(
                pii_sensitivity=PiiSensitivity.HIGH,
                redaction_strategy=RedactionStrategy.SHA256_HASH,
            )
        ),
    )
    await hash_client.add_episode(name="hash-episode", episode_body=body, source=EpisodeType.TEXT, scope=hash_scope)
    await hash_client.run_dream_job(job_name="formation-default")
    hash_facts = _scope_facts(hash_client, hash_scope)
    assert hash_facts
    assert all("bob@example.com" not in fact for fact in hash_facts), hash_facts
    assert any("[HASH:" in fact for fact in hash_facts), hash_facts


@pytest.mark.asyncio
async def test_redaction_allowlist_exempts_exact_string(tmp_path):
    """An allowlisted exact span survives; other spans still redact."""
    scope = _make_scope(scope_id="allowlist-001")
    gov = GovernancePolicy(
        pii_sensitivity=PiiSensitivity.HIGH,
        redaction_allowlist=("support@memotron.dev",),
    )
    client = _make_client(tmp_path, _make_config(governance=gov))
    await client.add_episode(
        name="allowlist-episode",
        episode_body=(
            "Memory: subject=agent-7; predicate=prefers; "
            "object=route support@memotron.dev not bob@example.com; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.TEXT,
        scope=scope,
    )
    await client.run_dream_job(job_name="formation-default")

    facts = _scope_facts(client, scope)
    assert facts
    assert any("support@memotron.dev" in fact for fact in facts), facts
    assert all("bob@example.com" not in fact for fact in facts), facts


def test_invalid_custom_pattern_fails_fast_at_construction():
    with pytest.raises(ValueError, match="not a valid regular expression"):
        GovernancePolicy(custom_pii_patterns={"broken": "("})
    with pytest.raises(ValueError, match="names cannot be blank"):
        GovernancePolicy(custom_pii_patterns={"   ": r"\d+"})
    with pytest.raises(ValueError, match="cannot be blank"):
        GovernancePolicy(custom_pii_patterns={"blank": "  "})


@pytest.mark.asyncio
async def test_per_motive_governance_carries_own_redaction_config(tmp_path):
    """A Motive's governance override brings its own redaction levers even when
    the config-level governance would not redact at all."""
    from memotron import DreamConfig
    from memotron.config import MemoryBank, Motive

    scope = _make_scope(scope_id="motive-redaction-001")
    motive = Motive(
        name="pii-strict",
        goal="Form memories under strict PII governance",
        governance=GovernancePolicy(
            pii_sensitivity=PiiSensitivity.HIGH,
            custom_pii_patterns={"ride_code": r"RIDE-\d{4}"},
        ),
    )
    base = _make_config(governance=GovernancePolicy(pii_sensitivity=PiiSensitivity.NONE))
    config = DreamConfig(
        **{
            **base.model_dump(exclude={"memory_bank"}),
            "memory_bank": MemoryBank(motives=(motive,)),
        }
    )
    client = _make_client(tmp_path, config)

    body = (
        "Memory: subject=agent-7; predicate=prefers; object=pre-checks for RIDE-4242; "
        "relationship_type=PREFERS; confidence=0.9"
    )
    await client.add_episode(
        name="motive-governed",
        episode_body=body,
        source=EpisodeType.TEXT,
        scope=scope,
        motive="pii-strict",
    )
    await client.run_dream_job(job_name="formation-default")
    facts = _scope_facts(client, scope)
    assert facts
    assert all("RIDE-4242" not in fact for fact in facts), facts

    # Control: the same body WITHOUT the motive is not redacted (config-level
    # governance is pii_sensitivity=none).
    control_scope = _make_scope(scope_id="motive-redaction-control")
    await client.add_episode(
        name="config-governed",
        episode_body=body,
        source=EpisodeType.TEXT,
        scope=control_scope,
    )
    await client.run_dream_job(job_name="formation-default")
    control_facts = _scope_facts(client, control_scope)
    assert control_facts
    assert any("RIDE-4242" in fact for fact in control_facts), control_facts
