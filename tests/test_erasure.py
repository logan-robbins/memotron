"""WS-12: Verifiable-erasure proof gates.

Reduction-to-practice tests for content-plane ciphertext-at-rest under an
envelope-wrapped per-scope DEK, blind-indexed dedup over ciphertext,
decrypt-on-read, crypto-shred, and the fail-closed, re-executable erasure
certificate that binds key destruction + ciphertext-only sweep + chain survival
and replay survival into one receipted artifact.

Every test builds its own isolated tmp_path client and runs fully offline.
"""

from __future__ import annotations

import pytest

from memotron import (
    SHREDDED_CONTENT_PLACEHOLDER,
    ContentKeyUnavailableError,
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    Memotron,
    EpisodeType,
    ErasureBehavior,
    ErasureVerificationError,
    GovernancePolicy,
    MemoryScope,
    NodeInstruction,
    PiiSensitivity,
    RelationshipInstruction,
    ScopeKind,
    is_content_commitment,
    is_sealed_content,
)
from memotron.config import EntityResolutionPolicy, RollupConsolidationPolicy, default_config
from memotron.models import DreamJobKind, RelationshipCardinality
from memotron.receipts import ReceiptDecisionType
from memotron.replay import byte_replay

SENSITIVE_SUBJECT = "Priya Sharma"
SENSITIVE_OBJECT = "quarterly summaries by Friday"
SENSITIVE_BODY = (
    f"Memory: subject={SENSITIVE_SUBJECT}; predicate=prefers; object={SENSITIVE_OBJECT}; "
    "relationship_type=PREFERS; confidence=0.93"
)


def _scope(scope_id: str = "erasure-test") -> MemoryScope:
    return MemoryScope(kind=ScopeKind.CUSTOMER, scope_id=scope_id)


def _config() -> DreamConfig:
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
        ),
    )
    return DreamConfig(
        instruction_sets=(instructions,),
        jobs=(DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),),
        governance=GovernancePolicy(
            pii_sensitivity=PiiSensitivity.NONE,
            erasure_behavior=ErasureBehavior.CRYPTO_SHRED,
        ),
    )


def _client(tmp_path) -> Memotron:
    return Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")


async def _ingest_and_form(client: Memotron, scope: MemoryScope, body: str = SENSITIVE_BODY, name: str = "e1"):
    await client.add_episode(
        name=name,
        episode_body=body,
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="erasure test",
    )
    return await client.run_dream_job(job_name="formation-default")


def _memory_rows(client: Memotron, scope: MemoryScope):
    return [
        rel
        for rel in client.graph.relationships()
        if rel.type != "MENTIONS" and rel.properties.get("scope_key") == scope.key
    ]


# ---------------------------------------------------------------------------
# Gate 1: the content plane is ciphertext-only at rest — no plaintext content
# anywhere in the persisted database file.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_content_plane_ciphertext_at_rest(tmp_path):
    scope = _scope()
    client = _client(tmp_path)
    await _ingest_and_form(client, scope)

    rows = _memory_rows(client, scope)
    assert rows, "formation must materialize the preference"
    for rel in rows:
        props = rel.properties
        assert is_sealed_content(props.get("fact")), "fact must be sealed at rest"
        assert is_sealed_content(props.get("object")), "object must be sealed at rest"
        assert is_sealed_content(props.get("embedding")), "embedding must be sealed at rest"
        assert is_sealed_content(props.get("object_embedding")), "object embedding must be sealed"
        assert is_content_commitment(props.get("truth_key")), "truth_key must be a keyed commitment"
        assert is_content_commitment(props.get("truth_prefix")), "truth_prefix must be committed"
        assert is_content_commitment(props.get("fact_commitment")), "fact_commitment required"

    # The scope's entity nodes store sealed names and committed identity keys.
    entity_nodes = [node for node in client.graph.nodes_for_scope(scope.key) if "Entity" in node.labels]
    assert entity_nodes
    for node in entity_nodes:
        assert is_sealed_content(node.properties.get("name")), "node name must be sealed"
        assert is_content_commitment(node.properties.get("graph_key")), "node key must be committed"

    # Raw evidence: the stored episode body/name are sealed.
    for episode in client.graph.episodes_for_scope(scope.key):
        assert is_sealed_content(episode.body), "episode body must be sealed at rest"
        assert is_sealed_content(episode.name), "episode name must be sealed at rest"

    # The strongest form of the claim: the persisted database FILE contains no
    # plaintext content bytes (the KEK lives in a separate 0600 sibling file).
    db_bytes = (tmp_path / "graph.sqlite").read_bytes()
    assert b"Priya" not in db_bytes, "entity name leaked into the database file"
    assert b"quarterly summaries" not in db_bytes, "fact/object text leaked into the database file"
    assert (tmp_path / "graph.sqlite.kek").exists(), "envelope KEK file must exist beside the store"


