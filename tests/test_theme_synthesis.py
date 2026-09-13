"""WS-18 T19: real LLM rollup synthesis behind the deterministic entailment gate.

Covers the SynthesisTransport seam end to end: a faithful summary becomes the
rollup text (receipted with model identifier + prompt digest), a hallucinated /
malformed / over-long summary is receipted as rejected and falls back to the
deterministic structural label, crypto-shred scopes never reach the LLM, and no
transport at all keeps the deterministic label.  Plus exhaustive unit tests of
the pure entailment gate, of the structural fallback an operator actually reads
on a rejection, and of that label's determinism ACROSS interpreter processes.

WS-24 moved the on-switch: a configured ``SynthesisTransport`` is what enables
LLM synthesis, and ``ConsolidationSynthesisProfile.model_identifier`` is only
an override of the recorded provenance string (unset → the transport's own
``identifier``).  Requiring both made the feature unreachable on every
packaged config.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from memotron import Memotron, MemoryScope, ScopeKind
from memotron.config import (
    ConsolidationSynthesisProfile,
    DreamConfig,
    DreamJob,
    DreamJobKind,
    ErasureBehavior,
    GovernancePolicy,
    RollupConsolidationPolicy,
    default_config,
)
from memotron.dreaming import (
    ROLLUP_SYNTHESIS_SYSTEM_PROMPT,
    ROLLUP_TEXT_SOURCE_CRYPTO_SHRED_SCOPE,
    ROLLUP_TEXT_SOURCE_ENTAILMENT_REJECTED,
    ROLLUP_TEXT_SOURCE_LLM,
    ROLLUP_TEXT_SOURCE_NO_TRANSPORT,
    ROLLUP_TEXT_SOURCE_TRANSPORT_ERROR,
    rollup_summary_entailed,
    rollup_synthesis_prompt_digest,
)
from memotron.models import GraphRelationship, MemoryType
from memotron.receipts import ReceiptDecisionType

CONSOLIDATION_JOB = "consolidation-rollup"


class StubSynthesisTransport:
    """Deterministic in-test SynthesisTransport: records calls, replays responses."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    @property
    def identifier(self) -> str:
        return "stub-synthesis:test@v1"

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        self.calls.append((prompt, system_prompt))
        if not self.responses:
            raise AssertionError("stub transport exhausted")
        return self.responses.pop(0)


def synthesis_config(
    *,
    model_identifier: str | None = "stub-rollup-model",
    governance: GovernancePolicy | None = None,
) -> DreamConfig:
    return default_config().model_copy(
        update={
            "governance": governance,
            "consolidation_profiles": (ConsolidationSynthesisProfile(model_identifier=model_identifier),),
            "jobs": (
                DreamJob(
                    name=CONSOLIDATION_JOB,
                    kind=DreamJobKind.CONSOLIDATION,
                    cadence_seconds=1,
                    rollup_consolidation=True,
                    rollup_consolidation_policy=RollupConsolidationPolicy(
                        cluster_threshold=0.0,
                        min_cluster_size=3,
                        max_depth=1,
                    ),
                ),
            ),
        }
    )


async def seed_preferences(client: Memotron, scope: MemoryScope) -> None:
    for subject in ("Priya", "Marco", "Elena"):
        await client.add_memory(
            subject=subject,
            predicate="prefers",
            object="window seating",
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9,
        )


def rollup_rows(client: Memotron, scope: MemoryScope):
    return [
        relationship
        for relationship in client.graph.relationships()
        if relationship.type == "ROLLUP"
        and relationship.properties.get("scope_key") == scope.key
        and relationship.properties.get("status") == "active"
    ]


def receipts_of(client: Memotron, scope: MemoryScope, decision_type: ReceiptDecisionType):
    return [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == decision_type
    ]


# WS-23 M3: each SENTENCE must be covered by a SINGLE member fact, so a
# summary that fuses two members into one sentence ("Marco and Elena prefers
# window seating") is now a rejected recombination — the same rule that stops
# an inverted claim being assembled from two members' tokens.  A genuinely
# multi-member summary states one member per sentence, within the 2-sentence
# budget.
FAITHFUL_SUMMARY = "Priya prefers window seating. Marco prefers window seating."


