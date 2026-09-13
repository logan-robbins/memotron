"""Tests for the gateway identity self-lookup (#126 T3-1, per DW-026/027/028).

The properties worth pinning are the ones that fail *closed* — a resolver that
returns a plausible identity when it should have raised is the failure mode this
whole module exists to prevent, so most of these assert on refusal rather than success.
"""

from __future__ import annotations

import json
import math
from http.client import HTTPException
from typing import Any, Self
from urllib.error import HTTPError

import pytest

from memotron.gateway import GatewayRequestError
from memotron.gateway_identity import (
    DEFAULT_IDENTITY_CACHE_TTL_SECONDS,
    IDENTITY_CACHE_TTL_ENV,
    GatewayIdentity,
    IdentityCache,
    admin_base_url_from,
    identity_cache_ttl_from_env,
    key_fingerprint,
    resolve_gateway_identity,
)

DATA_PLANE = "https://latest.jedai-gateway.wdprapps.disney.com/v1"
ADMIN_PLANE = "https://latest.jedai-gateway-admin.wdprapps.disney.com"


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _urlopen_returning(payload: dict[str, Any], *, capture: list[Any] | None = None):
    def fake(request: Any, **_: Any) -> _FakeResponse:
        if capture is not None:
            capture.append(request)
        return _FakeResponse(payload)

    return fake


# --- admin host derivation -------------------------------------------------------


def test_admin_url_is_derived_from_the_data_plane_url() -> None:
    """The data plane 403s management routes, so the lookup must be redirected."""
    assert admin_base_url_from(DATA_PLANE) == ADMIN_PLANE


def test_admin_url_derivation_is_idempotent() -> None:
    assert admin_base_url_from(ADMIN_PLANE) == ADMIN_PLANE


def test_admin_url_keeps_an_explicit_port() -> None:
    assert (
        admin_base_url_from("https://latest.jedai-gateway.wdprapps.disney.com:8443/v1")
        == "https://latest.jedai-gateway-admin.wdprapps.disney.com:8443"
    )


def test_an_unrecognised_host_shape_refuses_rather_than_guessing() -> None:
    """Guessing which label is the service could send a credential to the wrong host."""
    with pytest.raises(GatewayRequestError, match="MEMOTRON_GATEWAY_ADMIN_BASE_URL"):
        admin_base_url_from("https://gw.example.com/v1")


def test_an_explicit_admin_url_overrides_the_derivation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMOTRON_GATEWAY_ADMIN_BASE_URL", "https://admin.internal/")
    assert admin_base_url_from("https://gw.example.com/v1") == "https://admin.internal"


@pytest.mark.parametrize("bad", ["", "   ", "not-a-url", "/v1", "localhost"])
def test_admin_url_refuses_anything_it_cannot_derive_from(bad: str) -> None:
    """Fail closed: a URL we cannot rewrite must raise, never fall back to the input."""
    with pytest.raises(GatewayRequestError):
        admin_base_url_from(bad)


# --- the self-lookup -------------------------------------------------------------


def test_resolve_reads_the_key_record_and_targets_the_admin_plane() -> None:
    captured: list[Any] = []
    identity = resolve_gateway_identity(
        "sk-abc",
        base_url=DATA_PLANE,
        urlopen=_urlopen_returning(
            {"info": {"key_alias": "svc-alpha", "team_id": "TEAM_DX0021", "user_id": "u-1"}},
            capture=captured,
        ),
    )
    assert identity == GatewayIdentity(key_alias="svc-alpha", team_id="TEAM_DX0021", user_id="u-1")
    assert captured[0].full_url == f"{ADMIN_PLANE}/key/info"
    assert captured[0].get_header("Authorization") == "Bearer sk-abc"


def test_resolve_accepts_a_flat_record_as_well_as_an_info_envelope() -> None:
    identity = resolve_gateway_identity(
        "sk-abc", base_url=DATA_PLANE, urlopen=_urlopen_returning({"key_alias": "flat"})
    )
    assert identity.key_alias == "flat"