# ---------------------------------------------------------------------------
# Gate 2: decrypt-on-read while the DEK lives; placeholder after the shred.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_reads_reveal_then_placeholder_after_shred(tmp_path):
    scope = _scope()
    client = _client(tmp_path)
    await _ingest_and_form(client, scope)

    results = await client.search(query="quarterly summaries", scope=scope)
    assert results and SENSITIVE_OBJECT in results[0].fact
    assert results[0].subject == SENSITIVE_SUBJECT

    profile = await client.profile(scope=scope)
    assert any(SENSITIVE_OBJECT in fact.fact for fact in (*profile.static_facts, *profile.dynamic_facts))

    evidence = await client.memory_evidence(relationship_uuid=results[0].relationship_uuid, scope=scope)
    assert SENSITIVE_OBJECT in evidence.fact
    assert evidence.subject == SENSITIVE_SUBJECT
    assert any(SENSITIVE_SUBJECT in episode.body for episode in evidence.episodes)

    timeline = await client.truth_timeline(scope=scope, subject=SENSITIVE_SUBJECT, predicate="prefers")
    assert timeline and SENSITIVE_OBJECT in timeline[0].fact

    await client.crypto_shred(scope=scope)

    # Every read surface resolves to the placeholder — never the erased plaintext.
    post_evidence = await client.memory_evidence(relationship_uuid=results[0].relationship_uuid, scope=scope)
    assert post_evidence.fact == SHREDDED_CONTENT_PLACEHOLDER
    assert all(episode.body == SHREDDED_CONTENT_PLACEHOLDER for episode in post_evidence.episodes)
    assert not await client.search(query="quarterly summaries", scope=scope)
    post_profile = await client.profile(scope=scope)
    for fact in (*post_profile.static_facts, *post_profile.dynamic_facts):
        assert SENSITIVE_OBJECT not in fact.fact


# ---------------------------------------------------------------------------
# Gate 3: envelope key management — the DEK is never persisted raw, and a
# shredded scope refuses new crypto-governed writes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_envelope_key_management_and_shredded_scope_writes_fail(tmp_path):
    scope = _scope()
    client = _client(tmp_path)
    dek = client.provision_governance_key(scope=scope)

    row = client.graph._connection.execute(
        "SELECT wrapped_dek, kek_id FROM governance_keys WHERE scope_key = ?",
        (scope.key,),
    ).fetchone()
    assert row is not None
    assert dek.hex() not in str(row["wrapped_dek"]), "the raw DEK must never be persisted"
    assert str(row["kek_id"]).startswith("local:")
    assert client.graph.get_governance_key(scope.key) == dek

    await _ingest_and_form(client, scope)
    await client.crypto_shred(scope=scope)

    state = client.graph.governance_key_state(scope.key)
    assert state is not None and state["shredded"] and state["shredded_at"]

    with pytest.raises(ContentKeyUnavailableError):
        client.graph.get_or_create_governance_key(scope.key)
    with pytest.raises(ContentKeyUnavailableError):
        await client.add_memory(
            subject="Priya Sharma",
            predicate="prefers",
            object="anything new",
            relationship_type="PREFERS",
            scope=scope,
        )


