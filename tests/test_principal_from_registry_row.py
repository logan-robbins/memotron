"""The join between the per-key registry and `MemoryPrincipal`. (#126 / #137)

`principal_for_key_alias` returns storage's dict; every surface that authenticates a caller
needs a `MemoryPrincipal`. Nothing crossed that gap, so neither `mcp_server` nor
`admin_server` could derive a principal from an authenticated key even once the registry
was populated (#168 shipped the writer).

**Why so many failure tests for one small function.** Every one of them is a way this could
fail OPEN. The dangerous direction is not a raise -- a raise becomes a 401 and the caller is
refused. It is a *value*: a role that quietly reads as USER, or a `default_scope_key` that
quietly becomes `None` and so drops an entry from `effective_allowed_scope_keys`, widening
the allowlist rather than narrowing it. Those look like success at every layer above.

`test_a_round_trip_through_the_registry_is_lossless` is the end-to-end control: bind a row
with the real storage backend, read it back, convert it, and compare against the principal
that was intended. It is the only test here that would catch the adapter and the storage
layer disagreeing about a field name.
"""

from __future__ import annotations

import pytest

from memotron.config import MemoryPrincipal, PrincipalRole, principal_from_registry_row
from memotron.models import MemoryScope, ScopeKind
from memotron.storage.sqlite import SQLiteStorageBackend

FULL_ROW: dict[str, object] = {
    "key_alias": "dw-acme-01",
    "principal_id": "acme-ops",
    "tenant_id": "acme",
    "agent_id": "claude-code",
    "role": "admin",
    "default_scope_key": "tenant:acme",
    "allowed_scope_keys": ("tenant:acme", "customer:acme:wdw"),
}


class TestTheHappyPath:
    def test_every_field_crosses(self) -> None:
        principal = principal_from_registry_row(FULL_ROW)

        assert principal == MemoryPrincipal(
            principal_id="acme-ops",
            tenant_id="acme",
            agent_id="claude-code",
            role=PrincipalRole.ADMIN,
            default_scope=MemoryScope(kind=ScopeKind.TENANT, scope_id="acme"),
            allowed_scope_keys={"tenant:acme", "customer:acme:wdw"},
        )

    def test_the_default_scope_joins_the_effective_allowlist(self) -> None:
        """Why a dropped `default_scope` widens access: the model ADDS it to the allowlist.

        So "the scope failed to parse, call it None" is not a neutral degradation -- it
        removes an entry that authorization would otherwise have had to match.
        """
        principal = principal_from_registry_row({**FULL_ROW, "allowed_scope_keys": ()})
        assert principal.effective_allowed_scope_keys() == {"tenant:acme"}

    @pytest.mark.parametrize("absent", ["", None])
    def test_optional_fields_absent_is_not_the_same_as_unparseable(self, absent: str | None) -> None:
        """Legitimately optional -> None. Present-but-broken raises (see the class below)."""
        principal = principal_from_registry_row({**FULL_ROW, "agent_id": absent, "default_scope_key": absent})
        assert principal.agent_id is None
        assert principal.default_scope is None

    def test_a_missing_role_defaults_to_the_least_privileged(self) -> None:
        """Absent is allowed and means USER -- the storage column has that default too.
        A role that is PRESENT and unrecognised is a different case and raises."""
        row = {k: v for k, v in FULL_ROW.items() if k != "role"}
        assert principal_from_registry_row(row).role is PrincipalRole.USER