@pytest.mark.asyncio
async def test_faithful_summary_becomes_rollup_text_with_identifier_and_prompt_digest(
    tmp_path: Path,
) -> None:
    transport = StubSynthesisTransport([json.dumps({"summary": FAITHFUL_SUMMARY})])
    client = Memotron(
        graph_path=tmp_path / "faithful.sqlite",
        config=synthesis_config(),
        rollup_synthesis_transport=transport,
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="faithful")
    await seed_preferences(client, scope)
    run = await client.run_dream_job(job_name=CONSOLIDATION_JOB)

    # The run stays chain-verified and byte-replayable with the LLM-synthesized
    # rollup receipts in it.
    proof = await client.byte_replay(run_uuid=run.job_runs[0].run_uuid)
    assert proof.receipt_count > 0  # byte_replay raises on any divergence

    rollups = rollup_rows(client, scope)
    assert len(rollups) == 1
    rollup = rollups[0]
    assert rollup.properties.get("object") == FAITHFUL_SUMMARY
    assert rollup.properties.get("rollup_model_identifier") == "stub-rollup-model"
    # The row says a model wrote these words, and it is telling the truth.
    assert rollup.properties.get("rollup_text_source") == ROLLUP_TEXT_SOURCE_LLM

    # The stored prompt digest pins the EXACT rendered prompt the stub saw.
    assert len(transport.calls) == 1
    prompt, system_prompt = transport.calls[0]
    assert system_prompt == ROLLUP_SYNTHESIS_SYSTEM_PROMPT
    assert "Member facts:" in prompt
    assert "Priya prefers window seating" in prompt
    expected_digest = rollup_synthesis_prompt_digest(system_prompt=system_prompt, prompt=prompt)
    assert rollup.properties.get("rollup_synthesis_prompt_digest") == expected_digest

    # The synthesis receipt carries the model identifier and the prompt digest.
    summarized = receipts_of(client, scope, ReceiptDecisionType.CONSOLIDATION_ROLLUP_CREATED)
    assert len(summarized) == 1
    assert summarized[0].model_identifier == "stub-rollup-model"
    payload = json.loads(summarized[0].event_payload)
    assert payload["rollup_model_identifier"] == "stub-rollup-model"
    assert payload["rollup_synthesis_prompt_digest"] == expected_digest
    assert payload["rollup_synthesis_transport_identifier"] == transport.identifier
    assert not receipts_of(client, scope, ReceiptDecisionType.CONSOLIDATION_ROLLUP_SYNTHESIS_REJECTED)


@pytest.mark.parametrize(
    ("response", "reason_prefix"),
    [
        (
            json.dumps({"summary": "Priya prefers window seating near the balcony."}),
            "entailment_failed:unentailed_token:balcony",
        ),
        ("this is not json at all", "summary_json_parse_failed"),
        (json.dumps({"synopsis": "wrong shape"}), "summary_json_shape_invalid"),
        (
            json.dumps({"summary": "Priya prefers window seating " * 12}),
            "entailment_failed:length_exceeded",
        ),
    ],
)
@pytest.mark.asyncio
async def test_rejected_summaries_are_receipted_and_fall_back_to_deterministic_label(
    tmp_path: Path, response: str, reason_prefix: str
) -> None:
    transport = StubSynthesisTransport([response])
    client = Memotron(
        graph_path=tmp_path / "rejected.sqlite",
        config=synthesis_config(),
        rollup_synthesis_transport=transport,
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="rejected")
    await seed_preferences(client, scope)
    run = await client.run_dream_job(job_name=CONSOLIDATION_JOB)

    # A gated (non-mutating) rejection receipt never breaks byte replay.
    proof = await client.byte_replay(run_uuid=run.job_runs[0].run_uuid)
    assert proof.receipt_count > 0  # byte_replay raises on any divergence

    # A deterministic-label control run on a separate store: identical members,
    # no model — the reject path must produce the same rollup text.
    control = Memotron(
        graph_path=tmp_path / "control.sqlite",
        config=synthesis_config(model_identifier=None),
    )
    control_scope = MemoryScope(kind=ScopeKind.USER, scope_id="rejected")
    await seed_preferences(control, control_scope)
    await control.run_dream_job(job_name=CONSOLIDATION_JOB)

    rollups = rollup_rows(client, scope)
    control_rollups = rollup_rows(control, control_scope)
    assert len(rollups) == 1 and len(control_rollups) == 1
    assert rollups[0].properties.get("object") == control_rollups[0].properties.get("object")
    assert "rollup_synthesis_prompt_digest" not in rollups[0].properties
    # The structural fallback for this cluster: three subjects share a
    # predicate and an object, so the label states the shared claim and counts
    # what varies.  It never asserts anything the members do not.
    assert rollups[0].properties.get("object") == "prefers window seating: 3 subjects"
    # ...and the row says so, even though rollup_model_identifier is still set
    # (synthesis WAS attempted under that model — it was just refused).
    assert rollups[0].properties.get("rollup_model_identifier") == "stub-rollup-model"
    assert rollups[0].properties.get("rollup_text_source") == ROLLUP_TEXT_SOURCE_ENTAILMENT_REJECTED
    assert control_rollups[0].properties.get("rollup_text_source") == ROLLUP_TEXT_SOURCE_NO_TRANSPORT

    rejected = receipts_of(client, scope, ReceiptDecisionType.CONSOLIDATION_ROLLUP_SYNTHESIS_REJECTED)
    assert len(rejected) == 1
    receipt = rejected[0]
    assert receipt.decision_reason.startswith(reason_prefix)
    assert receipt.decision_result == "gated"
    assert receipt.model_identifier == "stub-rollup-model"
    assert receipt.memory_type == MemoryType.ROLLUP.value
    payload = json.loads(receipt.event_payload)
    assert payload["rollup_synthesis_transport_identifier"] == transport.identifier
    # The hallucinated/invalid text itself is never stored in plaintext fields.
    assert receipt.sensitive_payload is None
    assert "balcony" not in (receipt.event_payload or "")