# ---------------------------------------------------------------------------
# Gate 4: truth management works OVER CIPHERTEXT — commitment-equality
# reinforcement, sealed-embedding semantic dedup, and supersession — and the
# runs (including their reinforce receipts) byte-replay against the live store.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_dedup_and_supersession_over_ciphertext_and_replay(tmp_path):
    scope = _scope()
    client = _client(tmp_path)

    # Exact reinforce: same fact twice → one row, observed_count 2 (blind-index equality).
    await _ingest_and_form(client, scope, name="e1")
    await _ingest_and_form(client, scope, name="e2")
    rows = [
        rel for rel in _memory_rows(client, scope) if rel.properties.get("status") == "active" and rel.type == "PREFERS"
    ]
    assert len(rows) == 1, "the repeated fact must reinforce, not duplicate, over ciphertext"
    assert int(rows[0].properties.get("observed_count", 1)) == 2

    # Supersession over ciphertext: contradicting single-active REQUIRES fact.
    await _ingest_and_form(
        client,
        scope,
        body=(
            f"Memory: subject={SENSITIVE_SUBJECT}; predicate=requires; object=SOC2 report; "
            "relationship_type=REQUIRES; confidence=0.9"
        ),
        name="e3",
    )
    await _ingest_and_form(
        client,
        scope,
        body=(
            f"Memory: subject={SENSITIVE_SUBJECT}; predicate=requires; object=ISO27001 audit; "
            "relationship_type=REQUIRES; confidence=0.95"
        ),
        name="e4",
    )
    requires = [rel for rel in _memory_rows(client, scope) if rel.type == "REQUIRES"]
    statuses = sorted(str(rel.properties.get("status")) for rel in requires)
    assert statuses == ["active", "superseded"], f"expected supersession over ciphertext, got {statuses}"

    # Every formation run — including the reinforce-only run e2 — byte-replays
    # with the LIVE store anchor (reinforce receipts carry state-hash brackets).
    ledger = client.graph.receipts
    run_uuids = [
        row["run_uuid"]
        for row in client.graph._connection.execute(
            "SELECT DISTINCT run_uuid FROM memory_receipts WHERE scope_key = ? ORDER BY rowid",
            (scope.key,),
        ).fetchall()
    ]
    reinforced_runs = 0
    for run_uuid in run_uuids:
        receipts = ledger.receipts_for_run(run_uuid)
        if any(r.decision_result == "reinforced" for r in receipts):
            reinforced_runs += 1
        proof = await byte_replay(ledger, run_uuid=run_uuid, graph=None)
        assert proof.receipt_count == len(receipts)
    assert reinforced_runs >= 1, "the reinforce run must exist and byte-replay"

    # The latest run additionally anchors to the live store.
    await byte_replay(ledger, run_uuid=run_uuids[-1], graph=client.graph)


# ---------------------------------------------------------------------------
# Gate 5: crypto-shred retires pending evidence and the chain + replay survive
# the shred with the live-store anchor.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_shred_retires_pending_episodes_and_replay_survives(tmp_path):
    scope = _scope()
    client = _client(tmp_path)
    await _ingest_and_form(client, scope)

    # Queue an episode but do NOT run formation — it must be retired at shred.
    await client.add_episode(
        name="pending-evidence",
        episode_body=SENSITIVE_BODY,
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="erasure test",
    )
    shred = await client.crypto_shred(scope=scope)
    assert shred["episodes_retired"] == 1
    assert all(
        client.graph.is_episode_processed(episode.uuid) for episode in client.graph.episodes_for_scope(scope.key)
    )

    # Chain verifies and the formation run still byte-replays — anchored to the
    # LIVE post-shred store (the state hash binds the write-time commitment,
    # which key destruction does not touch).
    ledger = client.graph.receipts
    run_uuids = [
        row["run_uuid"]
        for row in client.graph._connection.execute(
            "SELECT DISTINCT run_uuid FROM memory_receipts WHERE scope_key = ? ORDER BY rowid",
            (scope.key,),
        ).fetchall()
    ]
    for run_uuid in run_uuids:
        assert ledger.verify_chain(run_uuid).valid
        await byte_replay(ledger, run_uuid=run_uuid, graph=client.graph)


