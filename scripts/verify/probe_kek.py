"""B1 probe: can a tenant credential sealed on Postgres be read back later, or elsewhere?

`PostgresStorageBackend.__init__` falls back to `LocalKeyManager.ephemeral()` when no
key_manager is supplied (`storage/postgres/__init__.py:126`), and nothing in the product
supplies one — `storage/factory.py:74` passes only the URL and settings options.
`ephemeral()` is `secrets.token_bytes(...)` — "keys die with the process". If that reaches
deployment, sealed content is unreadable after any restart and across replicas.

Since T0-12's sibling landed, a **fail-closed guard** (`storage/postgres/__init__.py:112-125`)
raises unless `MEMOTRON_ALLOW_EPHEMERAL_KEK` is set, so the shape of the defect changed
from *silent loss* to *refuses to start*. See #130: that is better, and it is the reason
enabling `operationalStore` on the chart makes the agent-memory surface fail to boot.

CITATION REPAIR 2026-08-31: the two citations above previously read
`storage/postgres/__init__.py` line 113 and `client.py` line 157. The first was off by the guard's
own length; the second names a file that no longer exists — `client.py` became a package
during the module split. Both were checked here against current source.

This probe distinguishes explanations for the `wrapped DEK failed authentication` seen
during the MCP sweep (D-41), by changing exactly one thing per arm. Arms A-C set
`MEMOTRON_ALLOW_EPHEMERAL_KEK=1` deliberately: without it every arm dies in the
constructor and the probe measures the guard instead of the KEK, which is what it did
between the guard landing and this repair.

  A  seal and read in the SAME process                      -- baseline; must pass
  B  read from a FRESH process, SAME graph_path (same .kek) -- tests process restart
  C  read from a FRESH process, DIFFERENT graph_path        -- tests replica B
  D  construct with the flag UNSET                          -- tests the guard itself

  A ok, B fails            -> KEK is per-process. A restart loses tenant credentials.
  A ok, B ok, C fails      -> KEK comes from the .kek sibling file; the break is
                              multi-replica only (each pod has its own filesystem).
  A ok, B ok, C ok         -> the sweep failure was an artifact, NOT B1. Report that.
  D does not raise         -> the guard is gone. That is a REGRESSION on its own, and is
                              reported as such rather than folded into the B1 verdict.

    uv run python scripts/verify/probe_kek.py "host=... dbname=..."

exit 0 = credentials survive A-C and the guard holds · 1 = B1 reproduced · 2 = harness error
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

TENANT = "kek-probe-tenant"
SENTINEL = "sk-sentinel-value-for-the-kek-probe"

SEAL = r"""
import json, os, sys
from memotron import Memotron, MemoryScope, ScopeKind
# Construction is INSIDE the try. It used to sit above it, which meant that once the
# fail-closed KEK guard began raising here the child printed no __R__ line at all, the
# parent read that as "no result", and the baseline arm failed -- so the probe reported
# "the probe itself is wrong" for a product behaviour it was supposed to name.
try:
    dw = Memotron(graph_path=os.environ["MEMOTRON_GRAPH_PATH"])
except Exception as e:
    print("__R__" + json.dumps({"ok": False, "construct_failed": True,
                                "err": f"{type(e).__name__}: {e}"}))
    raise SystemExit(0)
scope = MemoryScope(kind=ScopeKind.TENANT, scope_id=sys.argv[1])
dw.configure_tenant_llm_credentials(tenant_id=sys.argv[1], provider="litellm",
                                    api_key=sys.argv[2], base_url="http://example.invalid")
try:
    dek = dw.provision_governance_key(scope=scope)
    print("__R__" + json.dumps({"ok": True, "dek_bytes": len(dek), "status_says_configured": True}))
except Exception as e:
    print("__R__" + json.dumps({"ok": False, "err": f"{type(e).__name__}: {e}"}))