@pytest.mark.asyncio
async def test_transport_error_is_receipted_rejection_not_a_wedged_run(
    tmp_path: Path,
) -> None:
    class FailingTransport(StubSynthesisTransport):
        async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
            self.calls.append((prompt, system_prompt))
            raise ValueError("HTTP 529 upstream overloaded")

    transport = FailingTransport([])
    client = Memotron(
        graph_path=tmp_path / "transport-error.sqlite",
        config=synthesis_config(),
        rollup_synthesis_transport=transport,
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="transport-error")
    await seed_preferences(client, scope)
    await client.run_dream_job(job_name=CONSOLIDATION_JOB)

    rollups = rollup_rows(client, scope)
    assert len(rollups) == 1  # deterministic label still lands
    # The model never answered, so nothing of its was refused — the row does
    # not claim an entailment rejection it did not make.
    assert rollups[0].properties.get("rollup_text_source") == ROLLUP_TEXT_SOURCE_TRANSPORT_ERROR
    rejected = receipts_of(client, scope, ReceiptDecisionType.CONSOLIDATION_ROLLUP_SYNTHESIS_REJECTED)
    assert len(rejected) == 1
    assert rejected[0].decision_reason.startswith("synthesis_transport_error:")


# Both ways of naming the model: the WS-24 default (inherit the transport's own
# identifier) and an explicit profile override.  The crypto-shred skip and the
# entailment gate are properties of the synthesis path itself, so neither may
# depend on which one is in force.
@pytest.mark.parametrize("model_identifier", [None, "stub-rollup-model"])
@pytest.mark.asyncio
async def test_crypto_shred_scope_content_never_reaches_the_llm(tmp_path: Path, model_identifier: str | None) -> None:
    transport = StubSynthesisTransport([json.dumps({"summary": FAITHFUL_SUMMARY})])
    client = Memotron(
        graph_path=tmp_path / "sealed.sqlite",
        config=synthesis_config(
            model_identifier=model_identifier,
            governance=GovernancePolicy(erasure_behavior=ErasureBehavior.CRYPTO_SHRED),
        ),
        rollup_synthesis_transport=transport,
    )
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="sealed")
    await seed_preferences(client, scope)
    await client.run_dream_job(job_name=CONSOLIDATION_JOB)

    assert transport.calls == []  # sealed content never leaves the process
    rollups = rollup_rows(client, scope)
    assert len(rollups) == 1  # deterministic label, sealed
    assert rollups[0].properties.get("rollup_text_source") == ROLLUP_TEXT_SOURCE_CRYPTO_SHRED_SCOPE
    rejected = receipts_of(client, scope, ReceiptDecisionType.CONSOLIDATION_ROLLUP_SYNTHESIS_REJECTED)
    assert len(rejected) == 1
    assert rejected[0].decision_reason == "crypto_shred_scope_content_never_sent_to_llm"