# ---------------------------------------------------------------------------
# Gate 6: the erasure certificate — fail-closed issuance, chain binding, and
# re-executable verification.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_erasure_certificate_lifecycle(tmp_path):
    scope = _scope()
    client = _client(tmp_path)
    await _ingest_and_form(client, scope)
    await _ingest_and_form(client, scope, name="e2")  # reinforce run too

    # Fail closed BEFORE the shred: no certificate without key destruction.
    with pytest.raises(ErasureVerificationError, match="has not been destroyed"):
        await client.erasure_certificate(scope=scope)

    await client.crypto_shred(scope=scope)
    certificate = await client.erasure_certificate(scope=scope)

    assert certificate.scope_key == scope.key
    assert certificate.key_state["shredded"] is True
    assert certificate.shred_receipt_hash
    assert certificate.sweep.violations == ()
    assert certificate.sweep.relationships_scanned >= 1
    assert certificate.sweep.episodes_scanned == 2
    assert certificate.sweep.sealed_field_count > 0
    assert certificate.sweep.committed_field_count > 0
    assert len(certificate.chain_verified_runs) >= 3  # 2 formations + shred operator run
    assert len(certificate.replay_verified_runs) >= 3  # all of them mutate (incl. zero-delta shred)
    assert certificate.certificate_digest

    # The certificate is bound into the SAME hash chain as a receipted event.
    issuance = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.ERASURE_CERTIFICATE_ISSUED
    ]
    assert len(issuance) == 1
    assert certificate.certificate_digest in issuance[0].decision_reason
    assert client.graph.receipts.verify_chain(issuance[0].run_uuid).valid

    # Re-executable: every leg re-verifies against the live store, any time.
    assert await client.verify_erasure(scope=scope, certificate=certificate) is True


@pytest.mark.asyncio
async def test_crypto_shred_purges_derived_rollups_and_binds_them_into_certificate(tmp_path):
    scope = _scope("rollup-erasure")
    base = _config()
    config = base.model_copy(
        update={
            "jobs": (
                *base.jobs,
                DreamJob(
                    name="consolidation-default",
                    kind=DreamJobKind.CONSOLIDATION,
                    cadence_seconds=1,
                    rollup_consolidation_policy=RollupConsolidationPolicy(
                        cluster_threshold=0.0,
                        min_cluster_size=2,
                        max_depth=1,
                    ),
                ),
            )
        }
    )
    client = Memotron(config=config, graph_path=tmp_path / "rollup-graph.sqlite")
    await client.add_memory(
        subject="Priya Sharma",
        predicate="prefers",
        object="monthly reports",
        relationship_type="PREFERS",
        scope=scope,
    )
    await client.add_memory(
        subject="Priya Sharma",
        predicate="prefers",
        object="quarterly reports",
        relationship_type="PREFERS",
        scope=scope,
    )
    await client.run_dream_job(job_name="consolidation-default")
    rollups = [
        relationship
        for relationship in _memory_rows(client, scope)
        if relationship.properties.get("memory_type") == "rollup"
    ]
    assert len(rollups) == 1

    shred = await client.crypto_shred(scope=scope)
    purges = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CONSOLIDATION_ROLLUP_ERASURE_PURGED
    ]
    assert shred["derived_artifacts_purged"] == 1
    assert len(purges) == 1
    assert client.graph.get_relationship(rollups[0].uuid).properties["status"] == "pruned"

    certificate = await client.erasure_certificate(scope=scope)
    assert certificate.derived_artifact_purge_receipt_hashes == (purges[0].receipt_hash,)
    assert await client.verify_erasure(scope=scope, certificate=certificate) is True


