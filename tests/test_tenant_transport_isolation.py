"""A tenant's LLM credential must never serve another tenant. (#158)

The defect this pins, reproduced 2026-09-02 at ``a1daaf4`` with no env key set:

    BEFORE anyone configures:               RuleBasedExtractionTransport
    AFTER tenant-alpha configures:          api_key='sk-ALPHA-SECRET'  # pragma: allowlist secret
    tenant-bravo has its own credential?    False
    transport that serves tenant-bravo:     api_key='sk-ALPHA-SECRET'   <-- alpha's  # pragma: allowlist secret
    after tenant-bravo configures:          api_key='sk-BRAVO-SECRET'  # pragma: allowlist secret
    alpha's own work now uses bravo's key:  True                        <-- and back

`configure_tenant_llm_credentials` called `set_runtime_transports`, which replaced
``self._extractor`` / ``self._dream_agent_transport`` on the SHARED client
(``client/_runtime.py:206-214``).  The MCP server holds one module-level client
(``mcp_server.py:93``), so whichever tenant configured last owned extraction and
dream-agent calls for everyone on that pod -- including tenants that configured
nothing.  Billing, upstream identity (ADR 0008 binds gateway identity to the key)
and the destination ``base_url`` all travelled with it.

The sealed credential was always stored correctly per tenant
(``storage/sqlite/_governance.py:758``).  The defect was in which transports the
RUNNING client used.
"""

from __future__ import annotations

import pathlib
import tempfile
from typing import Any

import pytest

from memotron import Memotron

ALPHA_KEY = "sk-ALPHA-SECRET"
BRAVO_KEY = "sk-BRAVO-SECRET"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Memotron:
    """A client with NO ambient credential, so every transport seen is one a test put there."""
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MEMOTRON_ALLOW_EPHEMERAL_KEK", "1")
    return Memotron(graph_path=str(pathlib.Path(tempfile.mkdtemp()) / "g.sqlite"))


def _key_for(client: Memotron, tenant_id: str | None) -> Any:
    """The api_key the client would actually use for *tenant_id*'s work."""
    extractor, _ = client.transports_for_tenant(tenant_id)
    return getattr(getattr(extractor, "_transport", None), "api_key", None)


def _base_url_for(client: Memotron, tenant_id: str | None) -> Any:
    extractor, _ = client.transports_for_tenant(tenant_id)
    return getattr(getattr(extractor, "_transport", None), "base_url", None)


def _configure(client: Memotron, tenant_id: str, key: str, host: str) -> None:
    client.configure_tenant_llm_credentials(
        tenant_id=tenant_id,
        provider="litellm",
        api_key=key,
        base_url=f"https://{host}/v1",
        model="claude-haiku-4-5",
    )