@pytest.mark.parametrize("model_identifier", [None, "stub-rollup-model"])
@pytest.mark.asyncio
async def test_entailment_gate_rejects_a_hallucination_however_the_model_is_named(
    tmp_path: Path, model_identifier: str | None
) -> None:
    transport = StubSynthesisTransport([json.dumps({"summary": "Priya prefers window seating near the balcony."})])
    client = Memotron(
        graph_path=tmp_path / "hallucinated.sqlite",
        config=synthesis_config(model_identifier=model_identifier),
        rollup_synthesis_transport=transport,
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="hallucinated")
    await seed_preferences(client, scope)
    await client.run_dream_job(job_name=CONSOLIDATION_JOB)

    assert len(transport.calls) == 1  # the model WAS asked
    rollups = rollup_rows(client, scope)
    assert len(rollups) == 1
    assert "balcony" not in str(rollups[0].properties.get("object"))
    assert "rollup_synthesis_prompt_digest" not in rollups[0].properties
    assert rollups[0].properties.get("rollup_text_source") == ROLLUP_TEXT_SOURCE_ENTAILMENT_REJECTED

    rejected = receipts_of(client, scope, ReceiptDecisionType.CONSOLIDATION_ROLLUP_SYNTHESIS_REJECTED)
    assert len(rejected) == 1
    assert rejected[0].decision_reason == "entailment_failed:unentailed_token:balcony"
    assert rejected[0].model_identifier == (model_identifier or transport.identifier)


@pytest.mark.asyncio
async def test_configured_transport_alone_runs_llm_synthesis_and_names_itself(
    tmp_path: Path,
) -> None:
    """WS-24: a configured transport IS the opt-in; ``model_identifier`` is an override.

    Replaces ``test_none_model_identifier_default_never_calls_the_transport``,
    which pinned the defect: WS-18 required a SECOND opt-in that no packaged
    config, Motive, or preset ever set, so a tenant with a live gateway seat
    kept the deterministic label and never even attempted a call.  The unset
    default now inherits the transport's own ``identifier`` — the honest answer
    to "which model wrote this rollup?".
    """
    transport = StubSynthesisTransport([json.dumps({"summary": FAITHFUL_SUMMARY})])
    client = Memotron(
        graph_path=tmp_path / "default.sqlite",
        config=synthesis_config(model_identifier=None),
        rollup_synthesis_transport=transport,
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="default")
    await seed_preferences(client, scope)
    await client.run_dream_job(job_name=CONSOLIDATION_JOB)

    assert len(transport.calls) == 1
    rollups = rollup_rows(client, scope)
    assert len(rollups) == 1
    rollup = rollups[0]
    assert rollup.properties.get("object") == FAITHFUL_SUMMARY
    assert rollup.properties.get("rollup_model_identifier") == transport.identifier
    assert rollup.properties.get("rollup_text_source") == ROLLUP_TEXT_SOURCE_LLM
    prompt, system_prompt = transport.calls[0]
    assert rollup.properties.get("rollup_synthesis_prompt_digest") == (
        rollup_synthesis_prompt_digest(system_prompt=system_prompt, prompt=prompt)
    )
    summarized = receipts_of(client, scope, ReceiptDecisionType.CONSOLIDATION_ROLLUP_CREATED)
    assert [receipt.model_identifier for receipt in summarized] == [transport.identifier]
    assert not receipts_of(client, scope, ReceiptDecisionType.CONSOLIDATION_ROLLUP_SYNTHESIS_REJECTED)


@pytest.mark.asyncio
async def test_no_transport_keeps_the_deterministic_label_whatever_the_profile_says(
    tmp_path: Path,
) -> None:
    """No transport is the ONLY deterministic-label condition, and it is byte-identical.

    Proven against a control store whose profile leaves ``model_identifier``
    unset: both produce the same rollup text and neither stamps a prompt digest
    or a model identifier.
    """
    client = Memotron(
        graph_path=tmp_path / "no-transport.sqlite",
        config=synthesis_config(model_identifier="configured-but-no-transport"),
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="no-transport")
    await seed_preferences(client, scope)
    await client.run_dream_job(job_name=CONSOLIDATION_JOB)

    control = Memotron(
        graph_path=tmp_path / "no-transport-control.sqlite",
        config=synthesis_config(model_identifier=None),
    )
    control_scope = MemoryScope(kind=ScopeKind.USER, scope_id="no-transport")
    await seed_preferences(control, control_scope)
    await control.run_dream_job(job_name=CONSOLIDATION_JOB)

    rollups = rollup_rows(client, scope)
    control_rollups = rollup_rows(control, control_scope)
    assert len(rollups) == 1 and len(control_rollups) == 1
    assert rollups[0].properties.get("object") == control_rollups[0].properties.get("object")
    for row in (rollups[0], control_rollups[0]):
        assert "rollup_synthesis_prompt_digest" not in row.properties
        assert row.properties.get("rollup_model_identifier") is None
        assert row.properties.get("rollup_text_source") == ROLLUP_TEXT_SOURCE_NO_TRANSPORT


