"""The MCP servers must accept the Host header of the environment they are deployed to.

The defect this pins, measured 2026-09-02 against the deployed `latest` image
(`0.1.0-a1daaf4`) and reproduced locally on the identical image:

    POST https://latest.jedai-memotron.wdprapps.disney.com/mcp -> 421 "Invalid Host header"
    GET  https://latest.jedai-memotron.wdprapps.disney.com/health -> 200

Every pod reported Ready and the GCLB backend was healthy the whole time, because
``/health`` is not behind the transport-security middleware and returns an
unconditional constant (``mcp_server.py`` health handler). So the entire product API
was unreachable through its own ingress while every signal said the service was fine.

Original cause, under MCP SDK >= 1.29: ``FastMCP(...)`` resolved DNS-rebinding
protection at CONSTRUCTION from its ``host`` argument, defaulting to ``127.0.0.1`` with
``allowed_hosts=['127.0.0.1:*', 'localhost:*', '[::1]:*']``.  The entry point then set
``mcp.settings.host = "0.0.0.0"`` afterwards -- binding publicly -- and the security
settings were never re-evaluated.  Bind address and allowlist disagreed by construction.

**Under FastMCP 4 the failure mode is the opposite one, and worse.** There is no
construction-time allowlist to go stale; instead ``http_host_origin_protection``
defaults to ``False``, so passing ``allowed_hosts`` *without* it installs no guard at
all and configures no one. Measured on fastmcp 4.0.3::

    allowed_hosts only ................ bogus Host -> 200   ACCEPTED
    protection=True + allowed_hosts ... bogus Host -> 421   refused
    nothing set (the default) ......... bogus Host -> 200   ACCEPTED

So these tests assert the *policy*, not the framework: protection stays ON, unset still
means localhost-only, and a declared host is accepted only once declared.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from memotron.runtime import build_mcp_http_app, mcp_host_security

LATEST_HOST = "latest.jedai-memotron.wdprapps.disney.com"


def _allows(security, host: str) -> bool:
    """Would the real guard accept `host` under `security`?

    This DELEGATES to fastmcp's own matcher instead of reimplementing it. The previous
    version was a hand-written mirror, and it had silently stopped mirroring: fastmcp's
    `_normalize_host` strips the port from BOTH the pattern and the header, so bare
    `Host: localhost` matches the `localhost:*` pattern -- while the mirror required the
    colon and reported it refused. Measured divergence before this fix::

        host              mirror said   real guard
        localhost         False         True     <-- opposite
        127.0.0.1         False         True     <-- opposite
        evil.example.com  False         False

    A mirror that is STRICTER than the thing it models makes every negative assertion
    pass for the wrong reason. `DEFAULT_HOSTS` is included because
    `HostOriginGuardMiddleware._allowed_hosts_for_scope` prepends it on every request;
    omitting it here is exactly how the mirror drifted. The per-connection entry that
    method also appends is NOT modelled -- see `TestTheAllowlistIsNotAnAccessControl`.
    """
    from fastmcp.server.http import DEFAULT_HOSTS, _host_matches

    if not security.protection:
        return True
    return _host_matches(host, tuple(DEFAULT_HOSTS) + security.allowed_hosts)


class TestTheAllowlistIsNotAnAccessControl:
    """What the Host allowlist does NOT do. Pinned because a comment claimed otherwise.

    `Host` is chosen by the caller. Anyone who can open a TCP connection to the port can
    send whatever they like, and these values are accepted under EVERY configuration
    including the fail-closed default:

        prod shape (allowlist unset), measured against the built image:
            Host: jedai-memotron.wdprapps.disney.com  -> 421
            Host: evil.example.com                       -> 421
            Host: localhost                              -> 200
            Host: 127.0.0.1                              -> 200
            Host: [::1]:8000                             -> 200

    This is **not fixable here**. `HostOriginGuardMiddleware._allowed_hosts_for_scope`
    starts with `allowed_hosts = list(DEFAULT_HOSTS)` -- ('127.0.0.1', 'localhost', '::1')
    -- on every request, before anything this repo passes. Deleting `_LOCALHOST_HOSTS`
    from `runtime.py` would change nothing.

    So the guard is a **DNS-rebinding** control, which is what its upstream name says it
    is: it stops a browser on an attacker's page from reaching a server the browser can
    route to. It is not a network access control, and no deployment argument should rest
    on it alone. Authentication (`MEMOTRON_REQUIRE_GATEWAY_IDENTITY`) is what bounds
    who may call the tools.
    """

    @pytest.mark.parametrize("host", ["localhost", "localhost:8000", "127.0.0.1", "[::1]:8000"])
    def test_loopback_hosts_are_accepted_even_when_nothing_is_declared(
        self, host: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MEMOTRON_MCP_ALLOWED_HOSTS", raising=False)
        assert _allows(mcp_host_security(), host), (
            f"{host!r} refused. If this ever starts failing the guard became stricter "
            "upstream -- check whether port-forward and in-cluster probing still work "
            "before celebrating."
        )

    def test_and_that_is_upstreams_doing_not_ours(self) -> None:
        """The control for the claim above: it is FastMCP's constant, not our config."""
        from fastmcp.server.http import DEFAULT_HOSTS

        assert "localhost" in DEFAULT_HOSTS and "127.0.0.1" in DEFAULT_HOSTS