class TestOneTenantsKeyNeverServesAnother:
    def test_an_unconfigured_tenant_does_not_inherit_a_configured_one(self, client: Memotron) -> None:
        """THE LEAK, direction 1: bravo configured nothing and must not get alpha's key."""
        _configure(client, "tenant-alpha", ALPHA_KEY, "alpha.example")

        assert client.tenant_llm_credential_state("tenant-bravo") is None, "precondition: bravo has no credential"
        assert _key_for(client, "tenant-bravo") != ALPHA_KEY, (
            "tenant-bravo is being served by tenant-alpha's credential: its extraction would be "
            "billed to alpha's gateway key and arrive upstream authenticated as alpha (ADR 0008)."
        )
        assert _base_url_for(client, "tenant-bravo") != "https://alpha.example/v1", (
            "tenant-bravo's content would be sent to the endpoint alpha configured"
        )
        # Positive control: alpha DOES get alpha's key, so the resolver is not simply
        # returning the default for everyone.
        assert _key_for(client, "tenant-alpha") == ALPHA_KEY

    def test_configuring_a_second_tenant_does_not_steal_the_first(self, client: Memotron) -> None:
        """THE LEAK, direction 2: bravo configuring must not repoint alpha's work."""
        _configure(client, "tenant-alpha", ALPHA_KEY, "alpha.example")
        _configure(client, "tenant-bravo", BRAVO_KEY, "bravo.example")

        assert _key_for(client, "tenant-alpha") == ALPHA_KEY, (
            "configuring tenant-bravo repointed tenant-alpha's transports at bravo's key"
        )
        assert _key_for(client, "tenant-bravo") == BRAVO_KEY
        assert _base_url_for(client, "tenant-alpha") == "https://alpha.example/v1"
        assert _base_url_for(client, "tenant-bravo") == "https://bravo.example/v1"

    def test_order_does_not_matter(self, client: Memotron) -> None:
        """Whichever configured last must not win. Same assertion, reversed order."""
        _configure(client, "tenant-bravo", BRAVO_KEY, "bravo.example")
        _configure(client, "tenant-alpha", ALPHA_KEY, "alpha.example")

        assert _key_for(client, "tenant-bravo") == BRAVO_KEY
        assert _key_for(client, "tenant-alpha") == ALPHA_KEY

    def test_no_tenant_uses_the_process_default(self, client: Memotron) -> None:
        """A tenant-less operation keeps the construction-time transport, unchanged behaviour."""
        before = _key_for(client, None)
        _configure(client, "tenant-alpha", ALPHA_KEY, "alpha.example")
        assert _key_for(client, None) == before, "configuring a tenant changed what tenant-less operations use"

    def test_clearing_a_credential_stops_serving_it(self, client: Memotron) -> None:
        _configure(client, "tenant-alpha", ALPHA_KEY, "alpha.example")
        assert _key_for(client, "tenant-alpha") == ALPHA_KEY
        client.clear_tenant_llm_credentials("tenant-alpha")
        assert _key_for(client, "tenant-alpha") != ALPHA_KEY, "a cleared credential is still being used from cache"


class TestTheEngineHonoursIt:
    """Resolution is worthless if the dream engine still holds the shared extractor."""

    def test_engine_for_a_tenant_carries_that_tenants_transport(self, client: Memotron) -> None:
        _configure(client, "tenant-alpha", ALPHA_KEY, "alpha.example")
        _configure(client, "tenant-bravo", BRAVO_KEY, "bravo.example")

        engine_a = client.engine_for_tenant("tenant-alpha")
        engine_b = client.engine_for_tenant("tenant-bravo")

        key_a = getattr(getattr(engine_a._extractor, "_transport", None), "api_key", None)
        key_b = getattr(getattr(engine_b._extractor, "_transport", None), "api_key", None)
        assert key_a == ALPHA_KEY
        assert key_b == BRAVO_KEY
        assert key_a != key_b, "both engines share one extractor -- the leak survives at engine level"