# ---------------------------------------------------------------------------
# The deterministic fallback label an operator actually reads
# ---------------------------------------------------------------------------


async def fallback_label(client: Memotron, scope: MemoryScope, rows: list[tuple[str, str, str]]) -> str:
    """Seed ``rows`` as real memories and label them as one cluster.

    The labeler is exercised directly (not through consolidation) so clusters
    the consistency gate would never form — members sharing no predicate — can
    still be pinned; that shape is the labeler's defensive floor.
    """
    uuids = []
    for subject, predicate, object_value in rows:
        result = await client.add_memory(
            subject=subject,
            predicate=predicate,
            object=object_value,
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9,
        )
        uuids.append(result.relationship_uuid)
    members = [client.graph.get_relationship(uuid) for uuid in uuids]
    return client._engine._synthesize_rollup_label(members, scope_key=scope.key)


@pytest.mark.asyncio
async def test_shared_subject_cluster_labels_the_shared_stem_and_counts_what_varies(
    tmp_path: Path,
) -> None:
    """The reported defect, end to end, on the real consolidation path.

    Ten ``Special Offers has field <path>`` rows used to render as
    ``"summarizes special field offers has"`` — the top-6 shared tokens, jumbled
    into something that reads like a broken sentence.  The label now states the
    stem every member shares and counts what differs.
    """
    client = Memotron(graph_path=tmp_path / "portal.sqlite", config=synthesis_config(model_identifier=None))
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="portal-kb")
    fields = [
        "data.web_links.wdw_offer_detail",
        "data.descriptions.offer_intro",
        "data.descriptions.offer_terms",
        "data.media.hero_image",
        "data.media.thumbnail",
        "data.dates.book_by",
        "data.dates.travel_start",
        "data.dates.travel_end",
        "data.pricing.rate_code",
        "data.pricing.discount_percent",
    ]
    for field in fields:
        await client.add_memory(
            subject="Special Offers",
            predicate="has field",
            object=field,
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9,
        )
    await client.run_dream_job(job_name=CONSOLIDATION_JOB)

    rollups = rollup_rows(client, scope)
    assert len(rollups) == 1
    rollup = rollups[0]
    assert rollup.properties.get("object") == "Special Offers has field: 10 values"
    assert rollup.properties.get("fact") == ("Rollup (portal-kb) summarizes Special Offers has field: 10 values")
    assert rollup.properties.get("rollup_text_source") == ROLLUP_TEXT_SOURCE_NO_TRANSPORT
    # Everything in the label that is not a count is a verbatim span of every
    # member fact — the label cannot assert a relationship the members do not.
    for member_uuid in rollup.properties["derived_from"]:
        member = client.graph.get_relationship(member_uuid)
        assert str(member.properties["fact"]).startswith("Special Offers has field ")


def synthetic_members(
    rows: list[tuple[str, str, str]], *, memory_type: str = MemoryType.PREFERENCE.value
) -> list[GraphRelationship]:
    """Cluster members as stored rows, without going through formation.

    Formation's semantic dedup merges near-identical objects — correct
    behaviour, but it makes clusters whose objects differ only in a trailing
    index unreachable through ``add_memory``.  The labeler reads exactly the
    properties built here, and the end-to-end portal test above covers the
    real-row path.
    """
    return [
        GraphRelationship(
            source_uuid=f"subject-{index}",
            target_uuid=f"object-{index}",
            type="PREFERS",
            properties={
                "fact": f"{subject} {predicate} {object_value}",
                "object": object_value,
                "predicate": predicate,
                "memory_type": memory_type,
            },
        )
        for index, (subject, predicate, object_value) in enumerate(rows)
    ]


