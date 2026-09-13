"""Motive lifecycle fidelity: orphaned-motive retention, per-memory version pinning.

Closes AUDIT.md §3 gaps: deleting/renaming a Motive must not crash pruning for
memories formed under it, and every formed memory row must pin the exact
Motive version (digest) that governed its formation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Self

import pytest

from memotron import (
    Memotron,
    EpisodeType,
    MemoryScope,
    ScopeKind,
)
from memotron.config import MemoryBank, Motive, PruningPolicy, default_config

_HEX_DIGEST_LENGTH = 64


def _bank(*motives: Motive) -> MemoryBank:
    return MemoryBank(motives=list(motives))


def _requirement_episode_body() -> str:
    return (
        "Memory: subject=Acme Parks; predicate=requires; object=SOC2 before vendor approval; "
        "relationship_type=REQUIRES; confidence=0.95"
    )


async def _form_under_motive(
    graph_path,
    *,
    motive: Motive,
    scope: MemoryScope,
    reference_time: datetime,
) -> Memotron:
    config = default_config().model_copy(update={"memory_bank": _bank(motive)})
    client = Memotron(graph_path=graph_path, config=config)
    await client.add_episode(
        name="req-episode",
        episode_body=_requirement_episode_body(),
        source=EpisodeType.MESSAGE,
        scope=scope,
        motive=motive.name,
        reference_time=reference_time,
    )
    await client.run_due_dreams()
    return client


@pytest.mark.asyncio
async def test_pruning_survives_orphaned_motive(tmp_path) -> None:
    """A memory whose motive_name no longer exists in the bank must still prune.

    Pre-fix behaviour: _retention_policy_for_relationship raised ValueError from
    MemoryBank.motive() and the whole pruning run crashed.  Post-fix: retention
    falls back to config.pruning.retention and the orphan is visible in the
    receipted policy_source.
    """
    graph_path = tmp_path / "orphaned-motive.sqlite"
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="orphan-test")
    old_time = datetime(2026, 1, 1, tzinfo=UTC)

    forming = await _form_under_motive(
        graph_path,
        motive=Motive(name="temp-motive", goal="Temporary formation motive"),
        scope=scope,
        reference_time=old_time,
    )
    formed = await forming.search(query="SOC2 vendor approval", scope=scope, as_of=old_time)
    assert formed, "formation under the motive must materialize the fact"
    assert formed[0].relationship_uuid

    stored = forming.graph.get_relationship(formed[0].relationship_uuid)
    assert stored.properties["motive_name"] == "temp-motive"

    # Rebuild the engine over the same graph with a bank that no longer knows
    # the motive, and a pruning policy that forces the aged row through the
    # retention gate.
    orphan_config = default_config().model_copy(
        update={
            "memory_bank": _bank(Motive(name="unrelated-motive", goal="Different motive")),
            "pruning": PruningPolicy(active_max_age_seconds=60),
        }
    )
    pruning_client = Memotron(graph_path=graph_path, config=orphan_config)

    # Must not raise despite the orphaned motive_name on the stored row.
    # run_dream_job bypasses cadence (the forming client just ran the same-named
    # pruning job, so run_due_dreams would skip it as not yet due).
    await pruning_client.run_dream_job(job_name="pruning-default")

    decisions = await pruning_client.dream_decisions()
    policy_sources = [
        str(decision.details.get("policy_source", ""))
        for decision in decisions
        if isinstance(decision.details, dict) and "policy_source" in decision.details
    ]
    assert any(source == "config.pruning.retention:orphaned_motive:temp-motive" for source in policy_sources), (
        f"orphaned motive must be surfaced in policy_source, saw: {policy_sources}"
    )


@pytest.mark.asyncio
async def test_motive_version_digest_stamped_on_formation(tmp_path) -> None:
    """Formed rows pin the exact Motive version digest that governed formation."""
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="digest-test")
    when = datetime(2026, 7, 1, tzinfo=UTC)
    motive = Motive(name="pin-motive", goal="Version pin baseline")

    first = await _form_under_motive(tmp_path / "digest-a.sqlite", motive=motive, scope=scope, reference_time=when)
    results = await first.search(query="SOC2 vendor approval", scope=scope)
    assert results
    row = first.graph.get_relationship(results[0].relationship_uuid)
    digest = row.properties["motive_version_digest"]
    assert isinstance(digest, str) and len(digest) == _HEX_DIGEST_LENGTH
    int(digest, 16)  # must be hex

    # Determinism: an identical Motive in a fresh store produces the same digest.
    second = await _form_under_motive(tmp_path / "digest-b.sqlite", motive=motive, scope=scope, reference_time=when)
    results_b = await second.search(query="SOC2 vendor approval", scope=scope)
    row_b = second.graph.get_relationship(results_b[0].relationship_uuid)
    assert row_b.properties["motive_version_digest"] == digest

    # Sensitivity: editing the Motive under the same name changes the digest,
    # so a memory row alone distinguishes which version formed it.
    edited = Motive(name="pin-motive", goal="Version pin EDITED goal")
    third = await _form_under_motive(tmp_path / "digest-c.sqlite", motive=edited, scope=scope, reference_time=when)
    results_c = await third.search(query="SOC2 vendor approval", scope=scope)
    row_c = third.graph.get_relationship(results_c[0].relationship_uuid)
    edited_digest = row_c.properties["motive_version_digest"]
    assert isinstance(edited_digest, str) and len(edited_digest) == _HEX_DIGEST_LENGTH
    assert edited_digest != digest


@pytest.mark.asyncio
async def test_motive_version_digest_none_without_motive(tmp_path) -> None:
    """Direct writes with no governing Motive stamp an explicit null digest."""
    client = Memotron(graph_path=tmp_path / "no-motive.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="no-motive")
    result = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="concise answers",
        relationship_type="PREFERS",
        scope=scope,
    )
    row = client.graph.get_relationship(result.relationship_uuid)
    assert row.properties["motive_name"] is None
    assert row.properties["motive_version_digest"] is None


@pytest.mark.asyncio
async def test_receipts_relationship_uuid_index_exists(tmp_path) -> None:
    """The per-memory receipts join (relationship_uuid) must be index-backed."""
    client = Memotron(graph_path=tmp_path / "receipts-index.sqlite")
    rows = client.graph._connection.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='memory_receipts_relationship_uuid_idx'"
    ).fetchall()
    assert rows, "memory_receipts.relationship_uuid index must exist"


# ---------------------------------------------------------------------------
# WS-21 T26 — per-Motive extraction system prompt
# ---------------------------------------------------------------------------


_OVERRIDE_PROMPT = (
    "You are a compliance-grade extractor. Extract ONLY explicitly dated "
    "requirements; calibrate confidence at 0.95 minimum."
)


class _RequestCapturingTransport:
    """Offline stub capturing the full ExtractionRequest the engine builds."""

    def __init__(self) -> None:
        self.requests: list[object] = []

    async def extract_memories(self, request) -> list[dict]:
        self.requests.append(request)
        return []


class _FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args) -> None:
        return None


@pytest.mark.asyncio
async def test_motive_system_prompt_override_threaded_through_formation(tmp_path) -> None:
    """The engine passes the resolved Motive's override on the ExtractionRequest.

    None (no motive / no override) must leave the field None — the transports
    then send the instruction-set system prompt exactly as before.
    """
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="sys-prompt-thread")
    motive = Motive(
        name="strict-motive",
        goal="Strict temporal extraction",
        system_prompt_override=_OVERRIDE_PROMPT,
    )
    transport = _RequestCapturingTransport()
    config = default_config().model_copy(update={"memory_bank": _bank(motive)})
    client = Memotron(
        graph_path=tmp_path / "thread.sqlite",
        config=config,
        extraction_transport=transport,
    )
    await client.add_episode(
        name="with-motive",
        episode_body=_requirement_episode_body(),
        source=EpisodeType.MESSAGE,
        scope=scope,
        motive=motive.name,
    )
    await client.add_episode(
        name="without-motive",
        episode_body=_requirement_episode_body(),
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.run_due_dreams()

    assert len(transport.requests) == 2
    by_name = {request.episode.name: request for request in transport.requests}
    assert by_name["with-motive"].system_prompt_override == _OVERRIDE_PROMPT
    assert by_name["without-motive"].system_prompt_override is None


@pytest.mark.asyncio
async def test_llm_transport_sends_override_as_system_message(monkeypatch) -> None:
    """The OpenAI-compatible transport sends the override as the system message;
    with no override the payload is byte-for-byte the instruction-set system
    prompt."""
    import json as _json

    from memotron import gateway as gateway_module
    from memotron.extraction import (
        ExtractionRequest,
        OpenAICompatibleExtractionTransport,
    )
    from memotron.models import Episode

    config = default_config()
    instructions = config.instruction_set("default")
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="sys-prompt-wire")
    episode = Episode(
        name="wire-check",
        body=_requirement_episode_body(),
        source=EpisodeType.MESSAGE,
        source_description="test",
        scope=scope,
        reference_time=datetime(2026, 7, 1, tzinfo=UTC),
    )

    def _request(override: str | None) -> ExtractionRequest:
        return ExtractionRequest(
            episode=episode,
            instructions=instructions,
            graph_context=[],
            prompt_profile=None,
            prompt="episode prompt",
            system_prompt_override=override,
        )

    captured: list[dict] = []

    def fake_urlopen(http_request, timeout=None):
        payload = _json.loads(http_request.data.decode("utf-8"))
        captured.append(payload)
        assert http_request.full_url.endswith("/chat/completions")
        body = _json.dumps({"choices": [{"message": {"content": '{"memories": []}'}}]})
        return _FakeHttpResponse(body.encode("utf-8"))

    # `gateway`, not `extraction`: the urlopen call lives in
    # OpenAICompatibleTransport._open. urllib.request is one shared module
    # object, so the reach is identical either way.
    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", fake_urlopen)

    openai = OpenAICompatibleExtractionTransport(model="gpt-test", api_key="test-key")

    await openai.extract_memories(_request(_OVERRIDE_PROMPT))
    await openai.extract_memories(_request(None))

    openai_with, openai_without = captured
    openai_system = next(message for message in openai_with["messages"] if message["role"] == "system")
    assert openai_system["content"] == _OVERRIDE_PROMPT
    # None → exactly today's behaviour, byte-for-byte.
    openai_system_default = next(message for message in openai_without["messages"] if message["role"] == "system")
    assert openai_system_default["content"] == instructions.system_prompt


def test_system_prompt_override_changes_motive_and_contract_digests() -> None:
    """The override is frozen policy: it must shift motive_version_digest AND the
    FormationContract digest (the contract dumps the full Motive)."""
    from memotron.config import DedupPolicy, FormationContract
    from memotron.receipts import motive_version_digest, payload_digest

    base = Motive(name="digest-motive", goal="Digest sensitivity baseline")
    overridden = Motive(
        name="digest-motive",
        goal="Digest sensitivity baseline",
        system_prompt_override=_OVERRIDE_PROMPT,
    )
    assert "system_prompt_override" in base.model_dump()
    assert motive_version_digest(base, {}) != motive_version_digest(overridden, {})

    config = default_config()
    instructions = config.instruction_set("default")
    profile = config.prompt_profile("support-memory", "v1")

    def _contract_digest(motive: Motive) -> str:
        contract = FormationContract(
            scope_key="customer:digest",
            instruction_set=instructions.model_dump(mode="json"),
            formation_profile=profile,
            motive=motive,
            governance=None,
            dedup=DedupPolicy(),
        )
        return payload_digest(contract.model_dump(mode="json"))

    assert _contract_digest(base) != _contract_digest(overridden)


def test_blank_system_prompt_override_fails_fast() -> None:
    with pytest.raises(ValueError, match="system_prompt_override cannot be blank"):
        Motive(name="bad", goal="goal", system_prompt_override="   ")
