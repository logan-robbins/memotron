"""The agent-memory surface must consult the CALLER, not just the agent id it was handed.

#206 Phase 2.

`_authorize` already resolved a principal before this change — but it built one from the
`agent_id` the caller supplied (`principal_for_agent`), which is self-attesting: it proves
the *agent* may reach the scope, never that *this caller* may act as that agent.

**The tenant is the boundary; the agent is a namespace inside it.** There is deliberately
no `agent:<id>` allowlist check, and the reason is structural rather than cautious:
`memory_bootstrap` calls `register_agent`, so registration is self-service. A caller could
register agent X and then be authorized for `agent:X` because it had just created it — a
check that reads as a boundary and is decoration. Making agent scopes real boundaries
requires privileged registration first, which #206 records as out of scope.

The tripwire at the bottom is the load-bearing test. Everything above it proves the guard
WORKS; only that one proves it is REACHED, and those are different claims with different
failure modes — the same distinction `test_mcp_entry_point_installs_the_guard.py` makes
for the deployed entry point.
"""

from __future__ import annotations

import ast
import collections
import pathlib

import pytest

from memotron import MemoryPrincipal, PrincipalRole
from memotron.mcp_auth import REQUIRE_IDENTITY_ENV, IdentityUnavailableError, ScopeNotAuthorizedError

PKG = pathlib.Path(__file__).resolve().parents[1] / "src" / "memotron" / "agent_memory"

#: Public methods that take `agent_id` and legitimately never reach a caller check.
#: Helpers, not operations — guarding them would be wrong, and `principal_for_agent`
#: specifically is what `_authorize` itself calls, so a guard there would recurse.
EXEMPT = frozenset({"agent_scope", "default_motive_for_agent_id", "principal_for_agent"})

GUARDS = ("_authorize", "_require_caller_tenant")


class _Platform:
    """The narrowest thing `_require_caller_tenant` needs. Calling the real constructor
    would drag in a client, a control plane and a store to test four branches."""

    def __init__(self, provider):
        self.tenant_id = "tenant-under-test"
        self._principal_provider = provider

    _require_caller_tenant = None  # bound below


from memotron.agent_memory._registry import AgentRegistryMixin  # noqa: E402

_Platform._require_caller_tenant = AgentRegistryMixin._require_caller_tenant


def _principal(tenant: str) -> MemoryPrincipal:
    return MemoryPrincipal(
        principal_id=f"p-{tenant}",
        tenant_id=tenant,
        allowed_scope_keys={f"tenant:{tenant}"},
        role=PrincipalRole.USER,
    )


@pytest.fixture(autouse=True)
def _flag_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test inherits the flag from the ambient environment, in either direction."""
    monkeypatch.delenv(REQUIRE_IDENTITY_ENV, raising=False)


@pytest.fixture
def guard_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REQUIRE_IDENTITY_ENV, "1")


class TestTheThreeStates:
    def test_with_the_flag_OFF_any_caller_is_permitted(self) -> None:
        """The rollout default: byte-for-byte the pre-#206 behaviour."""
        _Platform(lambda: _principal("someone-else"))._require_caller_tenant("op")

    def test_with_NO_PROVIDER_it_is_permitted_even_under_the_flag(self, guard_on: None) -> None:
        """An embedder with no notion of a caller — tests, the local platform, a
        notebook — is not a deployment and has nothing to authenticate. Without this,
        arming the flag anywhere would break every in-process user of the SDK."""
        _Platform(None)._require_caller_tenant("op")

    def test_the_principals_OWN_tenant_is_permitted(self, guard_on: None) -> None:
        """The positive control. A guard that refused everything would satisfy both
        refusal tests below and be an outage."""
        _Platform(lambda: _principal("tenant-under-test"))._require_caller_tenant("op")

    def test_ANOTHER_tenant_is_refused(self, guard_on: None) -> None:
        with pytest.raises(ScopeNotAuthorizedError, match="tenant-under-test"):
            _Platform(lambda: _principal("other-tenant"))._require_caller_tenant("op")

    def test_a_missing_principal_under_the_flag_FAILS_CLOSED(self, guard_on: None) -> None:
        """A provider that returns None while the flag is armed is a deployment fault —
        the middleware did not run — not an anonymous caller."""
        with pytest.raises(IdentityUnavailableError, match="middleware"):
            _Platform(lambda: None)._require_caller_tenant("op")

    def test_the_refusal_names_the_operation_and_both_tenants(self, guard_on: None) -> None:
        """An operator reading this in a log needs to know what was refused and why,
        without reading the source."""
        with pytest.raises(ScopeNotAuthorizedError) as excinfo:
            _Platform(lambda: _principal("other-tenant"))._require_caller_tenant("memory_search")
        message = str(excinfo.value)
        assert "memory_search" in message
        assert "tenant-under-test" in message and "other-tenant" in message


def _call_graph() -> tuple[dict[str, set[str]], dict[str, str]]:
    calls: dict[str, set[str]] = collections.defaultdict(set)
    public: dict[str, str] = {}
    for path in sorted(PKG.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and isinstance(inner.func.value, ast.Name)
                    and inner.func.value.id == "self"
                ):
                    calls[node.name].add(inner.func.attr)
            args = [a.arg for a in node.args.args] + [a.arg for a in node.args.kwonlyargs]
            if not node.name.startswith("_") and "agent_id" in args and path.name != "_protocol.py":
                public[node.name] = path.name
    return calls, public


def _reaches(name: str, calls: dict[str, set[str]], seen: set[str] | None = None) -> bool:
    seen = seen or set()
    if name in seen:
        return False
    seen.add(name)
    targets = calls.get(name, set())
    if targets & set(GUARDS):
        return True
    return any(_reaches(t, calls, seen) for t in targets)


def test_EVERY_public_agent_id_method_reaches_a_caller_check() -> None:
    """The tripwire, and the only test here that survives a refactor.

    Every other test proves the guard WORKS. This proves it is REACHED — including by
    methods that do not exist yet. The regression shape it catches: someone adds
    `memory_something(agent_id=...)` next month, it never calls a guard, every other test
    passes, and the surface has a hole nobody looks for.

    Modelled on `_CROSS_SCOPE_TOOLS` in `mcp_server`: the exemption list is explicit and
    small, so adding to it is a visible decision in a diff rather than an omission.
    """
    calls, public = _call_graph()
    unguarded = sorted(name for name in public if name not in EXEMPT and not _reaches(name, calls))
    assert not unguarded, (
        f"public method(s) taking agent_id with no caller check on any path: {unguarded}. "
        f"Either call `_require_caller_tenant` (or something that reaches it), or add the "
        f"name to EXEMPT in this file with a reason — a helper, not an operation."
    )


def test_the_exemption_list_is_not_quietly_absorbing_real_operations() -> None:
    """Guard the guard. EXEMPT is only safe while it holds helpers; if it grows to cover
    an operation, the tripwire above stops meaning anything and says nothing about it."""
    _, public = _call_graph()
    assert (set(public) | {"default_motive_for_agent_id"}) >= EXEMPT, (
        "EXEMPT names something that is not a public agent_id method any more — stale"
    )
    assert len(EXEMPT) == 3, (
        f"the exemption list changed size ({len(EXEMPT)}). Every entry must be a HELPER, "
        "not an operation: `agent_scope` constructs a scope, `default_motive_for_agent_id` "
        "is pure, and `principal_for_agent` is what `_authorize` itself calls, so guarding "
        "it would recurse. An operation added here is a hole, not an exemption."
    )