@pytest.mark.asyncio
async def test_shared_stem_runs_into_the_objects_so_sibling_clusters_differ(
    tmp_path: Path,
) -> None:
    """The stem is the members' common leading span, not just subject+predicate.

    Three clusters that differ only INSIDE their objects would otherwise get
    the same label ("Acme prefers: 3 values") and be indistinguishable in the
    console.  The stem runs word by word for as long as the member facts
    literally agree, so it stays a verbatim span of every member while keeping
    the siblings apart.
    """
    client = Memotron(
        graph_path=tmp_path / "sibling-stems.sqlite",
        config=synthesis_config(model_identifier=None),
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="sibling-stems")
    labels = [
        client._engine._synthesize_rollup_label(
            synthetic_members([("Acme", "prefers", f"{group} operating preference {index}") for index in range(3)]),
            scope_key=scope.key,
        )
        for group in ("group-a", "group-b", "group-c")
    ]
    assert labels == [
        "Acme prefers group-a operating preference: 3 values",
        "Acme prefers group-b operating preference: 3 values",
        "Acme prefers group-c operating preference: 3 values",
    ]


@pytest.mark.asyncio
async def test_rollup_of_rollups_labels_the_child_labels_not_the_boilerplate(
    tmp_path: Path,
) -> None:
    """Depth-2 members share ``Rollup (<scope>)`` / ``summarizes`` by construction.

    Restating that boilerplate would nest it ("Rollup (x) summarizes Rollup (x)
    summarizes ..."); what carries information is what the CHILD labels agree
    on.
    """
    client = Memotron(
        graph_path=tmp_path / "rollup-of-rollups.sqlite",
        config=synthesis_config(model_identifier=None),
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="depth-two")
    members = synthetic_members(
        [
            (
                "Rollup (depth-two)",
                "summarizes",
                f"Acme prefers {group} operating preference: 3 values",
            )
            for group in ("group-a", "group-b", "group-c")
        ],
        memory_type=MemoryType.ROLLUP.value,
    )
    label = client._engine._synthesize_rollup_label(members, scope_key=scope.key)
    assert label == "Acme prefers: 3 rollups"
    assert "Rollup (depth-two) summarizes" not in label


@pytest.mark.asyncio
async def test_shared_predicate_cluster_states_the_shared_claim(tmp_path: Path) -> None:
    """Subjects vary, predicate and object are shared: state it, count subjects."""
    client = Memotron(
        graph_path=tmp_path / "shared-predicate.sqlite",
        config=synthesis_config(model_identifier=None),
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="shared-predicate")
    label = await fallback_label(
        client,
        scope,
        [(subject, "prefers", "window seating") for subject in ("Priya", "Marco", "Elena")],
    )
    assert label == "prefers window seating: 3 subjects"

    # Predicate shared but nothing else: the count phrase carries the cluster,
    # the predicate is quoted as the one thing they have in common.
    varied_scope = MemoryScope(kind=ScopeKind.USER, scope_id="shared-predicate-only")
    varied = await fallback_label(
        client,
        varied_scope,
        [
            ("Alpha", "requires", "mTLS on the gateway"),
            ("Beta", "requires", "SOC2 evidence"),
            ("Gamma", "requires", "quarterly access review"),
        ],
    )
    assert varied == "requires: 3 related preference facts"


@pytest.mark.asyncio
async def test_shared_subject_only_cluster_is_about_the_subject(tmp_path: Path) -> None:
    client = Memotron(
        graph_path=tmp_path / "shared-subject.sqlite",
        config=synthesis_config(model_identifier=None),
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="shared-subject")
    label = await fallback_label(
        client,
        scope,
        [
            ("D-Scribe", "is", "an ingestion pipeline"),
            ("D-Scribe", "runs on", "the jedai gateway"),
            ("D-Scribe", "emits", "formation receipts"),
        ],
    )
    assert label == "3 related preference facts about D-Scribe"


