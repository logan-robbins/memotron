#!/usr/bin/env python3
"""#206 C4: can a caller actually reach Memotron's MCP tools THROUGH the JedAI Gateway?

Registration is not reachability. `mcp-validate registry drift` reports 0 findings for
`jedai_memotron` on `latest` while proving nothing about whether a tool call works --
and `registry reconcile`, which WOULD test reachability, classifies Memotron as
third-party. Its first-party test (in mcp-forge, `libs/mcp-validator/.../reconcile.py`,
line 88 at time of writing) requires a card in THAT repo whose manifest_id starts
`mcp_jedai_` -- and Memotron's MCP server lives in this repo instead. So it prints
`(3p) Reachable —` and returns EXTERNAL before testing anything. Both gates report green
having never checked us. Hence this.

    uv run python scripts/verify/probe_gateway_mcp.py \\
        --gateway https://latest.jedai-gateway.wdprapps.disney.com \\
        --key "$LITELLM_API_KEY"

Exit 0 if Memotron's tools are reachable; 1 on a finding; 2 if the probe could not run.

WHY THE HANDSHAKE IS NOT OPTIONAL
----------------------------------
A bare `tools/list` POST returns `{"tools": []}` with HTTP 200 and no error, for EVERY
server, including known-good ones. MCP Streamable HTTP requires
`initialize` -> `notifications/initialized` -> `tools/list`, carrying the `mcp-session-id`
the initialize response returns as a HEADER. Skip it and you get a confident, silent zero
that reads exactly like "this server has no tools".

That false reading cost real time: it was nearly filed as a Memotron defect before a
control server produced the identical zero.

THE CONTROL IS THE POINT
-------------------------
Every run also probes `--control` (default `jedai_postgres`), a first-party server known to
work. **A run where the control returns 0 tools proves nothing about Memotron** -- it means
the key, the gateway or the probe is wrong, and the Memotron reading must be discarded
rather than reported. This is the same rule as `pg_isready` not being installed: a probe that
cannot produce a success cannot interpret a failure.

WHAT A FAILURE HERE ACTUALLY MEANS
-----------------------------------
Two independent conditions must both hold, and they are held by DIFFERENT systems:

  1. the key has MCP access grants in LiteLLM       -> else 0 tools, `_meta` EMPTY
  2. the key is bound in Memotron key_principals -> else 0 tools, `_meta` says
                                                       `auth_required` / http_status 401

The empty-vs-auth_required distinction in `_meta` is how you tell them apart, and it is the
only signal that does: both present as "0 tools, HTTP 200".
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

TIMEOUT = 45


def _post(url: str, payload: dict, key: str, session: str | None = None) -> tuple[int, dict, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {key}",
    }
    if session:
        headers["mcp-session-id"] = session
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=TIMEOUT)
        return response.status, dict(response.headers), response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


def _sse(body: str) -> dict:
    """MCP Streamable HTTP answers as text/event-stream even for a single reply."""
    for line in body.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:])
            except json.JSONDecodeError:
                continue
    return {}


def list_tools(gateway: str, server: str, key: str) -> tuple[list[str], dict]:
    """Full handshake, then tools/list. Returns (tool names, LiteLLM's _meta)."""
    url = f"{gateway.rstrip('/')}/{server}/mcp"
    status, headers, _ = _post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe_gateway_mcp", "version": "1"},
            },
        },
        key,
    )
    if status != 200:
        return [], {"probe_error": f"initialize returned {status}"}
    # OPTIONAL since #246. The server runs `stateless_http=True`, which is that issue's
    # fix, and a stateless server issues no `mcp-session-id` at all. This used to return
    # `probe_error` on its absence -- so the probe hard-failed against the FIXED server
    # and reported it as broken. The id is still forwarded when present, because LiteLLM
    # may mint its own, and because a stateful server must keep working here.
    session = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id") or ""

    _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, key, session)
    _, _, body = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, key, session)
    result = _sse(body).get("result", {})
    return [t["name"] for t in result.get("tools", [])], result.get("_meta", {})


def _diagnose(meta: dict) -> str:
    blob = json.dumps(meta)
    if '"internal"' in blob:
        return (
            "LiteLLM reached Memotron and the exchange failed mid-flight. This is almost "
            "certainly #246: MCP Streamable HTTP sessions live in ONE process, the api role "
            "runs multiple replicas, and the Service has no sessionAffinity -- so `tools/list` "
            "lands on a pod that never saw the `initialize`. It is INTERMITTENT by nature: "
            "roughly (replicas-1)/replicas of calls fail. Re-run a few times -- alternating "
            "success and this error IS the signature. A steady failure is something else."
        )
    if "forbidden" in blob or "403" in blob:
        return (
            "the key reached Memotron, which RECOGNISED it and refused the scope -- the "
            "gateway key has no `key_principals` binding. This is the expected state for a "
            "freshly minted key: mint gives it MCP access in LiteLLM, `memotron key bind "
            "--alias <key_alias> --principal-id ... --tenant-id ...` gives it a principal "
            "here. 403 is PROGRESS from 401 -- it means the MCP grant already works."
        )
    if "auth_required" in blob or "401" in blob:
        return (
            "the key reached Memotron and Memotron REFUSED it before identifying it -- "
            "no usable gateway key on the request as far as Memotron is concerned. "
            "Distinct from 403: 401 is 'who are you', 403 is 'I know you, you are not bound'."
        )
    if not meta:
        return (
            "LiteLLM returned no server outcome at all -- the key most likely has no MCP "
            "access grant for this server. Check the access group in the registration."
        )
    return f"unrecognised outcome: {blob[:200]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--server", default="jedai_memotron")
    parser.add_argument("--control", default="jedai_postgres")
    args = parser.parse_args()

    try:
        control_tools, control_meta = list_tools(args.gateway, args.control, args.key)
    except Exception as exc:
        print(f"PROBE ERROR: control {args.control} raised {type(exc).__name__}: {exc}")
        return 2

    print(f"  CONTROL  {args.control:20} {len(control_tools):3} tools")
    if not control_tools:
        print(
            f"\nPROBE INVALID: the control server returned no tools "
            f"({_diagnose(control_meta)})\n"
            "Nothing can be concluded about "
            f"{args.server} from this run. Fix the key or the gateway first."
        )
        return 2

    tools, meta = list_tools(args.gateway, args.server, args.key)
    print(f"  TARGET   {args.server:20} {len(tools):3} tools")

    if tools:
        print(f"\nGATEWAY MCP: PASS -- {len(tools)} tool(s) reachable through the gateway")
        print(f"  sample: {sorted(tools)[:6]}")
        return 0

    print(f"\nGATEWAY MCP: FAIL -- {args.server} is registered but exposes no tools.")
    print(f"  {_diagnose(meta)}")
    if '"internal"' in json.dumps(meta):
        print("  NOTE: one red run does not mean the path is broken. See #246.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
