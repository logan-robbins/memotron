"""WS-8 acceptance proof tests for Memotron interop & adoption surface.

Four required proof tests:
1. test_memory_tool_backend_roundtrip           — full memory_20250818 command set
2. test_memory_tool_optimistic_concurrency_conflict — stale vs current sha256
3. test_memory_tool_read_only_mount_rejects_write   — read-only mount guard
4. test_memory_router_enforces_scope_and_injects_memory — router policy + injection

All tests are hermetic: zero network calls, zero disk writes outside tmp_path.
"""

from __future__ import annotations

import pytest

from memotron import (
    AnthropicMemoryToolBackend,
    DreamRunSummary,
    EchoUpstreamTransport,
    MemoryBank,
    MemoryRouter,
    MemoryScope,
    MemoryToolBackend,
    MemoryToolCommandError,
    MemoryToolConflictError,
    MemoryToolNotFoundError,
    MemoryToolPathError,
    MemoryToolReadOnlyError,
    Motive,
    ScopeKind,
)
from memotron.interop import (
    MotiveAsInstructionsAdapter,
    _content_sha256,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_scope(kind: str = "user", scope_id: str = "ws8-test") -> MemoryScope:
    return MemoryScope(kind=ScopeKind(kind), scope_id=scope_id)


def rw_backend(scope: MemoryScope | None = None) -> AnthropicMemoryToolBackend:
    return AnthropicMemoryToolBackend(scope=scope or make_scope(), mount_mode="read_write")


def ro_backend(scope: MemoryScope | None = None) -> AnthropicMemoryToolBackend:
    return AnthropicMemoryToolBackend(scope=scope or make_scope(), mount_mode="read_only")


# ---------------------------------------------------------------------------
# 1. test_memory_tool_backend_roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_tool_backend_roundtrip() -> None:
    """Exercise the full memory_20250818 command set:
    create → view → str_replace → insert → rename → delete.

    Verifies:
    - Each command returns ok=True with the documented fields.
    - content_sha256 is a real SHA-256 hex digest.
    - version increments with each write.
    - view lists the file in the directory listing.
    - Scope isolation: a backend for scope A cannot see scope B's files.
    """
    scope_a = make_scope(scope_id="scope-a")
    scope_b = make_scope(scope_id="scope-b")
    backend_a = rw_backend(scope_a)
    backend_b = rw_backend(scope_b)

    # --- create ---
    result = backend_a.handle(
        {
            "command": "create",
            "path": "/memories/prefs.md",
            "content": "# Preferences\n- Prefers concise answers",
        }
    )
    assert result["ok"] is True
    assert result["path"] == "/memories/prefs.md"
    assert result["version"] == 1
    original_hash = result["content_sha256"]
    assert original_hash == _content_sha256("# Preferences\n- Prefers concise answers")

    # --- view file ---
    view = backend_a.handle({"command": "view", "path": "/memories/prefs.md"})
    assert view["ok"] is True
    assert view["type"] == "file"
    assert view["content"] == "# Preferences\n- Prefers concise answers"
    assert view["content_sha256"] == original_hash
    assert view["version"] == 1

    # --- view directory listing ---
    listing = backend_a.handle({"command": "view", "path": "/memories/"})
    assert listing["ok"] is True
    assert listing["type"] == "directory"
    assert "/memories/prefs.md" in listing["entries"]

    # --- str_replace ---
    replace_result = backend_a.handle(
        {
            "command": "str_replace",
            "path": "/memories/prefs.md",
            "old_str": "- Prefers concise answers",
            "new_str": "- Prefers concise answers\n- Prefers bullet lists",
        }
    )
    assert replace_result["ok"] is True
    assert replace_result["version"] == 2
    new_hash = replace_result["content_sha256"]
    assert new_hash != original_hash

    # Verify content after str_replace
    view2 = backend_a.handle({"command": "view", "path": "/memories/prefs.md"})
    assert "- Prefers bullet lists" in view2["content"]

    # --- insert (append a line after line 1) ---
    insert_result = backend_a.handle(
        {
            "command": "insert",
            "path": "/memories/prefs.md",
            "insert_line": 1,
            "new_str": "- Prefers dark mode",
        }
    )
    assert insert_result["ok"] is True
    assert insert_result["version"] == 3

    view3 = backend_a.handle({"command": "view", "path": "/memories/prefs.md"})
    assert "- Prefers dark mode" in view3["content"]

    # --- rename ---
    rename_result = backend_a.handle(
        {
            "command": "rename",
            "path": "/memories/prefs.md",
            "new_path": "/memories/user-prefs.md",
        }
    )
    assert rename_result["ok"] is True
    assert rename_result["renamed_from"] == "/memories/prefs.md"
    assert rename_result["renamed_to"] == "/memories/user-prefs.md"

    # Old path is gone, new path exists.
    with pytest.raises(MemoryToolNotFoundError):
        backend_a.handle({"command": "view", "path": "/memories/prefs.md"})
    view_renamed = backend_a.handle({"command": "view", "path": "/memories/user-prefs.md"})
    assert view_renamed["ok"] is True

    # --- delete ---
    del_result = backend_a.handle({"command": "delete", "path": "/memories/user-prefs.md"})
    assert del_result["ok"] is True

    with pytest.raises(MemoryToolNotFoundError):
        backend_a.handle({"command": "view", "path": "/memories/user-prefs.md"})

    # --- scope isolation ---
    # scope_b should not see scope_a's files (already deleted, but check a fresh file).
    backend_a.handle(
        {
            "command": "create",
            "path": "/memories/secret.md",
            "content": "scope-a only",
        }
    )
    # scope_b's listing is empty (no files written to scope_b).
    listing_b = backend_b.handle({"command": "view", "path": "/memories/"})
    assert "/memories/secret.md" not in listing_b["entries"]


@pytest.mark.asyncio
async def test_memory_tool_backend_path_validation() -> None:
    """Path-traversal and invalid-root paths are rejected fail-fast."""
    backend = rw_backend()

    with pytest.raises(MemoryToolPathError):
        backend.handle({"command": "create", "path": "/memories/../etc/passwd", "content": "x"})

    with pytest.raises(MemoryToolPathError):
        backend.handle({"command": "create", "path": "/etc/passwd", "content": "x"})

    # blank path → MemoryToolCommandError (require_path fires before path validation)
    with pytest.raises(MemoryToolCommandError):
        backend.handle({"command": "create", "path": "", "content": "x"})


@pytest.mark.asyncio
async def test_memory_tool_unknown_command_fails_fast() -> None:
    backend = rw_backend()
    with pytest.raises(MemoryToolCommandError):
        backend.handle({"command": "nonexistent", "path": "/memories/x.md"})


@pytest.mark.asyncio
async def test_memory_tool_str_replace_not_found() -> None:
    backend = rw_backend()
    backend.handle({"command": "create", "path": "/memories/file.md", "content": "hello"})
    with pytest.raises(MemoryToolCommandError, match="not found"):
        backend.handle(
            {
                "command": "str_replace",
                "path": "/memories/file.md",
                "old_str": "goodbye",
                "new_str": "hi",
            }
        )


@pytest.mark.asyncio
async def test_memory_tool_str_replace_ambiguous() -> None:
    backend = rw_backend()
    backend.handle(
        {
            "command": "create",
            "path": "/memories/file.md",
            "content": "foo foo foo",
        }
    )
    with pytest.raises(MemoryToolCommandError, match="3 times"):
        backend.handle(
            {
                "command": "str_replace",
                "path": "/memories/file.md",
                "old_str": "foo",
                "new_str": "bar",
            }
        )


# ---------------------------------------------------------------------------
# 2. test_memory_tool_optimistic_concurrency_conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_tool_optimistic_concurrency_conflict() -> None:
    """Stale expected_hash fails fast; current hash succeeds; prior version retrievable.

    Verifies:
    - A write with a stale expected_hash raises MemoryToolConflictError.
    - A write with the current expected_hash succeeds and increments version.
    - The prior immutable version is still retrievable via get_versions().
    """
    backend = rw_backend()

    # Create the file — get its hash.
    r1 = backend.handle(
        {
            "command": "create",
            "path": "/memories/state.md",
            "content": "v1 content",
        }
    )
    hash_v1 = r1["content_sha256"]
    assert r1["version"] == 1

    # Overwrite with overwrite=True to get v2.
    r2 = backend.handle(
        {
            "command": "create",
            "path": "/memories/state.md",
            "content": "v2 content",
            "overwrite": True,
            "expected_hash": hash_v1,  # correct hash → succeeds
        }
    )
    hash_v2 = r2["content_sha256"]
    assert r2["version"] == 2
    assert hash_v2 != hash_v1

    # Now try a conditional write with the STALE v1 hash → must fail.
    with pytest.raises(MemoryToolConflictError):
        backend.handle(
            {
                "command": "create",
                "path": "/memories/state.md",
                "content": "v3 content",
                "overwrite": True,
                "expected_hash": hash_v1,  # stale!
            }
        )

    # The current version is still v2 (write was rolled back).
    view = backend.handle({"command": "view", "path": "/memories/state.md"})
    assert view["version"] == 2
    assert view["content"] == "v2 content"

    # Prior version (v1) is still retrievable via get_versions().
    versions = backend.get_versions("/memories/state.md")
    assert len(versions) == 2
    assert versions[0].version == 1
    assert versions[0].content == "v1 content"
    assert versions[0].content_sha256 == hash_v1
    assert versions[1].version == 2
    assert versions[1].content == "v2 content"

    # A conditional write with the current hash (hash_v2) succeeds.
    r3 = backend.handle(
        {
            "command": "create",
            "path": "/memories/state.md",
            "content": "v3 content",
            "overwrite": True,
            "expected_hash": hash_v2,  # current → succeeds
        }
    )
    assert r3["version"] == 3

    # All three versions are retained (immutable history).
    all_versions = backend.get_versions("/memories/state.md")
    assert len(all_versions) == 3
    assert all_versions[0].content == "v1 content"
    assert all_versions[1].content == "v2 content"
    assert all_versions[2].content == "v3 content"


@pytest.mark.asyncio
async def test_memory_tool_str_replace_concurrency() -> None:
    """str_replace also enforces optimistic concurrency via expected_hash."""
    backend = rw_backend()
    r1 = backend.handle(
        {
            "command": "create",
            "path": "/memories/doc.md",
            "content": "alpha beta",
        }
    )
    hash_v1 = r1["content_sha256"]

    # Mutate to v2 first (simulates another writer).
    backend.handle(
        {
            "command": "str_replace",
            "path": "/memories/doc.md",
            "old_str": "alpha",
            "new_str": "ALPHA",
        }
    )

    # Now str_replace with the stale v1 hash → conflict.
    with pytest.raises(MemoryToolConflictError):
        backend.handle(
            {
                "command": "str_replace",
                "path": "/memories/doc.md",
                "old_str": "beta",
                "new_str": "BETA",
                "expected_hash": hash_v1,  # stale
            }
        )


# ---------------------------------------------------------------------------
# 3. test_memory_tool_read_only_mount_rejects_write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_tool_read_only_mount_rejects_write() -> None:
    """All write commands (create / str_replace / insert / delete / rename) fail
    fast with MemoryToolReadOnlyError on a read-only mount.

    View (read) commands succeed on a read-only mount.
    """
    scope = make_scope()
    backend = ro_backend(scope)

    # view (read) on empty directory works fine.
    listing = backend.handle({"command": "view", "path": "/memories/"})
    assert listing["ok"] is True

    # All write commands must fail fast.
    write_commands: list[dict] = [
        {"command": "create", "path": "/memories/x.md", "content": "data"},
        {"command": "str_replace", "path": "/memories/x.md", "old_str": "a", "new_str": "b"},
        {"command": "insert", "path": "/memories/x.md", "insert_line": 0, "new_str": "line"},
        {"command": "delete", "path": "/memories/x.md"},
        {"command": "rename", "path": "/memories/x.md", "new_path": "/memories/y.md"},
    ]
    for cmd in write_commands:
        with pytest.raises(MemoryToolReadOnlyError, match="read-only"):
            backend.handle(cmd)

    # Confirm the backend is still clean (no side effects).
    assert not backend.get_versions("/memories/x.md")


@pytest.mark.asyncio
async def test_memory_tool_read_only_allows_reads() -> None:
    """A read-only backend seeded with pre-existing data returns it correctly.

    Because writes require a read_write backend, we seed data via a rw backend,
    then demonstrate the ro backend shares no in-process storage (separate
    instances in this POC model).  The test validates that the ro flag itself
    does not block view commands when the backend has content (we seed by
    calling a rw backend first and reconstructing the ro backend with no state,
    then directly pushing to the internal store to simulate a pre-seeded mount).
    """
    scope = make_scope()
    ro = ro_backend(scope)

    # Directly seed the store (simulating a pre-seeded read-only mount).
    from memotron.interop import MemoryFile, MemoryFileVersion, _content_sha256

    content = "pre-seeded content"
    version = MemoryFileVersion(
        version=1,
        content=content,
        content_sha256=_content_sha256(content),
    )
    ro._store[(scope.key, "/memories/seed.md")] = MemoryFile(
        path="/memories/seed.md",
        scope_key=scope.key,
        versions=[version],
    )

    # View succeeds.
    view = ro.handle({"command": "view", "path": "/memories/seed.md"})
    assert view["content"] == content

    # Write still rejected.
    with pytest.raises(MemoryToolReadOnlyError):
        ro.handle({"command": "str_replace", "path": "/memories/seed.md", "old_str": "pre", "new_str": "post"})


# ---------------------------------------------------------------------------
# 4. test_memory_router_enforces_scope_and_injects_memory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_router_enforces_scope_and_injects_memory() -> None:
    """The router injects retrieved memory context for the correct tenant scope
    and refuses a cross-tenant request — using the hermetic EchoUpstreamTransport.

    Verifies:
    - Memory context from memory_context_provider is injected as a system message.
    - The correct Motive goal is annotated in the system message.
    - A cross-tenant request (wrong scope key) fails fast with ValueError.
    - The upstream echo response is passed back unmodified plus _memotron_injected.
    - No real network calls are made.
    """
    tenant_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="disney-enterprise")
    other_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="other-tenant")

    # A simple hermetic memory context provider (no Memotron instance needed).
    async def memory_provider(scope: MemoryScope) -> str:
        if scope.key == tenant_scope.key:
            return "Memory profile for disney-enterprise\nStatic facts:\n- Acme requires SOC2 report"
        return ""

    motive = Motive(
        name="learn-compliance-requirements",
        goal="Track and maintain compliance requirements for enterprise customers.",
    )
    bank = MemoryBank(motives=[motive])

    router = MemoryRouter(
        scope=tenant_scope,
        memory_context_provider=memory_provider,
        upstream=EchoUpstreamTransport(),  # hermetic, no network
        memory_bank=bank,
        default_motive_name="learn-compliance-requirements",
    )

    # --- happy path: correct tenant scope ---
    request = {
        "_scope": tenant_scope,
        "model": "claude-3-5-haiku",
        "messages": [
            {"role": "user", "content": "What are the compliance requirements?"},
        ],
    }
    response = await router.route(request)

    # Response carries injection metadata.
    assert "_memotron_injected" in response
    injected_meta = response["_memotron_injected"]
    assert injected_meta["scope_key"] == tenant_scope.key
    assert injected_meta["motive_name"] == "learn-compliance-requirements"
    assert injected_meta["memory_context_chars"] > 0

    # The echo response should contain the injected system message content.
    echo_content = response["choices"][0]["message"]["content"]
    assert "SOC2 report" in echo_content  # memory context injected
    assert "Track and maintain compliance requirements" in echo_content  # motive goal

    # Private _scope field is NOT forwarded to upstream (stripped from payload).
    assert "_scope" not in echo_content

    # --- cross-tenant request: wrong scope key → ValueError ---
    bad_request = {
        "_scope": other_scope,  # wrong tenant!
        "model": "claude-3-5-haiku",
        "messages": [{"role": "user", "content": "Leak disney data"}],
    }
    with pytest.raises(ValueError, match="cross-tenant"):
        await router.route(bad_request)