@pytest.mark.asyncio
async def test_gate_erasure_certificate_fails_closed_on_plaintext_remnant(tmp_path):
    scope = _scope()
    client = _client(tmp_path)
    await _ingest_and_form(client, scope)
    await client.crypto_shred(scope=scope)
    certificate = await client.erasure_certificate(scope=scope)

    # Plant a plaintext remnant in the scope (simulating a defective write path).
    leaked = _memory_rows(client, scope)[0]
    client.graph.update_relationship(leaked.uuid, properties={"fact": "PLAINTEXT LEAK"})

    # Issuance fails closed and NAMES the remnant (row + field, never content).
    with pytest.raises(ErasureVerificationError) as excinfo:
        await client.erasure_certificate(scope=scope)
    assert any(f"relationships:{leaked.uuid}:fact" in failure for failure in excinfo.value.failures)

    # And the previously issued certificate no longer re-verifies — the proof is
    # a durable, re-checkable artifact, not a point-in-time attestation.
    with pytest.raises(ErasureVerificationError):
        await client.verify_erasure(scope=scope, certificate=certificate)


# ---------------------------------------------------------------------------
# Gate 7: counterfactual isolation is unchanged for a crypto scope — the
# production store and ledger are byte-for-byte untouched by evaluation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_counterfactual_isolation_for_crypto_scope(tmp_path):
    from memotron.replay import CounterfactualPolicy, counterfactual_evaluation

    scope = _scope()
    client = _client(tmp_path)
    await _ingest_and_form(client, scope)
    ledger = client.graph.receipts
    run_uuid = client.graph._connection.execute(
        "SELECT DISTINCT run_uuid FROM memory_receipts WHERE scope_key = ? AND run_kind = 'formation'",
        (scope.key,),
    ).fetchone()["run_uuid"]

    state_before = client.graph.graph_state_hash(scope.key)
    count_before = client.graph._connection.execute("SELECT COUNT(*) AS n FROM memory_receipts").fetchone()["n"]
    report = await counterfactual_evaluation(
        ledger,
        CounterfactualPolicy(allowed_memory_types=("requirement",), salience_threshold=0.0),
        run_uuid=run_uuid,
        alternate_motive="strict-requirements-only",
    )
    assert report.original_only, "the preference must be rejected under the strict alternate policy"
    assert client.graph.graph_state_hash(scope.key) == state_before
    assert client.graph._connection.execute("SELECT COUNT(*) AS n FROM memory_receipts").fetchone()["n"] == count_before


# ---------------------------------------------------------------------------
# Gate 8: what sealing COSTS, measured — aliases stop merging under CRYPTO_SHRED.
#
# This gate exists because a plausible-sounding defect turned out to be unreachable, and the
# reachability question is the interesting part.
#
# The claim was: `subject_surface` / `object_surface` carry the caller's ORIGINAL entity text,
# are written raw, and are absent from `RELATIONSHIP_SEALED_FIELDS` — so a shred would leave
# cleartext entity names behind while the certificate still reported `violations=()`.
#
# Every part of that is true about the CODE and false about the SYSTEM: entity resolution is
# inert while content is sealed (`dreaming/_entities.py` skips sealed names), so resolution
# never rewrites a canonical name, so the surface forms are never written in the first place.
# They are reachable only under SOFT_RETIRE, where there is nothing to seal.
#
# What the same measurement DID expose is a real cost, and it is the stronger argument
# against enabling sealing: under CRYPTO_SHRED an alias and its canonical form become two
# separate entities on two separate truth slots, so dedup and supersession silently stop
# working across them. Nothing errors. The store just quietly holds both.
# ---------------------------------------------------------------------------


def _alias_config(*, sealed: bool):
    """`default_config()` with a synonym, optionally under CRYPTO_SHRED."""
    # Two synonyms, one per SIDE of the triple. `_materialization` writes
    # `subject_surface` and `object_surface` from two independent branches, so a
    # subject-only alias leaves the object branch unexercised — which is exactly
    # how it read on 2026-09-08: the module sat at 50% diff coverage with the
    # object half dark. They can regress separately; they are covered separately.
    update: dict = {
        "entity_resolution": EntityResolutionPolicy(
            synonyms={"the gateway": "jedai gateway", "client creds": "oauth2 client credentials"}
        )
    }
    if sealed:
        update["governance"] = GovernancePolicy(
            pii_sensitivity=PiiSensitivity.NONE,
            erasure_behavior=ErasureBehavior.CRYPTO_SHRED,
        )
    return default_config().model_copy(update=update)