@pytest.mark.asyncio
async def test_cluster_sharing_nothing_lists_keywords_instead_of_a_pseudo_sentence(
    tmp_path: Path,
) -> None:
    """The last resort names the cluster and offers keywords, comma-separated.

    A comma-separated parenthetical cannot be misread as a claim; the old
    space-joined token bag could, and was.
    """
    client = Memotron(
        graph_path=tmp_path / "shares-nothing.sqlite",
        config=synthesis_config(model_identifier=None),
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="shares-nothing")
    label = await fallback_label(
        client,
        scope,
        [
            ("Alpha", "requires", "portal audit review"),
            ("Beta", "prefers", "portal audit dashboards"),
            ("Gamma", "blocks", "portal audit exports"),
        ],
    )
    assert label == "3 related preference facts (audit, portal)"

    # No shared vocabulary at all: say only what is certain — how many, of what
    # type — rather than inventing a topic.
    bare_scope = MemoryScope(kind=ScopeKind.USER, scope_id="shares-nothing-at-all")
    bare = await fallback_label(
        client,
        bare_scope,
        [
            ("Alpha", "requires", "mTLS"),
            ("Beta", "prefers", "dashboards"),
            ("Gamma", "blocks", "exports"),
        ],
    )
    assert bare == "3 related preference facts"


@pytest.mark.asyncio
async def test_long_shared_stem_is_clipped_without_dropping_the_count(
    tmp_path: Path,
) -> None:
    """The count is what makes the label a description rather than a fragment."""
    client = Memotron(
        graph_path=tmp_path / "long-stem.sqlite",
        config=synthesis_config(model_identifier=None),
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="long-stem")
    label = await fallback_label(
        client,
        scope,
        [
            (
                "Walt Disney World Resort special offers content type definition",
                "has field",
                field,
            )
            for field in (
                "data.web_links.wdw_offer_detail",
                "data.media.hero_image",
                "data.pricing.rate_code",
                "data.dates.book_by",
            )
        ],
    )
    assert len(label) <= 80
    assert label.endswith(": 4 values")
    assert label.startswith("Walt Disney World Resort special offers content type")


# The labeler runs in a fresh interpreter so within-process caching (and a
# single process's hash seed) cannot mask the defect: equal-frequency tokens
# used to be ordered by ``Counter`` insertion, which follows randomized set
# iteration, so the same three facts produced different labels in different
# runs.  An in-process comparison could never see it.
_CROSS_PROCESS_LABEL_SCRIPT = '''
import asyncio
import tempfile
from collections import Counter
from pathlib import Path

from memotron import Memotron, MemoryScope, ScopeKind
from memotron.dreaming import _ROLLUP_LABEL_STOPWORDS
from memotron.graph import normalize_key

# Five tokens tie at frequency three, so both the ORDER and (under a top-N
# slice) the SELECTION were previously hash-seed dependent.
TIED = [
    ("Alpha", "requires", "portal audit review evidence retention window"),
    ("Beta", "prefers", "portal audit review evidence retention budget"),
    ("Gamma", "blocks", "portal audit review evidence retention cadence"),
]
STRUCTURAL = [
    ("Special Offers", "has field", "data.descriptions.offer_intro"),
    ("Special Offers", "has field", "data.media.hero_image"),
    ("Special Offers", "has field", "data.pricing.rate_code"),
]


async def members_for(client, scope, rows):
    uuids = []
    for subject, predicate, object_value in rows:
        result = await client.add_memory(
            subject=subject,
            predicate=predicate,
            object=object_value,
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9,
        )
        uuids.append(result.relationship_uuid)
    return [client.graph.get_relationship(uuid) for uuid in uuids]


def legacy_label(members):
    """The pre-fix ordering: sort by frequency only, ties left in Counter order."""
    token_sets = [
        set(normalize_key(str(member.properties["fact"])).split()) - _ROLLUP_LABEL_STOPWORDS
        for member in members
    ]
    frequency = Counter(token for token_set in token_sets for token in token_set)
    threshold = max(1, len(token_sets) // 2)
    ordered = sorted(
        (token for token, seen in frequency.items() if seen >= threshold),
        key=lambda token: -frequency[token],
    )
    return " ".join(ordered[:6])


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        client = Memotron(graph_path=Path(tmp) / "graph.sqlite")
        tied_scope = MemoryScope(kind=ScopeKind.USER, scope_id="tied")
        structural_scope = MemoryScope(kind=ScopeKind.USER, scope_id="structural")
        tied = await members_for(client, tied_scope, TIED)
        structural = await members_for(client, structural_scope, STRUCTURAL)
        print(client._engine._synthesize_rollup_label(tied, scope_key=tied_scope.key))
        print(
            client._engine._synthesize_rollup_label(
                structural, scope_key=structural_scope.key
            )
        )
        print(legacy_label(tied))


asyncio.run(main())
'''

