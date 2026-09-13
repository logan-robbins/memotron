"""`--no-admin-writes`: a kill switch for an admin surface that has no authentication.

The deployed admin server runs `--principal-role admin` (`.helm/values.yaml`) with **no
authentication of any kind** — no `Authorization`, no cookie, no session. Its 28 POST routes
include `/api/tenant-config/purge`, which wipes a tenant's graph, dream history and agent
registrations, and `/api/tenant-config/llm`, which rewrites a tenant's sealed credential with
a **caller-chosen `base_url`** — redirecting that tenant's extraction traffic on its next
dream cycle. The ingress is `gce-internal`, so the reachable population is the VPC rather than
the internet, which lowers the severity without changing the shape.

This is a **kill switch, not a permission system**. It buys time until the scope guard and
SSO land (#126 / #137 / #23).

**It refuses 13 routes and leaves 15 alone, and that split is the important part.** The 15
under `/api/platform/` are the agent-memory HTTP API that `README.md` documents as the
equivalent of the MCP tools — `bootstrap`, `start`, `search`, `remember`, `publish` — which an
agent calls at session startup. An earlier version of this flag refused **every** POST, and
would have taken that documented API down on every environment to close an exposure living
entirely in the other thirteen routes.

The objection that killed the first allowlist attempt — *"classifying 28 routes correctly is
error-prone, and `remember`/`publish` read like queries but are writes"* — was right about a
SEMANTIC read/write split and does not apply to a structural prefix. And the guard defaults to
**refuse**: `/api/platform/` is the narrow named set that stays open, so a route added tomorrow
is refused until someone decides otherwise, rather than quietly permitted.

**The route list here is DERIVED from the AST, never typed by hand.** A hand-maintained list
silently stops being complete the moment someone adds a route, with no signal that it has —
which is exactly why `test_scope_guard_completeness.py` exists one layer up. A POST route
added tomorrow is covered by these tests the moment it is written.
"""

from __future__ import annotations

import ast
import json
import pathlib
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from queue import Queue
from threading import Thread

import pytest

from memotron import Memotron, MemoryScope, PrincipalRole, ScopeKind, admin_server
from memotron.admin_server import build_demo_control_plane, build_demo_principal


def _routes(handler_method: str) -> list[str]:
    """Every `/api/...` literal inside one handler method, read from the source AST."""
    source = pathlib.Path(admin_server.__file__).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == handler_method:
            return sorted(
                {
                    child.value
                    for child in ast.walk(node)
                    if isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and child.value.startswith("/api/")
                }
            )
    raise AssertionError(f"{handler_method} not found — the admin server was restructured")


POST_ROUTES = _routes("do_POST")
GET_ROUTES = _routes("do_GET")
#: Derived, not typed: the split is a PREFIX, so it cannot drift per route.
AGENT_MEMORY_ROUTES = [r for r in POST_ROUTES if r.startswith("/api/platform/")]
TENANT_ADMIN_ROUTES = [r for r in POST_ROUTES if not r.startswith("/api/platform/")]


def _serve(handler_cls, graph_path: Path, scope: MemoryScope) -> tuple[str, HTTPServer]:
    queue: Queue = Queue()

    def run() -> None:
        client = Memotron(graph_path=graph_path)
        handler_cls.client = client
        handler_cls.default_scope = scope
        handler_cls.graph_path = graph_path
        # Mirrors `main()`: the read test drives `/api/overview`, which resolves a tenant
        # through the principal, so a handler without one 500s before the guard is reached.
        handler_cls.tenant_id = "readonly-tenant"
        handler_cls.control_plane = build_demo_control_plane(
            client=client, default_scope=scope, tenant_id="readonly-tenant", agent_id="readonly-agent"
        )
        client.control_plane = handler_cls.control_plane
        handler_cls.principal = build_demo_principal(
            principal_id="readonly-user",
            tenant_id="readonly-tenant",
            agent_id="readonly-agent",
            default_scope=scope,
            role=PrincipalRole.ADMIN,
        )
        server = HTTPServer(("127.0.0.1", 0), handler_cls)
        queue.put(server)
        server.serve_forever()

    Thread(target=run, daemon=True).start()
    server = queue.get(timeout=10)
    return f"http://127.0.0.1:{server.server_port}", server


def _get_raw(url: str) -> tuple[int, str]:
    """A GET that tolerates a non-2xx, for the routes whose STATUS is the assertion."""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def _post(base_url: str, path: str) -> tuple[int, str]:
    request = urllib.request.Request(
        f"{base_url}{path}", data=b"{}", method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


@pytest.fixture
def admin(tmp_path: Path):
    """A real server on a real socket. Two handler classes, so read-only is the ONLY difference."""
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="readonly")
    graph_path = tmp_path / "readonly.sqlite"
    Memotron(graph_path=graph_path)

    locked = type("LockedHandler", (admin_server.MemoryGraphHandler,), {"no_admin_writes": True})
    open_ = type("OpenHandler", (admin_server.MemoryGraphHandler,), {"no_admin_writes": False})
    locked_url, locked_server = _serve(locked, graph_path, scope)
    open_url, open_server = _serve(open_, graph_path, scope)
    yield locked_url, open_url
    locked_server.shutdown()
    open_server.shutdown()