async def _two_facts_via_alias(client: Memotron, scope: MemoryScope) -> None:
    """The canonical first, then the same slot via its alias. REQUIRES is SINGLE_ACTIVE."""
    await client.add_memory(
        subject="Jedai Gateway",
        predicate="requires",
        object="OAuth2 client credentials",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
    )
    await client.add_memory(
        subject="the gateway",
        predicate="requires",
        object="SAML assertions",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
    )


@pytest.mark.asyncio
async def test_alias_resolution_merges_when_content_is_NOT_sealed(tmp_path) -> None:
    """The positive control. Without it the sealed assertion below proves nothing —
    an alias that never merged under either policy would satisfy it trivially."""
    scope = _scope("alias-clear")
    client = Memotron(config=_alias_config(sealed=False), graph_path=tmp_path / "clear.sqlite")
    await _two_facts_via_alias(client, scope)

    rows = _memory_rows(client, scope)
    statuses = sorted(str(row.properties.get("status")) for row in rows)
    assert statuses == ["active", "superseded"], f"the alias must merge onto one slot, got {statuses}"

    surfaced = [row for row in rows if "subject_surface" in row.properties]
    assert surfaced, "resolution rewrote the subject, so the surface form must be recorded"
    assert surfaced[0].properties["subject_surface"] == "the gateway"


@pytest.mark.asyncio
async def test_alias_resolution_records_the_OBJECT_surface_form_too(tmp_path) -> None:
    """The object side of the same mechanism, which the subject-only control missed.

    Not a duplicate: `subject_surface` and `object_surface` are written by two separate
    branches, so this is the only thing standing between the object branch and a silent
    regression. It also pins the direction — the SURFACE form is the caller's text and
    the stored triple carries the CANONICAL one, not the reverse.
    """
    scope = _scope("alias-object")
    client = Memotron(config=_alias_config(sealed=False), graph_path=tmp_path / "object.sqlite")
    await client.add_memory(
        subject="Jedai Gateway",
        predicate="requires",
        object="client creds",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
    )

    rows = _memory_rows(client, scope)
    assert rows, "formation must materialize the preference"
    surfaced = [row for row in rows if "object_surface" in row.properties]
    assert surfaced, (
        "resolution rewrote the object, so its surface form must be recorded — if this is "
        "empty, object-side resolution stopped firing and the branch is dead code"
    )
    assert surfaced[0].properties["object_surface"] == "client creds"


@pytest.mark.asyncio
async def test_alias_resolution_is_INERT_under_seal_so_aliases_do_not_merge(tmp_path) -> None:
    """The cost of sealing, stated as a test so it cannot be rediscovered by surprise."""
    scope = _scope("alias-sealed")
    client = Memotron(config=_alias_config(sealed=True), graph_path=tmp_path / "sealed.sqlite")
    await _two_facts_via_alias(client, scope)

    rows = _memory_rows(client, scope)
    statuses = sorted(str(row.properties.get("status")) for row in rows)
    assert statuses == ["active", "active"], (
        "under CRYPTO_SHRED the alias and the canonical form are different entities, so both "
        f"rows stay active on a SINGLE_ACTIVE slot. Got {statuses} — if this now supersedes, "
        "entity resolution has been made seal-aware and Gate 8's premise needs revisiting."
    )
    assert not [row for row in rows if "subject_surface" in row.properties], (
        "no surface form should be written under seal, because resolution never rewrote a name"
    )

    # The remnant this gate was written to chase: unreachable, but pinned both ways.
    from memotron.erasure import RELATIONSHIP_SEALED_FIELDS

    assert "subject_surface" in RELATIONSHIP_SEALED_FIELDS, (
        "kept in the swept set deliberately: if resolution is ever made seal-aware, the field "
        "starts being written and must already be enumerated here — the sweep can only fail "
        "on what it lists"
    )
    raw = (tmp_path / "sealed.sqlite").read_bytes()
    assert b"the gateway" not in raw, "the caller's alias text is in cleartext at rest"