@pytest.mark.asyncio
async def test_memory_router_no_scope_in_request() -> None:
    """When no _scope is present in the request, the router uses its own scope
    and still injects memory context.  No cross-tenant check fires."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    call_log: list[str] = []

    async def memory_provider(s: MemoryScope) -> str:
        call_log.append(s.key)
        return "User memory context"

    router = MemoryRouter(
        scope=scope,
        memory_context_provider=memory_provider,
        upstream=EchoUpstreamTransport(),
    )
    response = await router.route(
        {
            "model": "any",
            "messages": [{"role": "user", "content": "Hello"}],
        }
    )
    # Provider called with the router's scope.
    assert scope.key in call_log
    # Memory context injected.
    assert "User memory context" in response["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_memory_router_motive_annotation_in_system_message() -> None:
    """Motive goal appears in the injected system message."""
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="agent-1")

    async def provider(s: MemoryScope) -> str:
        return "Agent memory"

    motive = Motive(name="distill-lessons", goal="Distill reusable agent operating lessons.")
    bank = MemoryBank(motives=[motive])

    router = MemoryRouter(
        scope=scope,
        memory_context_provider=provider,
        upstream=EchoUpstreamTransport(),
        memory_bank=bank,
        default_motive_name="distill-lessons",
    )
    response = await router.route({"model": "any", "messages": [{"role": "user", "content": "Hi"}]})
    system_content = response["choices"][0]["message"]["content"]
    assert "Distill reusable agent operating lessons" in system_content


@pytest.mark.asyncio
async def test_memory_router_motive_override_per_request() -> None:
    """Per-request _motive_name overrides the router's default_motive_name."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="u1")

    async def provider(s: MemoryScope) -> str:
        return ""

    m1 = Motive(name="motive-a", goal="Goal A")
    m2 = Motive(name="motive-b", goal="Goal B")
    bank = MemoryBank(motives=[m1, m2])

    router = MemoryRouter(
        scope=scope,
        memory_context_provider=provider,
        upstream=EchoUpstreamTransport(),
        memory_bank=bank,
        default_motive_name="motive-a",
    )
    response = await router.route(
        {
            "_motive_name": "motive-b",
            "model": "any",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    )
    assert response["_memotron_injected"]["motive_name"] == "motive-b"
    assert "Goal B" in response["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_memory_router_existing_system_message_preserved() -> None:
    """Memory context is prepended; the original system message is preserved."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="u2")

    async def provider(s: MemoryScope) -> str:
        return "Memory block"

    router = MemoryRouter(
        scope=scope,
        memory_context_provider=provider,
        upstream=EchoUpstreamTransport(),
    )
    response = await router.route(
        {
            "model": "any",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hi"},
            ],
        }
    )
    echo = response["choices"][0]["message"]["content"]
    assert "Memory block" in echo
    assert "You are a helpful assistant." in echo


# ---------------------------------------------------------------------------
# MotiveAsInstructionsAdapter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_motive_as_instructions_adapter_run_dream() -> None:
    """MotiveAsInstructionsAdapter.run_dream() returns a DreamRunSummary keyed
    to the Motive and aggregates the dream run result correctly.
    """
    from datetime import UTC, datetime

    from memotron.models import DreamJobKind, DreamJobRun, DreamRunResult

    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    motive = Motive(
        name="learn-compliance-requirements",
        goal="Track enterprise compliance requirements.",
    )

    # Fake dream runner that returns a canned result.
    async def fake_runner(*, job_name: str) -> DreamRunResult:
        return DreamRunResult(
            ran_at=datetime.now(UTC),
            job_runs=[
                DreamJobRun(
                    job_name=job_name,
                    job_kind=DreamJobKind.FORMATION,
                    processed_episodes=5,
                    created_relationships=3,
                    reinforced_relationships=1,
                    superseded_relationships=1,
                )
            ],
        )

    adapter = MotiveAsInstructionsAdapter(
        motive=motive,
        scope=scope,
        dream_runner=fake_runner,
    )

    summary = await adapter.run_dream(job_name="formation-default")

    assert isinstance(summary, DreamRunSummary)
    assert summary.motive_name == "learn-compliance-requirements"
    assert summary.motive_goal == "Track enterprise compliance requirements."
    assert summary.input_scope_key == scope.key
    assert summary.episodes_seen == 5
    assert summary.relationships_created == 3
    assert summary.reinforced == 1
    assert summary.superseded == 1
    assert summary.reviewable is True
    assert "Motive" in summary.notes
    assert motive.name in summary.notes


# ---------------------------------------------------------------------------
# MemoryToolBackend alias
# ---------------------------------------------------------------------------


def test_memory_tool_backend_alias() -> None:
    """MemoryToolBackend is an alias for AnthropicMemoryToolBackend."""
    assert MemoryToolBackend is AnthropicMemoryToolBackend


# ---------------------------------------------------------------------------
# EchoUpstreamTransport hermetic test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_echo_upstream_transport_no_network() -> None:
    """EchoUpstreamTransport makes zero network calls and returns a deterministic response."""
    transport = EchoUpstreamTransport()
    response = await transport.chat_completion(
        {
            "model": "test",
            "messages": [
                {"role": "system", "content": "SYSTEM_CONTENT"},
                {"role": "user", "content": "hi"},
            ],
        }
    )
    assert response["_memotron_echo"] is True
    assert response["choices"][0]["message"]["role"] == "assistant"
    assert "SYSTEM_CONTENT" in response["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Mount mode validation
# ---------------------------------------------------------------------------


def test_invalid_mount_mode_rejected() -> None:
    """Unknown mount modes fail fast at construction time."""
    from memotron.interop import MemoryToolError

    with pytest.raises(MemoryToolError, match="mount_mode"):
        AnthropicMemoryToolBackend(scope=make_scope(), mount_mode="invalid")


# ---------------------------------------------------------------------------
# Import smoke test — all public exports present
# ---------------------------------------------------------------------------


def test_interop_exports_available() -> None:
    """All WS-8 public classes are importable from the memotron package."""
    from memotron import (
        AnthropicMemoryToolBackend,
        DreamRunSummary,
        EchoUpstreamTransport,
        MemoryFile,
        MemoryFileVersion,
        MemoryRouter,
        MemoryToolBackend,
        MemoryToolCommandError,
        MemoryToolConflictError,
        MemoryToolError,
        MemoryToolNotFoundError,
        MemoryToolPathError,
        MemoryToolReadOnlyError,
        MotiveAsInstructionsAdapter,
        UpstreamTransport,
    )

    assert AnthropicMemoryToolBackend is not None
    assert MemoryToolBackend is AnthropicMemoryToolBackend
    assert DreamRunSummary is not None
    assert EchoUpstreamTransport is not None
    assert MemoryRouter is not None
    assert UpstreamTransport is not None
    assert MemoryFile is not None
    assert MemoryFileVersion is not None
    assert MotiveAsInstructionsAdapter is not None
    # Error hierarchy
    assert issubclass(MemoryToolCommandError, MemoryToolError)
    assert issubclass(MemoryToolConflictError, MemoryToolError)
    assert issubclass(MemoryToolNotFoundError, MemoryToolError)
    assert issubclass(MemoryToolPathError, MemoryToolError)
    assert issubclass(MemoryToolReadOnlyError, MemoryToolError)