def test_there_are_post_routes_to_protect_at_all() -> None:
    """Guards the guard: if the AST walk returned nothing, every test below would pass vacuously."""
    assert len(POST_ROUTES) >= 25, f"expected the full mutating surface, derived {len(POST_ROUTES)}"
    for required in ("/api/tenant-config/purge", "/api/tenant-config/llm"):
        assert required in POST_ROUTES, f"{required} vanished from do_POST — was it moved to another verb?"


def test_only_GET_and_POST_exist_so_one_guard_covers_the_whole_mutating_surface() -> None:
    """If a `do_PUT`/`do_DELETE`/`do_PATCH` is ever added, this guard stops being sufficient.

    The kill switch lives in `do_POST` alone. That is only safe while POST is the only
    mutating verb, and nothing else in the codebase would notice if that changed.
    """
    source = pathlib.Path(admin_server.__file__).read_text()
    verbs = {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name.startswith("do_")
    }
    assert verbs == {"do_GET", "do_POST"}, f"a new HTTP verb appeared: {verbs - {'do_GET', 'do_POST'}}"


@pytest.mark.parametrize("path", TENANT_ADMIN_ROUTES)
def test_every_TENANT_ADMIN_route_is_refused(admin, path: str) -> None:
    """THE POINT. Derived over every route, so a new one is covered without editing this file."""
    locked_url, _ = admin
    status, body = _post(locked_url, path)
    assert status == 405, f"{path} returned {status} under --no-admin-writes; body={body[:200]}"
    assert "no-admin-writes" in body


@pytest.mark.parametrize("path", TENANT_ADMIN_ROUTES)
def test_the_same_route_is_NOT_refused_with_the_switch_off(admin, path: str) -> None:
    """The positive control, and it is not optional.

    Every assertion above is "this must be refused". A server that 405'd every POST
    unconditionally — or that failed to start, or rejected the body — would satisfy all of
    them while making the flag meaningless. This proves the refusal is caused by the flag and
    by nothing else.
    """
    _, open_url = admin
    status, _ = _post(open_url, path)
    assert status != 405, f"{path} returned 405 with no_admin_writes=False, so the 405 above proves nothing"


def test_reads_still_work_under_no_admin_writes(admin) -> None:
    """A kill switch that also breaks the console's reads would not be deployed, so it must not."""
    locked_url, _ = admin
    with urllib.request.urlopen(f"{locked_url}/api/overview", timeout=10) as response:
        assert response.status == 200
        assert json.loads(response.read().decode())


def test_the_refusal_happens_BEFORE_the_payload_method_runs(admin, monkeypatch) -> None:
    """405 must mean "did not happen", not "happened and then reported an error".

    `/api/tenant-config/purge` is the route that makes this matter: if the guard ran after
    dispatch, the tenant's graph would already be gone by the time the caller saw the 405.
    """
    locked_url, _ = admin
    called: list[str] = []
    monkeypatch.setattr(
        admin_server.MemoryGraphHandler,
        "_purge_tenant_state_payload",
        lambda self: called.append("purge") or {},
    )
    status, _ = _post(locked_url, "/api/tenant-config/purge")
    assert status == 405
    assert called == [], "the purge payload method ran despite the 405 — the guard is after dispatch"


def test_no_admin_writes_defaults_to_FALSE_so_no_existing_caller_changes() -> None:
    assert admin_server.MemoryGraphHandler.no_admin_writes is False


@pytest.mark.parametrize("path", AGENT_MEMORY_ROUTES)
def test_the_agent_memory_API_is_NOT_refused(admin, path: str) -> None:
    """THE ARM THAT WOULD HAVE CAUGHT THE FIRST VERSION OF THIS FLAG.

    Everything under `/api/platform/` is the product's own documented HTTP surface. A blanket
    refusal passed every other test in this file while breaking it on all three environments,
    and nothing here would have said so. Derived from the same AST walk, so the two halves
    cannot drift apart or silently become empty.
    """
    locked_url, _ = admin
    status, body = _post(locked_url, path)
    assert status != 405, (
        f"{path} is the documented agent-memory API and must survive --no-admin-writes; body={body[:200]}"
    )


def test_the_two_route_populations_are_both_real_and_together_are_everything() -> None:
    """Guards the split itself.

    If the prefix ever stopped matching, one list would silently go empty and its
    parametrized tests would vanish — passing by having nothing to assert.
    """
    assert len(AGENT_MEMORY_ROUTES) >= 12, f"agent-memory routes collapsed to {AGENT_MEMORY_ROUTES}"
    assert len(TENANT_ADMIN_ROUTES) >= 10, f"tenant-admin routes collapsed to {TENANT_ADMIN_ROUTES}"
    assert sorted(AGENT_MEMORY_ROUTES + TENANT_ADMIN_ROUTES) == sorted(POST_ROUTES)
    assert not set(AGENT_MEMORY_ROUTES) & set(TENANT_ADMIN_ROUTES)
    # The two routes the flag exists for must land on the refused side, whatever else moves.
    for required in ("/api/tenant-config/purge", "/api/tenant-config/llm"):
        assert required in TENANT_ADMIN_ROUTES, f"{required} is no longer refused"