"""

# Must exercise the LIVE-KEY path. tenant_llm_credential_state() returns METADATA and
# never unwraps the DEK, so it succeeds even when the key is unusable -- reading it would
# test the wrong layer, the same mistake as asserting on memory_refresh instead of
# memory_start. get_or_create_governance_key() -> _require_live_key() is the real seam.
READ = r"""
import json, os, sys
from memotron import Memotron, MemoryScope, ScopeKind
out = {}
try:
    dw = Memotron(graph_path=os.environ["MEMOTRON_GRAPH_PATH"])
except Exception as e:
    print("__R__" + json.dumps({"ok": False, "construct_failed": True,
                                "err": f"{type(e).__name__}: {e}"}))
    raise SystemExit(0)
scope = MemoryScope(kind=ScopeKind.TENANT, scope_id=sys.argv[1])
try:
    state = dw.tenant_llm_credential_state(sys.argv[1])
    out["status_says_configured"] = bool(state and state.get("has_api_key"))
except Exception as e:
    out["status_says_configured"] = f"{type(e).__name__}"
try:
    dek = dw.provision_governance_key(scope=scope)
    out.update(ok=True, dek_bytes=len(dek))
except Exception as e:
    out.update(ok=False, err=f"{type(e).__name__}: {e}")
print("__R__" + json.dumps(out))
"""


def child(code: str, graph_path: str, dsn: str | None, *args: str, allow_ephemeral: bool = True) -> dict:
    env = dict(os.environ)
    env["MEMOTRON_GRAPH_PATH"] = graph_path
    # Set explicitly rather than inherited, in BOTH directions. Inheriting it made the
    # probe's answer depend on the operator's shell: with the flag exported every arm
    # ran, without it every arm died in the constructor, and nothing in the output said
    # which had happened.
    if allow_ephemeral:
        env["MEMOTRON_ALLOW_EPHEMERAL_KEK"] = "1"
    else:
        env.pop("MEMOTRON_ALLOW_EPHEMERAL_KEK", None)
    if dsn:
        env["MEMOTRON_OPERATIONAL_STORE_DSN"] = dsn
        env.setdefault("MEMOTRON_OPERATIONAL_STORE_POOL_MIN_SIZE", "1")
        env.setdefault("MEMOTRON_OPERATIONAL_STORE_POOL_MAX_SIZE", "4")
    else:
        env.pop("MEMOTRON_OPERATIONAL_STORE_DSN", None)
    p = subprocess.run([sys.executable, "-c", code, *args], env=env, capture_output=True, text=True, timeout=300)
    for line in p.stdout.splitlines():
        if line.startswith("__R__"):
            return json.loads(line[5:])
    return {"ok": False, "err": f"no result: {p.stdout[-300:]} {p.stderr[-300:]}"}


def scenario(label: str, dsn: str | None) -> list[tuple[str, dict]]:
    home = tempfile.mkdtemp(prefix="kek-a-")
    other = tempfile.mkdtemp(prefix="kek-c-")
    gp = os.path.join(home, "graph.db")

    print(f"\n  === {label} ===")
    a = child(SEAL, gp, dsn, TENANT, SENTINEL)
    b = child(READ, gp, dsn, TENANT)
    c = child(READ, os.path.join(other, "graph.db"), dsn, TENANT)
    # Arm D asks the opposite question: with the opt-out absent, does the guard actually
    # refuse? Only meaningful for Postgres. SQLite has no such guard, so arm D there reads
    # OK and that is the CORRECT result -- it is the control showing the guard is
    # engine-specific, not a second place the guard should have fired. Only the Postgres
    # scenario's arm D feeds the verdict.
    d = child(
        SEAL, os.path.join(tempfile.mkdtemp(prefix="kek-d-"), "graph.db"), dsn, TENANT, SENTINEL, allow_ephemeral=False
    )

    rows = [
        ("A same process", a),
        ("B restart, same .kek", b),
        ("C fresh fs (replica B)", c),
        ("D guard, flag unset", d),
    ]
    for name, r in rows:
        detail = f"live DEK recovered ({r.get('dek_bytes')} bytes)" if r.get("ok") else r["err"][:80]
        if r.get("construct_failed"):
            detail = "REFUSED TO CONSTRUCT: " + detail
        flag = r.get("status_says_configured")
        if flag is True and not r.get("ok"):
            detail += "  [status still reports CONFIGURED]"
        print(f"    {name:26s} {'OK   ' if r.get('ok') else 'FAIL '} {detail}")
    return rows


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    dsn = sys.argv[1]

    # Read ONCE and report it, so a green run can never be mistaken for "the ephemeral-KEK
    # defect is fixed" when it merely had a key handed to it -- and so a red one names which
    # world it was measuring.
    durable_kek = bool(
        os.environ.get("MEMOTRON_KEK_B64", "").strip() or os.environ.get("MEMOTRON_KEK_FILE", "").strip()
    )
    print(
        f"\n  durable KEK configured: {durable_kek} "
        f"({'MEMOTRON_KEK_B64/_KEK_FILE set' if durable_kek else 'neither variable set'})"
    )

    scenario("SQLite (control - .kek sibling file, expected to survive)", None)
    pg = scenario("Postgres (the question)", dsn)

    a, b, c, d = (r for _, r in pg)
    print("\n=== verdict ===")

    # The guard is reported FIRST and separately. Folding it into the B1 verdict is what
    # made this probe useless: a product behaviour it should name -- "refuses to build
    # without an explicit key manager" -- came out as "the probe itself is wrong".
    # ARM D MEANS TWO DIFFERENT THINGS, and conflating them turns the #123 FIX into a
    # reported regression. The guard exists to stop a backend building with an EPHEMERAL
    # key. Once a durable KEK is configured (MEMOTRON_KEK_B64 / _KEK_FILE, #123),
    # building with the opt-out unset is the CORRECT outcome, not a bypass -- there is
    # nothing left to guard against. Before that fix existed the two were indistinguishable,
    # so this arm could assume "built == bypassed"; it no longer can.
    if durable_kek:
        if d.get("construct_failed"):
            print("  GUARD MISFIRE - a durable KEK is configured and the backend STILL refused to")
            print("  build. The guard is firing on the case it exists to permit; key_manager_from_env")
            print("  is returning None, or the client is not passing it to create_storage_backend.")
        else:
            print("  DURABLE KEK IN USE - the backend built with the opt-out unset, which is correct:")
            print("  a real key manager was supplied, so there is no ephemeral key to guard against.")
    elif d.get("construct_failed"):
        print("  GUARD HOLDS - with MEMOTRON_ALLOW_EPHEMERAL_KEK unset and NO durable KEK")
        print("  configured, the backend refuses to build. Sealed state cannot be silently lost;")
        print("  the failure is a startup refusal instead. See #130 for what that costs.")
    else:
        print("  GUARD REGRESSION - the backend built WITHOUT the opt-out and WITHOUT a durable")
        print("  KEK. T0-2's fail-closed guard is gone or bypassed, and silent loss of sealed")
        print("  content is reachable again. Fix this before reading the rest.")

    if a.get("construct_failed"):
        print("  INCONCLUSIVE - the baseline arm could not construct a backend even WITH the")
        print("  opt-out set. That is an environment or wiring problem, not a KEK finding.")
        return 2
    if not a.get("ok"):
        print("  INCONCLUSIVE - the baseline arm failed; the probe itself is wrong.")
        return 2
    # Same split on the exit code: refusing to build is only a PASS condition when no
    # durable key is configured.
    if durable_kek and d.get("construct_failed"):
        return 1
    if not durable_kek and not d.get("construct_failed"):
        return 1
    if not b.get("ok"):
        print("  B1 REPRODUCED, and worse than multi-replica: a tenant credential sealed on")
        print("  Postgres is unreadable by a FRESH PROCESS on the SAME machine. Every pod")
        print("  restart loses sealed tenant state. Cause: LocalKeyManager.ephemeral().")
        return 1
    if not c.get("ok"):
        print("  B1 REPRODUCED, multi-replica only: survives restart (KEK is the .kek file)")
        print("  but replica B cannot read replica A's sealed content. Each pod has its own")
        print("  filesystem, so this breaks the moment replicas > 1.")
        return 1
    print("  NOT B1. Sealed credentials survived restart and a fresh filesystem.")
    print("  The MCP-sweep failure (D-41) has some other cause - do not cite it as B1.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