class TestEveryFailureRaisesRatherThanDegrading:
    """Each of these, if it returned a value instead, would fail OPEN."""

    def test_an_unrecognised_role_raises_rather_than_falling_back_to_user(self) -> None:
        """A silent fallback would read as a successful downgrade. It is really an
        unreadable record, and the same silence in the other direction is worse."""
        with pytest.raises(ValueError):
            principal_from_registry_row({**FULL_ROW, "role": "superuser"})

    @pytest.mark.parametrize(
        "broken",
        ["tenant", "tenant:", ":acme", "not-a-kind:acme", "  "],
        ids=["no-colon", "blank-id", "blank-kind", "unknown-kind", "whitespace-only"],
    )
    def test_an_unparseable_default_scope_key_raises_rather_than_becoming_none(self, broken: str) -> None:
        """THE ONE THAT MATTERS. `None` here silently shrinks the allowlist.

        Note `"  "` is in this list deliberately: whitespace-only is stripped to empty and
        therefore treated as ABSENT, not broken -- so it must NOT raise. It is here so the
        boundary between the two is pinned rather than assumed.
        """
        row = {**FULL_ROW, "default_scope_key": broken}
        if not broken.strip():
            assert principal_from_registry_row(row).default_scope is None
            return
        with pytest.raises(ValueError):
            principal_from_registry_row(row)

    @pytest.mark.parametrize("missing", ["principal_id", "tenant_id"])
    def test_a_row_missing_an_identity_field_raises(self, missing: str) -> None:
        """Storage declares both NOT NULL, so this should be unreachable -- which is
        exactly why it is worth pinning. A KeyError is a fine outcome; a principal with a
        blank tenant is not."""
        row = {k: v for k, v in FULL_ROW.items() if k != missing}
        with pytest.raises((KeyError, ValueError)):
            principal_from_registry_row(row)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_identity_field_raises(self, blank: str) -> None:
        """`MemoryPrincipal` rejects these itself; asserted here so the adapter is never
        "fixed" to paper over them."""
        with pytest.raises(ValueError):
            principal_from_registry_row({**FULL_ROW, "tenant_id": blank})


class TestScopeKeyRoundTrip:
    """`MemoryScope.from_key` is the exact inverse of `.key`, and this pins that."""

    @pytest.mark.parametrize("kind", list(ScopeKind))
    def test_key_then_from_key_is_the_identity(self, kind: ScopeKind) -> None:
        original = MemoryScope(kind=kind, scope_id="acme:with:colons")
        assert MemoryScope.from_key(original.key) == original

    def test_the_id_may_contain_colons_and_only_the_first_splits(self) -> None:
        """`customer:acme:wdw` is a real shape in this codebase -- the scope id is
        `acme:wdw`. A naive `split(":")` would lose the tail."""
        scope = MemoryScope.from_key("customer:acme:wdw")
        assert scope.kind is ScopeKind.CUSTOMER
        assert scope.scope_id == "acme:wdw"

    def test_the_admin_parser_still_delegates_here(self) -> None:
        """`parse_scope_key` kept its name and nine call sites; its body moved to the model.
        If these ever disagree, the admin surface and the auth path are parsing scopes
        differently, which is precisely the divergence this consolidation removed."""
        from memotron.admin_server._parsing import parse_scope_key

        assert parse_scope_key("customer:acme:wdw") == MemoryScope.from_key("customer:acme:wdw")
        with pytest.raises(ValueError, match="kind:id format"):
            parse_scope_key("nope")


class TestAgainstRealStorage:
    def test_a_round_trip_through_the_registry_is_lossless(self) -> None:
        """END-TO-END CONTROL. Everything above uses a hand-written dict, which cannot
        catch the adapter and storage disagreeing about a field NAME -- both sides would
        be self-consistent and the principal would silently lose a field.

        So: write with the real backend, read with the real lookup, convert, compare.
        """
        store = SQLiteStorageBackend(":memory:")
        try:
            store.bind_key_principal(
                key_alias="dw-acme-01",
                principal_id="acme-ops",
                tenant_id="acme",
                agent_id="claude-code",
                role="admin",
                default_scope_key="tenant:acme",
                allowed_scope_keys=("tenant:acme", "customer:acme:wdw"),
            )
            row = store.principal_for_key_alias("dw-acme-01")
        finally:
            store.close()

        assert row is not None
        principal = principal_from_registry_row(row)

        assert principal.principal_id == "acme-ops"
        assert principal.tenant_id == "acme"
        assert principal.agent_id == "claude-code"
        assert principal.role is PrincipalRole.ADMIN
        assert principal.default_scope is not None
        assert principal.default_scope.key == "tenant:acme"
        assert principal.effective_allowed_scope_keys() == {"tenant:acme", "customer:acme:wdw"}