class TestResolutionIsAlwaysFresh:
    """#158 F2 and #159. The first fix cached per tenant, per client object. Two defects
    followed and both are structural, not incidental:

    * the cache key had to agree with storage's own normalisation and did not -- storage
      strips, the cache did not, so one resolve under ` acme` pinned a credential past both
      rotation AND revocation, permanently;
    * a credential is per-GRAPH and a cache is per-CLIENT, so revocation never reached a
      second client (#159).

    Measured before removing it: the cache saved **0.018 ms** on a path that then makes
    ~100 ms of LLM network calls, and engine construction was 0.001 ms. It bought nothing
    and was the only thing that made a stale credential possible. There is no cache now, so
    these properties hold by construction rather than by invalidation being correct.
    """

    def test_a_whitespace_variant_never_serves_a_stale_credential(self, client: Memotron) -> None:
        _configure(client, "acme", "sk-ORIGINAL", "a.example")
        assert _key_for(client, " acme") == "sk-ORIGINAL", "precondition: the variant resolves"

        _configure(client, "acme", "sk-ROTATED", "a.example")
        assert _key_for(client, " acme") == "sk-ROTATED", "rotation not seen under ' acme'"

        client.clear_tenant_llm_credentials("acme")
        assert _key_for(client, " acme") != "sk-ROTATED", "revoked credential still served under ' acme'"
        assert _key_for(client, "acme") != "sk-ROTATED"

    def test_a_credential_configured_later_is_seen_immediately(self, client: Memotron) -> None:
        """A resolved miss must not be remembered, or a later configure is masked."""
        assert _key_for(client, "never-configured") is None
        _configure(client, "never-configured", "sk-LATE", "late.example")
        assert _key_for(client, "never-configured") == "sk-LATE"

    def test_resolution_holds_no_per_tenant_state(self, client: Memotron) -> None:
        """The structural guarantee: nothing accumulates, so nothing can go stale.

        A regression here means a cache was reintroduced -- at which point #159 and the
        normalisation defect both come back, and invalidation has to be correct everywhere
        instead of nowhere.
        """
        for i in range(50):
            _configure(client, f"t{i}", f"sk-{i}", "x.example")
            client.transports_for_tenant(f"t{i}")
        per_tenant = {
            name: value
            for name, value in vars(client).items()
            if isinstance(value, dict) and any(str(k).startswith("t") for k in value)
        }
        assert not per_tenant, f"per-tenant state accumulated on the client: {sorted(per_tenant)}"


class TestRevocationReachesEveryClientOverTheGraph:
    """#159. A credential is per-GRAPH; the first fix invalidated per CLIENT OBJECT.

    `local_platform.py:209-210` ships exactly this topology -- `handler.client` and
    `handler.runtime_client` are two Memotron objects over one store -- so a rotation
    or revocation through the admin surface left the runtime client serving the old key.
    `worker.py` is worse: a long-lived client that never calls configure or clear, so it
    served a revoked credential until the pod restarted.

    A regression from the #158 fix: the pre-fix global install did propagate.
    """

    @staticmethod
    def _two_clients_on_one_graph(monkeypatch: pytest.MonkeyPatch) -> tuple[Memotron, Memotron]:
        monkeypatch.delenv("LITELLM_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("MEMOTRON_ALLOW_EPHEMERAL_KEK", "1")
        path = str(pathlib.Path(tempfile.mkdtemp()) / "shared.sqlite")
        return Memotron(graph_path=path), Memotron(graph_path=path)

    def test_rotation_through_one_client_is_seen_by_the_other(self, monkeypatch: pytest.MonkeyPatch) -> None:
        admin, runtime = self._two_clients_on_one_graph(monkeypatch)
        _configure(admin, "acme", "sk-V1", "a.example")
        assert _key_for(runtime, "acme") == "sk-V1", "precondition: the runtime client sees V1"

        _configure(admin, "acme", "sk-V2", "a.example")
        assert _key_for(runtime, "acme") == "sk-V2", (
            "the runtime client is still serving the ROTATED-AWAY key. A credential is "
            "per-graph; invalidating per client object cannot reach a second one."
        )

    def test_revocation_through_one_client_is_seen_by_the_other(self, monkeypatch: pytest.MonkeyPatch) -> None:
        admin, runtime = self._two_clients_on_one_graph(monkeypatch)
        _configure(admin, "acme", "sk-V1", "a.example")
        assert _key_for(runtime, "acme") == "sk-V1"

        admin.clear_tenant_llm_credentials("acme")
        assert _key_for(runtime, "acme") != "sk-V1", (
            "a REVOKED credential is still being served by the second client on the same graph"
        )

    def test_a_storage_level_delete_is_seen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Erasure tooling, another process, or a DBA -- none of which call the client."""
        client, _ = self._two_clients_on_one_graph(monkeypatch)
        _configure(client, "acme", "sk-V1", "a.example")
        assert _key_for(client, "acme") == "sk-V1"

        client.graph.clear_tenant_llm_credentials("acme")  # bypasses the client entirely
        assert _key_for(client, "acme") != "sk-V1", (
            "a credential deleted directly in storage is still served from client state"
        )