@pytest.mark.parametrize(
    "path",
    ["/api/overview", "/api/archive", "/api/adjudication/reviews", "/api/adjudication/entities"],
)
def test_console_READ_routes_all_still_answer_under_the_flag(admin, path: str) -> None:
    """ "Reads are unaffected" is a claim this PR makes about the deployed console, and until
    these arms existed it was asserted for `/api/overview` alone.

    These four are the GET routes the console renders from. A kill switch that also broke a
    read would not be deployable, and the failure would surface as an empty panel rather than
    an error anyone traced back to the flag.
    """
    locked_url, _ = admin
    with urllib.request.urlopen(f"{locked_url}{path}", timeout=10) as response:
        assert response.status == 200, path
        json.loads(response.read().decode())


def test_an_unknown_api_path_is_a_404_not_a_static_asset(admin) -> None:
    """The `/api/` prefix must fall to 404 BEFORE the static-asset branch.

    Without that ordering an unknown `/api/...` path would be looked up on disk and answered
    by the SPA's index.html, so a typo'd or removed endpoint would return 200 with HTML and a
    client would parse it as success.
    """
    locked_url, _ = admin
    status, body = _get_raw(f"{locked_url}/api/definitely-not-a-route")
    assert status == 404, f"expected 404, got {status}"
    assert "not found" in body


#: Every status the static handler can legitimately answer with, READ FROM THE SOURCE rather
#: than guessed: 200 when a built UI is present, **503 when it is not**
#: (`admin_server/__init__.py:204` -- "`GET /` will return 503 while the API keeps working"),
#: and 404 for a present-but-empty `dist` or a path-traversal rejection (`:187-190`).
#:
#: An earlier version of this test asserted `(200, 404)` from the reasoning that "whether a
#: built UI is present varies by checkout". It does -- and the status for *absent* is 503, not
#: 404. Locally `ui/admin/dist` exists so the test only ever saw 200; **CI has no UI build and
#: it failed there**, which is how a red `main` reached three PRs. Enumerating the branch from
#: the source would have cost one grep.
_STATIC_HANDLER_STATUSES = frozenset({200, 404, 503})


def test_a_non_api_path_is_served_as_a_static_asset(admin) -> None:
    """The other side of that branch: non-`/api/` paths reach the static handler.

    The invariant is **which handler answered**, not which status it chose -- the status
    depends on whether `ui/admin/dist` was built, which is an environment fact and not the
    thing under test. What must never happen is the JSON `{"error": "not found"}` from the
    `/api/` branch, which would mean that branch swallowed a static path.
    """
    locked_url, _ = admin
    status, body = _get_raw(f"{locked_url}/some-spa-route")
    assert status in _STATIC_HANDLER_STATUSES, (
        f"{status} is not a status the static handler produces; if this is a new branch, "
        f"read `_send_static_asset` and add it deliberately rather than widening the set"
    )
    assert '"error": "not found"' not in body, "a static path was answered by the /api/ 404 branch"


@pytest.mark.parametrize("build_present", [True, False])
def test_the_static_branch_is_pinned_in_BOTH_build_states(
    admin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_present: bool
) -> None:
    """Pins 200-with-a-build and 503-without, WITHOUT depending on this checkout.

    The test above tolerates any of the three statuses because it is about which handler
    answered. This one controls `static_dir` directly, so both documented branches are
    exercised on every machine.

    That distinction is the whole reason this exists: the ambient `ui/admin/dist` is an
    environment fact, and a test that silently reads it passes locally (where the UI is built)
    and fails in CI (where it is not). That is exactly what happened -- and because
    `check.sh` runs against whatever is on disk, no local gate could see it.
    """
    # Read from `_send_static_asset` (`admin_server/__init__.py:2148-2162`), not inferred:
    #   static_dir MISSING          -> 503 for any path
    #   present, target not a file  -> 404
    #   present, target is a file   -> 200
    # and "", "/", "memory-graph" are rewritten to index.html (:2155). So the build-state
    # branch is only observable through a path that maps to index.html.
    static_dir = tmp_path / "dist"
    if build_present:
        static_dir.mkdir()
        (static_dir / "index.html").write_text("<!doctype html><title>admin</title>")
    monkeypatch.setattr(admin_server.MemoryGraphHandler, "static_dir", static_dir)

    locked_url, _ = admin
    status, body = _get_raw(f"{locked_url}/memory-graph")

    if build_present:
        assert status == 200, f"a built UI must serve index.html, got {status}"
        assert "doctype" in body.lower()
    else:
        # The outcome `warn_if_admin_build_missing` describes in prose at :204.
        assert status == 503, f"a missing build directory must be 503, not {status}"
    assert '"error": "not found"' not in body
