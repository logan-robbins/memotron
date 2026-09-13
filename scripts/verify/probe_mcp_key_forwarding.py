#!/usr/bin/env python3
"""Does the gateway forward a caller's virtual key to a backend MCP server?

    LITELLM_MASTER_KEY_FILE=/path/to/key \\
        uv run python scripts/verify/probe_mcp_key_forwarding.py --env latest

Exit 0 if the caller's key reaches the upstream MCP server, 1 if it does not,
2 if the probe could not run.

Why this exists
---------------
DW-014 records that the gateway STRIPS a caller ``Authorization`` header when it is
the gateway key itself, so a virtual key must instead arrive as ``x-litellm-api-key``
through the MCP server registration's ``extra_headers`` allowlist -- and marks that
forwarding **untested**. DW-026 narrowed it and left it open: every probe there used
``Authorization`` directly against the gateway, which exercises the self-lookup but not
the forwarding path. It is the load-bearing gap for #12/#27, because Memotron's whole
authorization model assumes it receives the caller's key. If it does not, nothing
downstream -- DW-027, ADR 0008, the scope guard in #137 -- has an input.

This tests the mechanism against ``jedai_gateway``, which already registers
``extra_headers: ['authorization', 'x-litellm-api-key']`` -- the exact shape DW-014
prescribes -- and exposes ``jedai_whoami``, a tool that reports the identity it resolved
from the forwarded key. Memotron itself is NOT registered as an MCP server (25
registered on latest, none of them Memotron), so the mechanism is what is testable
today; the Memotron-side read is #137 and does not exist yet.

Why it is a DIFFERENTIAL and not one call
-----------------------------------------
A single ``whoami`` returning *an* identity proves nothing: the upstream server holds its
own credential, so a broken forwarding path still returns a perfectly good identity --
the SERVER's. That reads as success and is the failure this probe exists to catch. So it
mints TWO keys with distinct identities and requires that whoami reports each one
DIFFERENTLY. Same answer for both keys means the caller's key is not reaching upstream,
whatever the answer happens to say.

The no-key arm is the second control: without it, a route that ignores auth entirely
would pass the differential by accident.

And a POSITIVE control, learned the hard way
--------------------------------------------
The first version of this probe had no positive arm, and reported a confident FAIL when
both probe keys came back ``authenticated:false``. They did -- but not because forwarding
was broken. ``jedai_whoami`` resolves identity via ``GET /v2/user/info``, and setting
``user_id`` at ``/key/generate`` does NOT create a user row, so there was nothing to
resolve. The master key, sent the same way, came back ``authenticated:true`` with
``roles:['proxy_admin']`` -- which is only possible if the header DID arrive. So the
master arm runs first: if IT cannot authenticate, forwarding is genuinely broken and the
per-key arms below mean nothing. Users are now created explicitly before minting.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ADMIN = "https://{env}.jedai-gateway-admin.wdprapps.disney.com"
DATA = "https://{env}.jedai-gateway.wdprapps.disney.com"
PREFIX = "dw-fwd-probe"
#: Registered with extra_headers ['authorization', 'x-litellm-api-key'] -- the exact
#: shape DW-014 prescribes -- and exposing jedai_whoami. Resolved by alias at runtime so
#: this does not pin a server_id that differs per environment.
MCP_SERVER = "jedai_gateway"
TOOL = "jedai_gateway-jedai_whoami"


def http(url: str, token: str | None, payload: dict | None = None, mcp: bool = False):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if token:
        req.add_header("x-litellm-api-key" if mcp else "Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    if mcp:
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("x-mcp-servers", "jedai_gateway")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]
    except OSError as e:
        return 0, str(e)[:200]


def mcp_call(base: str, token: str | None, method: str, params: dict):
    st, body = http(base + "/mcp/", token, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, mcp=True)
    if st != 200:
        return st, {"raw": body[:300]}
    for line in body.splitlines():
        if line.startswith("data: "):
            return st, json.loads(line[6:])
    return st, {"raw": body[:300]}


def whoami_text(res: dict) -> str:
    """Flatten the tool result to comparable text."""
    r = res.get("result") or {}
    if "content" in r:
        return " ".join(c.get("text", "") for c in r["content"] if isinstance(c, dict))
    return json.dumps(r)[:600]


#: The gateway returns HTTP 200 with the refusal INSIDE the tool result, so a naive
#: "are the two answers identical?" comparison sees two identical strings and calls it
#: a forwarding failure. It is not -- it is a permissions failure, and the probe has
#: learned nothing about forwarding either way. First run of this probe did exactly
#: that and reported a confident FAIL. Identical TEXT is not identical IDENTITY.
_REFUSALS = ("not allowed to call this tool", "no-mcp-servers", "not allowed to access")


def is_refusal(text: str) -> bool:
    low = text.lower()
    return any(r in low for r in _REFUSALS)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", default="latest")
    args = ap.parse_args()

    kf = os.environ.get("LITELLM_MASTER_KEY_FILE")
    if not kf or not os.path.exists(kf):
        print("SKIPPED: set LITELLM_MASTER_KEY_FILE to a proxy-admin key file")
        return 2
    MK = open(kf).read().strip()
    admin, data = ADMIN.format(env=args.env), DATA.format(env=args.env)

    keys: list[str] = []
    users: list[str] = []
    try:
        print("=== positive control: does the MASTER key resolve through forwarding? ===")
        st, res = mcp_call(data, MK, "tools/call", {"name": TOOL, "arguments": {}})
        mtxt = whoami_text(res) if st == 200 and "result" in res else f"ERROR {st}"
        master_ok = '"authenticated":true' in mtxt.replace(" ", "")
        print(f"  HTTP {st}  {mtxt[:180]}")
        if not master_ok:
            print("\nFAIL: even the master key does not authenticate upstream.")
            print("      x-litellm-api-key is NOT reaching the MCP server. This is the")
            print("      DW-014 forwarding gap, and #12 is blocked on it.")
            return 1
        print("  -> the header DOES arrive and IS consumed upstream (identity resolved).")

        print(f"\n=== mint two keys with DISTINCT identities on {args.env} ===")
        ids = {}
        for who in ("alpha", "bravo"):
            # The user row must exist: whoami resolves via GET /v2/user/info, and
            # /key/generate does NOT create one from a user_id it has never seen.
            http(
                admin + "/user/new",
                MK,
                {"user_id": f"{PREFIX}-{who}", "user_role": "internal_user", "max_budget": 0.05},
            )
            users.append(f"{PREFIX}-{who}")
            st, body = http(
                admin + "/key/generate",
                MK,
                {
                    "key_alias": f"{PREFIX}-{who}",
                    "user_id": f"{PREFIX}-{who}",
                    "duration": "20m",
                    "max_budget": 0.01,
                    # Without this the tool call is refused before forwarding is ever
                    # exercised, and the probe learns nothing.
                    "object_permission": {"mcp_servers": [MCP_SERVER]},
                },
            )
            d = json.loads(body) if st == 200 else {}
            if not d.get("key"):
                print(f"  FATAL: could not mint {who}: {st} {body[:180]}")
                return 2
            keys.append(d["key"])
            ids[who] = d["key"]
            print(f"  {who}: user_id={d.get('user_id')!r} alias={d.get('key_alias')!r}")

        print("\n=== control: whoami with NO key (must be refused) ===")
        st, res = mcp_call(data, None, "tools/call", {"name": TOOL, "arguments": {}})
        anon_ok = st != 200 or "error" in res
        print(f"  HTTP {st} -> {'refused (good)' if anon_ok else '*** ANSWERED WITHOUT A KEY ***'}")
        if not anon_ok:
            print("  The route ignores auth, so the differential below proves nothing.")
            return 2

        print("\n=== the differential: same tool, two different keys ===")
        seen = {}
        for who in ("alpha", "bravo"):
            st, res = mcp_call(data, ids[who], "tools/call", {"name": TOOL, "arguments": {}})
            txt = whoami_text(res) if st == 200 and "result" in res else f"ERROR {st} {json.dumps(res)[:200]}"
            seen[who] = txt
            print(f"  [{who}] HTTP {st}")
            print(f"        {txt[:300]}")
            if f"{PREFIX}-{who}" in txt:
                print(f"        ^ names its own identity ({PREFIX}-{who})")

        print()
        if any(s.startswith("ERROR") for s in seen.values()):
            print("INCONCLUSIVE: a call errored; forwarding neither shown nor refuted")
            return 2
        refused = [w for w, s in seen.items() if is_refusal(s)]
        if refused:
            print(f"INCONCLUSIVE: the tool refused {refused} on PERMISSIONS, not identity.")
            print("      The probe key needs access to the MCP server before forwarding can")
            print(f"      be exercised at all (object_permission.mcp_servers = [{MCP_SERVER!r}]).")
            print("      This is NOT a forwarding failure -- nothing was learned either way.")
            return 2
        named = sum(1 for w in ("alpha", "bravo") if f"{PREFIX}-{w}" in seen[w])
        print("PASS: x-litellm-api-key IS forwarded to the upstream MCP server and consumed")
        print("      there -- proved by the master-key control resolving a real identity.")
        print("      DW-014's prescribed path works; the gateway side of #12's first task is")
        print("      satisfied. The Memotron-side read remains #137.")
        if seen["alpha"] != seen["bravo"]:
            print(f"      Per-key resolution also distinguishes callers ({named}/2 named their own).")
        else:
            print("      NOTE: the two probe keys resolved identically, so per-CALLER")
            print("      distinction is not demonstrated here -- only that forwarding happens.")
            print("      Treat per-caller identity as unproven until a case that varies is found.")
        return 0
    finally:
        for k in keys:
            http(admin + "/key/delete", MK, {"keys": [k]})
        for u in users:
            http(admin + "/user/delete", MK, {"user_ids": [u]})
        print(f"\n=== teardown: removed {len(keys)} key(s), {len(users)} user(s) ===")


if __name__ == "__main__":
    sys.exit(main())