# Distinct hash seeds perturb string-set iteration order, which is exactly the
# channel the old ordering leaked through.
_HASH_SEEDS = ("0", "1", "8191", "525601")


def test_fallback_label_is_byte_identical_across_interpreter_processes() -> None:
    runs = []
    for seed in _HASH_SEEDS:
        completed = subprocess.run(
            [sys.executable, "-c", _CROSS_PROCESS_LABEL_SCRIPT],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
        )
        lines = completed.stdout.strip().splitlines()
        assert len(lines) == 3, completed.stderr
        runs.append(tuple(lines))

    tied_labels = {run[0] for run in runs}
    structural_labels = {run[1] for run in runs}
    assert tied_labels == {"3 related preference facts (audit, evidence, portal, retention)"}
    assert structural_labels == {"Special Offers has field: 3 values"}

    # Control: the pre-fix ordering really does move between these processes,
    # so the equality above is a property of the fix and not of the fixture.
    legacy_labels = {run[2] for run in runs}
    assert len(legacy_labels) > 1, (
        "hash seeds did not perturb set iteration, so this run proves nothing "
        f"about cross-process determinism: {legacy_labels}"
    )


# ---------------------------------------------------------------------------
# Pure entailment-gate unit tests
# ---------------------------------------------------------------------------

MEMBERS = [
    "Acme Parks requires SOC2 report before vendor approval",
    "Acme Parks requires SOC2 report for the audit portal",
    "Acme Parks requires SOC2 report signed by 2026-09-01",
]


def test_gate_passes_fully_entailed_summary() -> None:
    ok, reason = rollup_summary_entailed("Acme Parks requires SOC2 report before vendor approval.", MEMBERS)
    assert ok and reason == ""


def test_gate_ignores_stopwords() -> None:
    ok, reason = rollup_summary_entailed(
        "The Acme Parks requires SOC2 report and it will be for the vendor approval.",
        MEMBERS,
    )
    assert ok, reason


def test_gate_rejects_new_entity_token() -> None:
    ok, reason = rollup_summary_entailed("Acme Parks requires SOC2 report from Deloitte.", MEMBERS)
    assert not ok and reason == "unentailed_token:deloitte"


def test_gate_rejects_new_number_token() -> None:
    ok, reason = rollup_summary_entailed("Acme Parks requires SOC2 report signed by 2027-09-01.", MEMBERS)
    assert not ok and reason == "unentailed_token:2027"


def test_gate_requires_numbers_verbatim() -> None:
    # "soc2" is entailed as a content token but the recased form breaks the
    # verbatim rule — identifiers are preserved exactly or not at all.
    ok, reason = rollup_summary_entailed("Acme Parks requires soc2 report before vendor approval.", MEMBERS)
    assert not ok and reason == "identifier_not_verbatim:soc2"


def test_gate_requires_identifiers_verbatim() -> None:
    members = ["Pipeline kb_ds_source_wdw_en_us_v1 governs ingest routing"]
    ok, reason = rollup_summary_entailed("Pipeline KB_DS_SOURCE_WDW_EN_US_V1 governs ingest routing", members)
    assert not ok and reason == "identifier_not_verbatim:KB_DS_SOURCE_WDW_EN_US_V1"
    ok, _ = rollup_summary_entailed("Pipeline kb_ds_source_wdw_en_us_v1 governs ingest routing", members)
    assert ok


def test_gate_accepts_decimals_and_versions_without_sentence_splitting() -> None:
    members = ["Service latency budget is 2.5 seconds per release v1.2"]
    ok, reason = rollup_summary_entailed("Service latency budget is 2.5 seconds per release v1.2", members)
    assert ok, reason


def test_gate_enforces_length_and_sentence_caps() -> None:
    long_summary = "Acme Parks requires SOC2 report " * 10
    ok, reason = rollup_summary_entailed(long_summary, MEMBERS)
    assert not ok and reason.startswith("length_exceeded:")

    ok, reason = rollup_summary_entailed(
        "Acme requires SOC2. Acme requires SOC2. Acme requires SOC2.",
        MEMBERS,
    )
    assert not ok and reason.startswith("sentence_count_exceeded:3")


def test_gate_rejects_blank_summary() -> None:
    ok, reason = rollup_summary_entailed("   ", MEMBERS)
    assert not ok and reason == "summary_blank"