class TestDefaultsAreUnchanged:
    def test_unset_keeps_localhost_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env var => localhost only. A dev box must not silently open up.

        This is the property stage, load and prod all depend on (#237, #252), and it is
        the OPPOSITE of FastMCP 4's own default, which is to accept anything. This
        function is what stands between three environments and an open MCP surface.
        """
        monkeypatch.delenv("MEMOTRON_MCP_ALLOWED_HOSTS", raising=False)
        security = mcp_host_security()

        assert security.protection is True
        assert _allows(security, "127.0.0.1:8000")
        assert _allows(security, "localhost:8000")
        # The control: the thing the defect was. Unset must still refuse it.
        assert not _allows(security, LATEST_HOST)

    def test_blank_is_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMOTRON_MCP_ALLOWED_HOSTS", "   ")
        security = mcp_host_security()
        assert security.protection is True
        assert not _allows(security, LATEST_HOST)


class TestDeclaredHostsAreAccepted:
    def test_the_deployed_hostname_is_allowed_once_declared(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """THE REGRESSION. Without the fix this is the 421; with it, 200."""
        monkeypatch.setenv("MEMOTRON_MCP_ALLOWED_HOSTS", LATEST_HOST)
        security = mcp_host_security()

        assert security.protection is True, "declaring a host must NOT be a backdoor to switching host protection off"
        assert _allows(security, LATEST_HOST)
        # With a port, as a proxy may present it.
        assert _allows(security, f"{LATEST_HOST}:443")
        # Positive control: an undeclared host is still refused, so the allowlist
        # is an allowlist and not an accidental "allow everything".
        assert not _allows(security, "evil.example.com")

    def test_localhost_survives_alongside_a_declared_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A deployed allowlist must not break port-forward or in-cluster probing."""
        monkeypatch.setenv("MEMOTRON_MCP_ALLOWED_HOSTS", LATEST_HOST)
        security = mcp_host_security()
        assert _allows(security, "127.0.0.1:8000")
        assert _allows(security, "localhost:8000")

    def test_multiple_hosts_and_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMOTRON_MCP_ALLOWED_HOSTS", f" {LATEST_HOST} , jedai-memotron-api:8000 ,,")
        security = mcp_host_security()
        assert _allows(security, LATEST_HOST)
        assert _allows(security, "jedai-memotron-api:8000")
        assert not _allows(security, "evil.example.com")

    def test_origins_are_derived_for_declared_hosts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A browser client sends Origin; an allowlist covering Host but not Origin still 421s."""
        monkeypatch.setenv("MEMOTRON_MCP_ALLOWED_HOSTS", LATEST_HOST)
        assert f"https://{LATEST_HOST}" in mcp_host_security().allowed_origins


class TestTheExplicitEscapeHatch:
    def test_star_disables_protection_and_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`*` is the only way to turn the protection off, and it must be deliberate."""
        monkeypatch.setenv("MEMOTRON_MCP_ALLOWED_HOSTS", "*")
        security = mcp_host_security()
        assert security.protection is False
        assert _allows(security, "anything.example.com")


class TestTheServersActuallyUseIt:
    """The helper being right is worthless if the served app does not apply it.

    NOTE ON WHAT THIS ASSERTS, because the obvious version of this test is useless.
    Asserting that protection is enabled PASSED ON THE BROKEN CODE under the old SDK,
    which auto-enabled exactly that with a localhost-only allowlist precisely because
    the default host was 127.0.0.1. A check whose passing condition is satisfied by the
    defect it should surface is this repository's most-repeated bug, and the first draft
    of this file contained one.

    So these tests do not inspect settings at all. They build the real ASGI app the way
    `serve_mcp_http` builds it, send it a forged `Host` header, and read the real status
    code. 421 or not-421 cannot be satisfied by the defect.

    Measured through exactly this harness, which is what the class is defending::

        nothing set (framework default) : good=200 evil=200   <- open
        allowed_hosts ONLY              : good=200 evil=200   <- open, and looks configured
        protection=True + allowed_hosts : good=200 evil=421   <- correct

    The middle row is why `mcp_host_security.protection` exists and why it is never
    silently False: an allowlist without the flag installs no guard at all.
    """

    #: The connection target. Deliberately NOT any host under test, and deliberately
    #: not a loopback name: `HostOriginGuardMiddleware._allowed_hosts_for_scope`
    #: (`fastmcp/server/http.py`) appends `scope["server"][0]` to the allowlist, so a
    #: request whose connection target IS the forged host allowlists itself. The first
    #: draft of this file set the host via `base_url`, which sets both at once -- every
    #: forged-Host case returned 200 and the tests would have certified an absent guard.
    CONNECTION_TARGET = "probe-target"

    @staticmethod
    def _status(app, host: str) -> int:
        """POST /mcp at `app` with `Host: host`, returning the final status code.

        The `Host` header and the connection target are set SEPARATELY and must stay
        that way -- see `CONNECTION_TARGET`. Under `ASGITransport` there is no socket,
        so an explicit `Host` header cannot misdirect the request anywhere.

        `follow_redirects=True` is required, not incidental: `/mcp` answers 307 to
        `/mcp/`, and the redirect is issued by the router BEFORE the host guard runs.
        A client that does not follow it reads 307 for every case -- so a forged Host
        would read as "not 421" and each of these tests would pass on an absent guard.
        That redirect defeated four separate probes before it was noticed.
        """
        with TestClient(
            app,
            base_url=f"http://{TestTheServersActuallyUseIt.CONNECTION_TARGET}",
            follow_redirects=True,
        ) as client:
            response = client.post(
                "/mcp",
                headers={
                    "Host": host,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
        return response.status_code

    @pytest.mark.parametrize("module_name", ["memotron.mcp_server", "memotron.agent_memory_mcp"])
    def test_a_forged_host_is_refused_when_nothing_is_declared(
        self, module_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset means localhost-only, ON THE SERVED APP -- not just in the helper.

        This is the test that would have caught the FastMCP 4 trap. Passing
        `allowed_hosts` without `host_origin_protection=True` installs no guard, so this
        would read 200 rather than 421 while every settings-level assertion stayed green.
        """
        import importlib

        monkeypatch.delenv("MEMOTRON_MCP_ALLOWED_HOSTS", raising=False)
        module = importlib.import_module(module_name)
        app = build_mcp_http_app(module.mcp)

        assert self._status(app, LATEST_HOST) == 421, (
            f"{module_name} ACCEPTED an undeclared Host with nothing declared. "
            "The allowlist is not being applied to the served app."
        )
        # Positive control: the app is reachable at all, so the 421 above is the host
        # guard refusing and not a broken request that would fail for any Host.
        assert self._status(app, "127.0.0.1:8000") != 421

    @pytest.mark.parametrize("module_name", ["memotron.mcp_server", "memotron.agent_memory_mcp"])
    def test_declared_host_is_accepted_by_the_served_app(
        self, module_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE 421. Declared host must be accepted, undeclared must still be refused."""
        import importlib

        monkeypatch.setenv("MEMOTRON_MCP_ALLOWED_HOSTS", LATEST_HOST)
        module = importlib.import_module(module_name)
        app = build_mcp_http_app(module.mcp)

        assert self._status(app, LATEST_HOST) != 421, (
            f"{module_name} REFUSES its own declared host {LATEST_HOST!r}. This is the "
            "421 that made every pod report Ready with the product API unreachable."
        )
        # Positive control: the allowlist is an allowlist, not an accidental open door.
        assert self._status(app, "evil.example.com") == 421
        # And localhost must survive, or port-forward and probes break.
        assert self._status(app, "127.0.0.1:8000") != 421


class TestLocalDevGetsTheSamePolicy:
    """`local_platform` must serve through the shared helper, not `mcp.run()`.

    Not a style rule. `mcp.run()` applies FastMCP 4's own default, which is
    `http_host_origin_protection=False` -- no guard at all -- while `serve_mcp_http`
    applies this repo's fail-closed policy. So a local server started the other way is
    strictly more open than the deployed ones, and its argument parser accepts
    `--mcp-host 0.0.0.0`.

    This is the third distinct place the bug has appeared. It keeps coming back because
    each serving path looks self-contained and reasonable on its own, which is the whole
    argument for having exactly one of them.

    An AST assertion, matching the two entry-point files: the alternative is starting a
    real local platform (admin UI, maintenance thread, HTTP server) to observe a config
    that is decided in one line.
    """

    def test_local_platform_does_not_call_mcp_run(self) -> None:
        import ast
        import pathlib

        source = pathlib.Path(__file__).resolve().parents[1] / "src" / "memotron" / "local_platform.py"
        tree = ast.parse(source.read_text())
        runs = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "mcp"
        ]
        assert not runs, (
            f"mcp.run() at line(s) {runs} in local_platform.py. That bypasses "
            "serve_mcp_http, so the local server accepts ANY Host header while the "
            "deployed ones are localhost-only by default."
        )

    def test_local_platform_serves_through_the_shared_helper(self) -> None:
        """The positive half. Forbidding `mcp.run()` alone would pass on a file that
        hand-rolls its own uvicorn call, which is how this diverged the first time."""
        import ast
        import pathlib

        source = pathlib.Path(__file__).resolve().parents[1] / "src" / "memotron" / "local_platform.py"
        tree = ast.parse(source.read_text())
        calls = {
            node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "serve_mcp_http" in calls, (
            "local_platform.py no longer calls serve_mcp_http; whatever replaced it does not carry the Host allowlist."
        )


class TestHealthIsReachableForTheKubeletProbe:
    """`/health` must answer 200 for ANY Host. `/mcp` must not. Both, or the deploy dies.

    FastMCP 4 installs `HostOriginGuardMiddleware` at position 0 of the Starlette stack,
    so it wraps every route including custom ones. The old SDK applied transport security
    around the `/mcp` mount only -- which is why the recorded outage shows `/mcp` -> 421
    and `/health` -> 200 on the same pod, at the same moment.

    Inheriting FastMCP's topology would therefore have been a NEW outage on top of the
    fix for the old one: `httpGet` probes with no `host:` -- which all six chart probes
    are -- send the **pod IP** as the Host header, no allowlist contains a pod IP, so
    every liveness and readiness probe returns 421 and no pod ever becomes Ready.

    Measured in the built image before the fix::

        Host: 10.154.184.212:8000  /health -> 421
        Host: dw.example.internal  /health -> 200

    No suite caught that. It is invisible to every lane in `check.sh`, because all of
    them read the source tree and none of them starts the product and sends it a request
    the way a kubelet would. It was found by running the container.

    The pair of assertions is the point. Only checking `/health` would pass on a server
    with no guard at all -- which is exactly what `mcp-forge` ships today
    (`mcp.http_app(stateless_http=True)`, no allowlist anywhere). That is survivable
    there because those servers declare no ingress; both of ours do.
    """

    ALLOWED = "dw.example.internal"
    POD_IP_HOST = "10.154.184.212:8000"

    @pytest.fixture
    def app(self, monkeypatch: pytest.MonkeyPatch):
        import importlib

        monkeypatch.setenv("MEMOTRON_MCP_ALLOWED_HOSTS", self.ALLOWED)
        return build_mcp_http_app(importlib.import_module("memotron.mcp_server").mcp)

    @staticmethod
    def _health(app, host: str) -> int:
        with TestClient(
            app,
            base_url=f"http://{TestTheServersActuallyUseIt.CONNECTION_TARGET}",
            follow_redirects=True,
        ) as client:
            return client.get("/health", headers={"Host": host}).status_code

    def test_the_kubelet_probes_pod_ip_host_is_answered(self, app) -> None:
        """The deploy-breaking case. kubelet sends the pod IP; no allowlist has it."""
        assert self._health(app, self.POD_IP_HOST) == 200, (
            "/health refused the pod IP as a Host. Every chart probe is an httpGet with "
            "no host: set, so kubelet sends the pod IP -- all six probes fail and no pod "
            "reaches Ready. /health must be served OUTSIDE the Host guard."
        )

    @pytest.mark.parametrize("path", ["/health", "/health/"])
    def test_both_spellings_answer(self, app, path: str) -> None:
        """A health endpoint that refuses on punctuation is a trap.

        `/health/` used to 421 while `/health` returned 200: Starlette matches the exact
        path and `Mount("/")` matches everything, so the trailing-slash form never reached
        the hoisted route. Exempting it in `StrictHostGuard` alone did NOT fix it and made
        things worse -- the exemption list claimed a path was open while the inner guard
        still refused it. Both spellings are now served from the same endpoint.
        """
        with TestClient(
            app,
            base_url=f"http://{TestTheServersActuallyUseIt.CONNECTION_TARGET}",
            follow_redirects=True,
        ) as client:
            assert client.get(path, headers={"Host": "evil.example.com"}).status_code == 200

    def test_health_answers_any_host(self, app) -> None:
        """It returns a constant and reads nothing from the request; there is nothing
        a forged Host could reach. This was its behaviour before FastMCP 4 too."""
        assert self._health(app, "evil.example.com") == 200
        assert self._health(app, self.ALLOWED) == 200

    def test_but_mcp_is_STILL_guarded(self, app) -> None:
        """The other half, and the reason this class is two tests and not one.

        Hoisting /health must not hoist anything else. Without this assertion the class
        passes on a server with the guard removed entirely -- the mcp-forge shape.
        """
        assert TestTheServersActuallyUseIt._status(app, "evil.example.com") == 421
        assert TestTheServersActuallyUseIt._status(app, self.ALLOWED) != 421


class TestTheConnectionAddressIsNotAllowlisted:
    """The regression the FastMCP 4 move introduced, and `StrictHostGuard` closes.

    `HostOriginGuardMiddleware._allowed_hosts_for_scope` appends `scope["server"][0]` --
    under uvicorn, the local socket address of the accepted connection, i.e. the **pod
    IP** for a container bound to `0.0.0.0`. curl's default `Host` when dialling an IP is
    that IP, so `curl http://<pod-ip>:8000/mcp` self-allowlisted and returned 200.

    mcp 1.29.0, the SDK this replaced, had no such fallback -- `transport_security.py`
    `_validate_host` consults `settings.allowed_hosts` and nothing else (read from the
    1.29.0 wheel). Same request, 421 before and 200 after: a widening, not a carry-over.

    It mattered because `.helm/values-prod.yaml` rests prod's whole safety argument on
    that request being refused, and `requireGatewayIdentity` is `false` everywhere except
    latest -- so it was the 39-tool surface, unauthenticated, to anything that could route
    to the pod.

    Measured in the built image, prod shape (allowlist unset), before and after::

        Host: 172.17.0.2:8000   /mcp  200  ->  421
        Host: localhost         /mcp  200  ->  200   (unchanged; see below)

    Loopback still passes. That is deliberate and pre-existing -- `_LOCALHOST_HOSTS` was
    in the old SDK's allowlist too, and port-forward and in-cluster probes rely on it.
    See `TestTheAllowlistIsNotAnAccessControl`.
    """

    def test_a_bogus_connection_address_style_host_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TestClient cannot reproduce the real self-allowlisting -- its `scope["server"]`
        is the base_url host, never a routable address. So this asserts the property that
        IS reachable here: an RFC1918 `Host` that nobody declared must be refused."""
        import importlib

        monkeypatch.setenv("MEMOTRON_MCP_ALLOWED_HOSTS", "dw.example.internal")
        app = build_mcp_http_app(importlib.import_module("memotron.mcp_server").mcp)
        assert TestTheServersActuallyUseIt._status(app, "10.154.184.212:8000") == 421
        assert TestTheServersActuallyUseIt._status(app, "172.17.0.2:8000") == 421
        # Control: the declared host still works, so the 421s are the allowlist and not
        # a server that refuses everything.
        assert TestTheServersActuallyUseIt._status(app, "dw.example.internal") != 421

    def test_the_strict_guard_is_actually_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Structural companion: FastMCP's guard alone would accept the connection address,
        so the outermost object must be ours."""
        import importlib

        from memotron.runtime import StrictHostGuard

        monkeypatch.setenv("MEMOTRON_MCP_ALLOWED_HOSTS", "dw.example.internal")
        app = build_mcp_http_app(importlib.import_module("memotron.mcp_server").mcp)
        assert isinstance(app, StrictHostGuard), (
            "the outermost app is not StrictHostGuard, so FastMCP's guard decides Host "
            "and the connection address is allowlisted again"
        )


class TestTheWildcardEscapeHatchCannotBeHalfApplied:
    """`"*,foo"` used to report protection ON while accepting everything.

    `_host_matches` short-circuits on `pattern == "*"`, so a `*` copied faithfully into
    `allowed_hosts` matched every Host -- while `McpHostSecurity.protection` said `True`.
    A control that misreports its own state is worse than one that is off, because the
    misreport is what a reader acts on.
    """

    @pytest.mark.parametrize("value", ["*", "*,foo", "foo,*", " foo , * , bar "])
    def test_any_star_element_disables_protection_explicitly(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMOTRON_MCP_ALLOWED_HOSTS", value)
        security = mcp_host_security()
        assert security.protection is False
        assert "*" not in security.allowed_hosts, (
            "a bare `*` survived into allowed_hosts; if protection were ever True "
            "alongside it, every Host would match while the object claimed to be guarded"
        )

    def test_no_configuration_reports_guarded_while_carrying_a_bare_star(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The invariant, stated once: protection and a bare `*` are mutually exclusive."""
        for value in ["", "   ", "a.example.com", "*", "*,foo", "foo,*", "a,b,c"]:
            monkeypatch.setenv("MEMOTRON_MCP_ALLOWED_HOSTS", value)
            security = mcp_host_security()
            assert not (security.protection and "*" in security.allowed_hosts), value


class TestTheHoistCannotFailSilently:
    """`build_mcp_http_app` reaches into `mcp._get_additional_http_routes()`, a PRIVATE
    FastMCP API. If it is renamed or moved, `hoisted` is empty, the function quietly
    returns without an exempt `/health`, and every probe 421s -- a total outage with no
    test failing. This is the test that fails first instead.
    """

    @pytest.mark.parametrize("module_name", ["memotron.mcp_server", "memotron.agent_memory_mcp"])
    def test_the_private_route_accessor_still_yields_health(self, module_name: str) -> None:
        import importlib

        from memotron.runtime import HOISTABLE_PATHS

        mcp = importlib.import_module(module_name).mcp
        paths = {getattr(r, "path", None) for r in mcp._get_additional_http_routes()}
        # HOISTABLE_PATHS, not UNGUARDED_PATHS: the latter includes the trailing-slash
        # spellings, which `build_mcp_http_app` SYNTHESISES and no server registers.
        assert set(HOISTABLE_PATHS) <= paths, (
            f"{module_name}: _get_additional_http_routes() no longer yields "
            f"{HOISTABLE_PATHS} (saw {sorted(p for p in paths if p)}). The private API "
            "moved; /health is now behind the Host guard and every probe will 421."
        )
