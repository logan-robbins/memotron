#!/usr/bin/env python3
"""#191: does a DEPLOYED admin server actually refuse tenant-administration writes?

Every claim about `--no-admin-writes` so far is a test claim: an in-process server on a
loopback socket, driven by `urllib`. That is the right instrument for the routing logic and it
cannot answer the deployment question, which is whether the flag reached the container and the
guard fires on the real ingress.

    uv run python scripts/verify/probe_admin_no_writes.py \\
        --url https://latest.jedai-memotron-admin.wdprapps.disney.com

Exit 0 if the deployed surface behaves; 1 on a finding; 2 if the probe could not run.

WHY THIS EXISTS SEPARATELY FROM sweep_admin.py
-----------------------------------------------
**Do not point `sweep_admin.py` at a deployed admin server.** It POSTs to every route it parses
out of `do_POST`, including `/api/tenant-config/purge`, and `_purge_tenant_state_payload`
requires no fields -- so where the flag is absent that call SUCCEEDS and wipes the tenant's
graph, dream history and agent registrations. Its `EXPECTED_TALLY` is also pinned to a fresh
local SQLite server, so its exit code is red against a deployed environment regardless.

`probe_admin_surface.py` is the other half of that pair and launches its OWN local server, so
it cannot be pointed at a remote host at all. Neither fits. Hence this.

THE TWO THINGS THIS ASSERTS, AND WHY BOTH ARE LOAD-BEARING
-----------------------------------------------------------
A. every tenant-ADMINISTRATION route answers 405
B. the agent-memory API under `/api/platform/` does NOT answer 405

**B is the one that is easy to leave out, and A alone is actively misleading without it.** The
first version of this flag refused *all 28* POST routes -- including the 15 under
`/api/platform/`, which `README.md` documents as the HTTP equivalent of the MCP tools and which
an agent calls at session startup. That version passes A. Shipping on A alone would have
confirmed the bug and called it a fix.

THE CANARY, WHICH IS THE WHOLE SAFETY ARGUMENT
-----------------------------------------------
Probing A means POSTing to `purge`. That is safe **only if the guard is already firing** -- and
that is precisely what is in question. So the order matters:

  1. a GET, to prove the server is reachable and reads work at all
  2. a NON-DESTRUCTIVE admin route (`/api/memory/pin`) as the canary. Guard present -> 405;
     guard absent -> 400 for a missing `relationship_uuid`, which changes nothing
  3. only if the canary returned 405 do we touch `purge`

If the canary is not 405 this exits 1 **without sending a single destructive request**. The
guard also runs before the request body is read (`admin_server/__init__.py`), so a 405 means
the handler was never reached -- "did not happen", not "happened and then reported an error".

The route lists are DERIVED from the server source, never typed here: a hand-maintained list
stops being complete the moment someone adds a route and gives no signal that it has.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
import urllib.error
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ADMIN_SOURCE = REPO_ROOT / "src" / "memotron" / "admin_server" / "__init__.py"

#: Must match `AGENT_MEMORY_ROUTE_PREFIX` in the server. Asserted below rather than assumed.
AGENT_MEMORY_PREFIX = "/api/platform/"

#: Requires a `relationship_uuid`, so an unguarded call fails validation instead of acting.
CANARY_ROUTE = "/api/memory/pin"

#: A read that happens to be a POST. This is check B in its minimal form.
AGENT_MEMORY_READ = "/api/platform/memory/search"

TIMEOUT = 30


def _routes_from_source() -> tuple[list[str], list[str]]:
    """(`tenant_admin`, `agent_memory`) POST routes, read from the server's own AST."""
    if not ADMIN_SOURCE.is_file():
        raise FileNotFoundError(f"cannot read admin server source at {ADMIN_SOURCE}")
    tree = ast.parse(ADMIN_SOURCE.read_text())

    prefix = next(
        (
            node.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "AGENT_MEMORY_ROUTE_PREFIX" for t in node.targets)
            and isinstance(node.value, ast.Constant)
        ),
        None,
    )
    if prefix != AGENT_MEMORY_PREFIX:
        raise ValueError(
            f"AGENT_MEMORY_ROUTE_PREFIX is {prefix!r} in the server but {AGENT_MEMORY_PREFIX!r} "
            "here -- the split this probe asserts has moved; read the server before editing this"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "do_POST":
            routes = sorted(
                {
                    child.value
                    for child in ast.walk(node)
                    if isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and child.value.startswith("/api/")
                }
            )
            return (
                [r for r in routes if not r.startswith(AGENT_MEMORY_PREFIX)],
                [r for r in routes if r.startswith(AGENT_MEMORY_PREFIX)],
            )
    raise ValueError("do_POST not found -- the admin server was restructured")


def _post(base: str, path: str) -> tuple[int, str]:
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def _get(base: str, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}{path}", timeout=TIMEOUT) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="admin base URL, e.g. https://<host>")
    parser.add_argument(
        "--all-agent-memory",
        action="store_true",
        help=(
            "POST to every /api/platform/ route, not just the read. Off by default: several of "
            "them WRITE, and an empty body is not a guarantee they refuse before acting."
        ),
    )
    args = parser.parse_args(argv)

    try:
        admin_routes, memory_routes = _routes_from_source()
    except (OSError, ValueError) as exc:
        print(f"cannot derive the route lists: {exc}", file=sys.stderr)
        return 2
    if not admin_routes or not memory_routes:
        print(
            f"derived {len(admin_routes)} admin and {len(memory_routes)} agent-memory routes; "
            "one being empty means the split collapsed and every check below would be vacuous",
            file=sys.stderr,
        )
        return 2

    print(f"\n  admin surface: {args.url}")
    print(f"  derived {len(admin_routes)} tenant-admin routes, {len(memory_routes)} agent-memory routes\n")

    findings: list[str] = []

    # -- 0. reachable, and reads work ---------------------------------------------------
    status, _ = _get(args.url, "/api/overview")
    print(f"  R0  GET  /api/overview                      {status}")
    if status != 200:
        print(f"\n  the admin surface did not answer a read ({status}); nothing below would mean anything")
        return 2

    # -- 1. the canary, before anything destructive -------------------------------------
    status, body = _post(args.url, CANARY_ROUTE)
    guard_is_live = status == 405
    print(f"  C1  POST {CANARY_ROUTE:<34} {status}  {'guard fires' if guard_is_live else 'NOT refused'}")
    if not guard_is_live:
        print(
            f"\n  FINDING: the canary was not refused ({status}). Either --no-admin-writes is not "
            f"deployed, or it no longer covers this route.\n"
            f"  Refusing to probe /api/tenant-config/purge: unguarded, that call SUCCEEDS.\n"
            f"  body: {body[:200]}"
        )
        return 1

    # -- 2. A: every tenant-admin route is refused ---------------------------------------
    print()
    for route in admin_routes:
        status, body = _post(args.url, route)
        ok = status == 405
        print(f"  A   POST {route:<34} {status}  {'refused' if ok else '*** NOT REFUSED ***'}")
        if not ok:
            findings.append(f"{route} answered {status}, not 405")

    # -- 3. B: the agent-memory API is NOT refused ---------------------------------------
    print()
    probes = memory_routes if args.all_agent_memory else [AGENT_MEMORY_READ]
    for route in probes:
        status, body = _post(args.url, route)
        ok = status != 405
        print(f"  B   POST {route:<34} {status}  {'served' if ok else '*** REFUSED ***'}")
        if not ok:
            findings.append(
                f"{route} answered 405 -- the kill switch is BLANKET, not narrowed; this is the "
                "documented agent-memory HTTP API and an agent calls it at session startup"
            )
    if not args.all_agent_memory:
        print(
            f"      ({len(memory_routes) - 1} further agent-memory routes not probed; --all-agent-memory sends writes)"
        )

    print()
    if findings:
        print("  ADMIN NO-WRITES: FAIL")
        for f in findings:
            print(f"    - {f}")
        return 1
    print(f"  ADMIN NO-WRITES: PASS -- {len(admin_routes)} refused, agent-memory API served")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
