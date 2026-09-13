"""Sweep every admin HTTP route. Parses routes straight from admin_server.py.

Usage: sweep_admin.py http://127.0.0.1:8030
POST bodies self-correct: the API names rejected fields, so we drop and retry.

Exit 0 if the outcome tally matches EXPECTED_TALLY, 1 if it drifts. The tally is the
point: see #140 for why a bare printout was not enough.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

_PKG_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src/memotron"


def _handler_source() -> pathlib.Path:
    """The file defining the HTTP handler, found rather than hardcoded.

    This was `src/memotron/admin_server.py` until 2026-08-31, so `routes()` has raised
    FileNotFoundError since `admin_server` became a package during the module split — which
    means this sweep, one of the four live-lane harnesses, could not run at all, and its
    recorded baseline (`Admin GET OK=16 POST OK=4 HTTP503=15`) has been unreproducible since.

    Located by content, not by name: whichever module defines `do_GET` is the handler. Both
    handlers and all route literals currently live in `admin_server/__init__.py`, but the
    point is that moving them again does not break this script.
    """
    module = _PKG_ROOT / "admin_server.py"  # freshness-ok: deliberate probe for the pre-split shape
    if module.exists():
        return module
    pkg = _PKG_ROOT / "admin_server"
    if not pkg.is_dir():
        raise SystemExit(f"sweep_admin: neither {module} nor {pkg} exists")
    hits = [p for p in sorted(pkg.rglob("*.py")) if "def do_GET" in p.read_text()]
    if len(hits) != 1:
        raise SystemExit(
            f"sweep_admin: expected exactly one module defining do_GET, found {len(hits)}: "
            f"{[str(p) for p in hits]}. The handler was split — teach routes() to span files."
        )
    return hits[0]


SRC = _handler_source()
FULL = {
    "scope": "tenant:memotron-dogfood",
    "tenant_id": "memotron-dogfood",
    "agent_id": "claude-code",
    "agent_name": "Claude Code",
    "query": "Postgres",
    "content": "sweep",
    "task_run_id": "sweep-1",
    "subject": "s",
    "predicate": "requires",
    "object": "o",
    "relationship_type": "REQUIRES",
    "reason": "sweep",
    "rationale": "sweep",
    "provider": "litellm",
    "api_key": "sk-sweep",
    "decision": "approve",
    "resolved_by": "sweep",
    "project_goal": "g",
    "memory_goal": "m",
    "keep": ["decisions"],
    "configured_by": "sweep",
}


def routes():
    src = SRC.read_text()
    i = src.index("def do_POST")

    def pat(b):
        return sorted(set(re.findall(r'parsed\.path == "(/[^"]*)"', b)) | set(re.findall(r'path == "(/api/[^"]*)"', b)))

    return pat(src[:i]), pat(src[i:])


def call(path, body=None, base=None):
    url = f"{(base if base is not None else BASE)}{path}" + ("" if body else "?scope=tenant%3Amemotron-dogfood")
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


#: MEASURED 2026-09-01 against a standalone `memotron-admin-server` on a fresh graph,
#: which is exactly what the deployed admin pod runs. 53 routes, every one exercised.
#:
#: This replaces the prose baseline `GET OK=16 POST OK=4 HTTP503=15` recorded in the
#: verification skill, which accounted for only 35 of 53 and could never be reconciled --
#: #140. The reason is now measured rather than guessed: **those three buckets do not span
#: the outcome space.** HTTP400 and HTTP204 had no bucket at all, so 14 of today's 53
#: outcomes were structurally uncountable in that format. The "missing 18" was an artifact
#: of an incomplete tally, NOT 18 unexercised or undiscovered routes.
#:
#: What each bucket means, so a future drift is diagnosable rather than just red:
#:
#:   HTTP503=18  every `/api/platform/*` route, and ALL of them. `handler.platform` is None
#:               in the standalone server and no CLI flag sets it (T1b-9). This is the
#:               number that should change when the platform surface is wired or removed.
#:   HTTP400=13  correct input validation, verified by reading every message: routes wanting
#:               `epoch_id`, `relationship_uuid`, `entity`, `subject`, `request_id`, `name`,
#:               `status`, `prompt_text` -- parameters the generic sweep body cannot supply.
#:               These are the surface working, not failing.
#:   HTTP204=1   a no-content success.
#:   OK=21       the routes that answer with a body on a fresh graph.
#:
#: A DIFFERENT graph will move OK/400 around; this is pinned to a FRESH one for that reason.
EXPECTED_TALLY = {"OK": 21, "HTTP400": 13, "HTTP204": 1, "HTTP503": 18}

#: The load-bearing invariant, separate from the tally so a change says which thing changed.
EXPECTED_PLATFORM_503 = 18


def main():
    global BASE
    BASE = sys.argv[1].rstrip("/")
    gets, posts = routes()
    tally: dict[str, int] = {}
    platform_503 = 0
    for label, rs in (("GET", gets), ("POST", posts)):
        print(f"\n===== {label} ({len(rs)}) =====")
        for r in rs:
            body = dict(FULL) if label == "POST" else None
            for _ in range(6):  # negotiate away unknown fields
                code, txt = call(r, body)
                m = re.search(r'unknown field\(s\): ([^"]+)', txt)
                if code == 400 and m and body is not None:
                    for f in [x.strip() for x in m.group(1).split(",")]:
                        body.pop(f, None)
                    continue
                break
            v = "OK" if code == 200 else f"HTTP{code}"
            tally[v] = tally.get(v, 0) + 1
            if code == 503 and r.startswith("/api/platform/"):
                platform_503 += 1
            flag = "  <-- platform API dead in standalone" if code == 503 else ""
            print(f"  {r:44s} {v:8s} {txt[:56].replace(chr(10), ' ')}{flag}")
    print("\n  TALLY:", "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))

    drift = []
    if tally != EXPECTED_TALLY:
        drift.append(f"tally {tally} != expected {EXPECTED_TALLY}")
    if platform_503 != EXPECTED_PLATFORM_503:
        drift.append(f"{platform_503} of the /api/platform/* routes returned 503, expected {EXPECTED_PLATFORM_503}")
    if not drift:
        print(f"  ADMIN SWEEP: PASS -- {sum(tally.values())} routes, tally matches the 2026-09-01 baseline")
        return 0
    print("\n  ADMIN SWEEP: DRIFT")
    for line in drift:
        print(f"        {line}")
    print("\n  Drift in EITHER direction is a finding. If the platform 503s dropped, the")
    print("  platform surface was wired up (T1b-9) -- update EXPECTED_PLATFORM_503 and say so.")
    print("  If OK/HTTP400 moved, check the graph is fresh before changing anything.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