def test_absent_and_blank_fields_become_none_rather_than_empty_strings() -> None:
    """A blank alias must not read as a registry key that could match something."""
    identity = resolve_gateway_identity(
        "sk-abc",
        base_url=DATA_PLANE,
        urlopen=_urlopen_returning({"info": {"key_alias": "   ", "team_id": None}}),
    )
    assert identity.key_alias is None
    assert identity.team_id is None
    assert identity.is_anonymous


def test_an_admin_minted_personal_key_is_anonymous_not_an_error() -> None:
    """DW-027 observed team_id and user_id both None on such keys. It authenticates;
    it just carries nothing to authorize against."""
    identity = resolve_gateway_identity("sk-abc", base_url=DATA_PLANE, urlopen=_urlopen_returning({"info": {}}))
    assert identity.is_anonymous


@pytest.mark.parametrize("blank", ["", "   "])
def test_an_empty_key_never_reaches_the_gateway(blank: str) -> None:
    def exploding(*_: Any, **__: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("the gateway was called with an empty key")

    with pytest.raises(GatewayRequestError):
        resolve_gateway_identity(blank, base_url=DATA_PLANE, urlopen=exploding)


def test_a_refusal_raises_rather_than_returning_an_anonymous_identity() -> None:
    """The failure this module exists to prevent: a 401 must not become is_anonymous."""

    def refusing(*_: Any, **__: Any) -> Any:
        raise HTTPError(f"{ADMIN_PLANE}/key/info", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    with pytest.raises(GatewayRequestError):
        resolve_gateway_identity("sk-bad", base_url=DATA_PLANE, urlopen=refusing)


def test_a_transport_fault_raises() -> None:
    def dropping(*_: Any, **__: Any) -> Any:
        raise HTTPException("connection dropped mid-response")

    with pytest.raises(GatewayRequestError):
        resolve_gateway_identity("sk-abc", base_url=DATA_PLANE, urlopen=dropping)


def test_a_record_that_is_not_an_object_raises() -> None:
    with pytest.raises(GatewayRequestError):
        resolve_gateway_identity("sk-abc", base_url=DATA_PLANE, urlopen=_urlopen_returning({"info": "nonsense"}))


# --- fingerprinting --------------------------------------------------------------


def test_fingerprint_is_stable_and_does_not_contain_the_key() -> None:
    assert key_fingerprint("sk-secret") == key_fingerprint("  sk-secret  ")
    assert "sk-secret" not in key_fingerprint("sk-secret")
    assert key_fingerprint("sk-a") != key_fingerprint("sk-b")


# --- the cache -------------------------------------------------------------------


def test_cache_serves_a_hit_without_calling_the_resolver() -> None:
    clock = [1000.0]
    cache = IdentityCache(ttl_seconds=60, now=lambda: clock[0])
    calls = 0

    def resolver(_: str) -> GatewayIdentity:
        nonlocal calls
        calls += 1
        return GatewayIdentity(key_alias="a", team_id="t", user_id=None)

    assert cache.resolve("sk-x", resolver).key_alias == "a"
    assert cache.resolve("sk-x", resolver).key_alias == "a"
    assert calls == 1


def test_cache_expires_and_re_resolves() -> None:
    """The TTL is the revocation window; an entry must not outlive it."""
    clock = [1000.0]
    cache = IdentityCache(ttl_seconds=60, now=lambda: clock[0])
    cache.put("sk-x", GatewayIdentity(key_alias="a", team_id=None, user_id=None))
    clock[0] += 59.9
    assert cache.get("sk-x") is not None
    clock[0] += 0.2
    assert cache.get("sk-x") is None


def test_a_failed_resolution_is_not_cached() -> None:
    """A gateway outage must not pin a caller to a refusal for a whole TTL."""
    cache = IdentityCache(ttl_seconds=60)
    attempts = 0

    def failing(_: str) -> GatewayIdentity:
        nonlocal attempts
        attempts += 1
        raise GatewayRequestError("gateway down", attempts=3, retryable=True)

    for _ in range(3):
        with pytest.raises(GatewayRequestError):
            cache.resolve("sk-x", failing)
    assert attempts == 3
    assert cache.get("sk-x") is None


def test_distinct_keys_do_not_share_a_cache_entry() -> None:
    cache = IdentityCache(ttl_seconds=60)
    cache.put("sk-a", GatewayIdentity(key_alias="a", team_id=None, user_id=None))
    assert cache.get("sk-b") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", DEFAULT_IDENTITY_CACHE_TTL_SECONDS),
        ("30", 30.0),
        ("0", 0.0),
        ("-5", DEFAULT_IDENTITY_CACHE_TTL_SECONDS),
        ("abc", DEFAULT_IDENTITY_CACHE_TTL_SECONDS),
    ],
)
def test_ttl_from_env_rejects_nonsense_rather_than_disabling_the_cache(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    monkeypatch.setenv("MEMOTRON_IDENTITY_CACHE_TTL", raw)
    assert identity_cache_ttl_from_env() == expected


class TestTheCacheIsBoundedAndItsTTLIsFinite:
    """#126 put this cache on the live request path, which made two latent things reachable.

    Both fail in the PERMISSIVE direction, which is why they are worth a test rather than a
    comment: an infinite TTL means a revoked key works forever, and an unbounded cache means
    a caller who can mint keys can grow it until the pod dies.
    """

    @pytest.mark.parametrize("value", ["inf", "-inf", "Infinity", "nan", "NaN"])
    def test_a_non_finite_TTL_falls_back_to_the_default(self, monkeypatch, value: str) -> None:
        """`float("inf")` parses and is `>= 0`, so it used to be returned verbatim -- a
        cached identity that never expires and a revocation that never lands.

        `nan` is worse: every comparison against it is False, so the expiry branch in `get`
        would never fire either, and nothing would look wrong anywhere.
        """
        monkeypatch.setenv(IDENTITY_CACHE_TTL_ENV, value)
        ttl = identity_cache_ttl_from_env()
        assert ttl == DEFAULT_IDENTITY_CACHE_TTL_SECONDS
        assert math.isfinite(ttl)

    def test_a_normal_TTL_is_still_honoured(self, monkeypatch) -> None:
        """The positive control. A validator that rejected everything would pass the test
        above and quietly pin every deployment to the default."""
        monkeypatch.setenv(IDENTITY_CACHE_TTL_ENV, "12.5")
        assert identity_cache_ttl_from_env() == 12.5

    def test_zero_is_allowed_because_it_means_do_not_cache(self, monkeypatch) -> None:
        monkeypatch.setenv(IDENTITY_CACHE_TTL_ENV, "0")
        assert identity_cache_ttl_from_env() == 0.0

    def test_the_cache_does_not_grow_without_bound(self) -> None:
        """Distinct LIVE keys, none expired -- the case lazy expiry cannot contain.

        `/key/generate` is not admin-only (DW-027), so minting keys in a loop is something a
        caller can actually do.
        """
        cache = IdentityCache(ttl_seconds=3600, max_entries=64)
        for i in range(500):
            cache.put(f"sk-key-{i}", GatewayIdentity(key_alias=f"a-{i}", team_id=None, user_id=None))
        assert len(cache._entries) <= 64

    def test_eviction_keeps_the_cache_USEFUL(self, monkeypatch) -> None:
        """The control that stops "bounded" from being satisfied by a cache that holds
        nothing. The most recently written key must still be a hit."""
        cache = IdentityCache(ttl_seconds=3600, max_entries=8)
        for i in range(50):
            cache.put(f"sk-key-{i}", GatewayIdentity(key_alias=f"a-{i}", team_id=None, user_id=None))
        assert cache.get("sk-key-49") is not None
        assert cache.get("sk-key-49").key_alias == "a-49"

    def test_expired_entries_are_preferred_for_eviction(self) -> None:
        """Sweeping expired entries first is what keeps the live ones alive; evicting the
        oldest half is only the fallback for when nothing has expired."""
        clock = {"t": 0.0}
        cache = IdentityCache(ttl_seconds=10, now=lambda: clock["t"], max_entries=4)
        for i in range(4):
            cache.put(f"stale-{i}", GatewayIdentity(key_alias=f"s-{i}", team_id=None, user_id=None))
        clock["t"] = 100.0  # every entry above is now expired
        cache.put("fresh", GatewayIdentity(key_alias="fresh", team_id=None, user_id=None))
        assert cache.get("fresh") is not None
        assert cache.get("stale-0") is None
