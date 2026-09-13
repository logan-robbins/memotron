"""Systematic sweep of every MCP tool on both Memotron servers.

Runs against a DISPOSABLE graph copy so destructive tools are safe.
Classifies each tool: OK / EMPTY (suspicious) / ERROR / SKIP (no safe args).

Usage: sweep.py <governance_url> <agentmem_url>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

SCOPE = {"scope_kind": "tenant", "scope_id": "memotron-dogfood"}
AGENT = "sweep-agent"

# Filled at runtime from live graph state so args are realistic.
#
# These defaults are stale literals harvested from one past SQLite session. A uuid that does
# not exist in the store under test makes every uuid-taking tool fail for the WRONG reason,
# which both wastes coverage and hides real divergence when comparing two backends. Override
# them per-store via SWEEP_<KEY> env vars — see seed_for_mcp_sweep.py, which harvests a real
# NON-MENTIONS relationship (on Postgres a scoped read returns MENTIONS first — see T0-4 —
# so grabbing row [0] blindly is exactly the confound that produced four phantom findings
# in D-39).
#: The tenant every scoped tool is swept against. The literal below is the local dogfood
#: store; a DEPLOYED target will refuse it, and correctly so. Measured against latest on
#: 2026-09-11 with a bound key: enumeration succeeded (39 tools) and then EVERY call was
#: refused with `scope 'tenant:memotron-dogfood' is not one of principal 'p-j...'` --
#: the authorization guard working exactly as designed, and 36 findings that were really
#: one stale literal. Set SWEEP_TENANT to a tenant the probe's key is actually bound to.
TENANT = os.environ.get("SWEEP_TENANT", "").strip() or "memotron-dogfood"

STATE = {
    "relationship_uuid": "0420e0a8-c0e1-4eec-a44d-90447217e343",
    "episode_uuid": "873a06c7-ba01-4d51-a60b-fbb018407126",
    "use_id": None,
    "job_name": "consolidation-default [memotron-dogfood/claude-code/agent:claude-code/balanced]",
    "epoch_id": None,
    "incident_id": None,
}

for _k in list(STATE):
    _v = os.environ.get(f"SWEEP_{_k.upper()}", "").strip()
    if _v:
        STATE[_k] = _v
if os.environ.get("SWEEP_VERBOSE"):
    print(f"  [STATE {', '.join(f'{k}={str(v)[:8]}' for k, v in STATE.items() if v)}]")


def arg_for(tool, key):
    """Best-effort plausible value for one required argument."""
    s = STATE
    table = {
        "scope_kind": "tenant",
        "scope_id": TENANT,
        "agent_id": AGENT,
        "agent_name": "Sweep Agent",
        "query": "Postgres",
        "subject": "sweep-subject",
        "predicate": "requires",
        "object": "sweep-object",
        "relationship_type": "REQUIRES",
        "name": "sweep-item",
        "episode_body": "Sweep episode body for coverage testing.",
        "content": "Sweep content for coverage testing.",
        "turns_json": json.dumps([{"role": "user", "content": "hello"}]),
        "task_run_id": "sweep-run-1",
        "idempotency_key": "sweep-idem-1",
        "kind": "injected",
        "verdict": "success",
        "judge_identity": "sweep-judge",
        "judge_input_digest": "0" * 64,
        "rationale": "sweep coverage",
        "reason": "sweep coverage",
        "corrected_object": "sweep-corrected",
        "entity": "Postgres cutover",
        "tenant_id": TENANT,
        "provider": "litellm",
        "api_key": "sk-sweep-not-a-real-key",
        "motive_name": "agent-memory",
        "decision": "approve",
        "resolved_by": "sweep-operator",
        "status": "resolved",
        "statement": "sweep resolution statement",
        "project_goal": "sweep goal",
        "memory_goal": "sweep memory goal",
        "keep_json": json.dumps(["decisions"]),
        "configured_by": "sweep-operator",
        "agents_json": json.dumps([AGENT]),
        "artifact_id": "sweep-artifact",
        "artifact_class": "file",
        "location": "/tmp/sweep.txt",
        "author": "sweep",
        "self_authored": True,
        "runtime_trace": "sweep-trace",
        "artifact_version": "v1",
        "relationship_uuid": s["relationship_uuid"],
        "candidate_episode_uuid": s["episode_uuid"],
        "use_id": s["use_id"],
        "job_name": s["job_name"],
        "epoch_id": s["epoch_id"],
        "incident_id": s["incident_id"],
        "request_id": "sweep-request",
    }
    return table.get(key, "sweep-value")


def classify(name, res, body):
    # `res.is_error`, read as an ATTRIBUTE and not through `getattr(..., False)`.
    # The field was `isError` under the old SDK. A defaulted getattr on the renamed
    # field would have read False for every single result and classified every ERROR
    # as OK -- the sweep would have gone green precisely because it was broken. Let
    # an AttributeError be raised if this name moves again.
    if res.is_error:
        return "ERROR", body[:120].replace("\n", " ")
    b = body.strip()
    if not b or b in ("[]", "{}", "null"):
        return "EMPTY", "returned nothing"
    # a JSON object whose only list fields are empty is suspicious for read tools
    try:
        d = json.loads(b)
        if isinstance(d, dict):
            lists = {k: v for k, v in d.items() if isinstance(v, list)}
            if lists and all(len(v) == 0 for v in lists.values()):
                return "EMPTY", "all list fields empty: " + ",".join(lists)
    except Exception:
        pass
    return "OK", b[:90].replace("\n", " ")


def text_of(res):
    return "\n".join(c.text for c in (getattr(res, "content", []) or []) if getattr(c, "text", None))


async def sweep(url, label):
    # fastmcp.Client: infers the transport, initializes on __aenter__, follows the
    # /mcp -> /mcp/ 307. `list_tools()` returns the list itself here, not a result
    # wrapper with a `.tools` attribute.
    async with _client(url) as s:
        tools = sorted(await s.list_tools(), key=lambda t: t.name)
        results = []
        for t in tools:
            # `.input_schema`. THREE names for one field, and picking the wrong one
            # here is an AttributeError rather than a wrong answer, which is the only
            # reason it was caught: this script is in no test suite.
            #   client-side protocol Tool : `.input_schema` (mcp v2 renamed it;
            #                               `.inputSchema` still resolves but warns)
            #   server-side FunctionTool  : `.parameters`  <- what tests/ reads
            # This is a CLIENT, so it is the first.
            schema = t.input_schema or {}
            req = schema.get("required", []) or []
            props = schema.get("properties", {}) or {}
            args, missing = {}, []
            for k in req:
                v = arg_for(t.name, k)
                if v is None:
                    missing.append(k)
                else:
                    args[k] = v
            if missing:
                results.append((t.name, "SKIP", "no value for " + ",".join(missing)))
                continue
            # coerce ints/bools the schema asks for
            for k, v in list(args.items()):
                typ = (props.get(k) or {}).get("type")
                if typ == "integer" and not isinstance(v, int):
                    args[k] = 1
                if typ == "boolean" and not isinstance(v, bool):
                    args[k] = True
            try:
                # raise_on_error=False: this sweep CLASSIFIES errors, so it must
                # receive them as results. fastmcp's client raises by default.
                res = await asyncio.wait_for(s.call_tool(t.name, args, raise_on_error=False), timeout=90)
                verdict, detail = classify(t.name, res, text_of(res))
            except Exception as e:
                verdict, detail = "ERROR", f"{type(e).__name__}: {str(e)[:100]}"
            results.append((t.name, verdict, detail))
            print(f"  {t.name:34s} {verdict:6s} {detail[:96]}")
        return label, results


#: Forwarded gateway key header, same name the identity middleware reads.
FORWARDED_KEY_HEADER = "x-litellm-api-key"


def _client(url):
    """A client for `url`, authenticated if a key is available.

    WITHOUT this the sweep cannot reach the only environment where the MCP surface is
    armed. `latest` runs `requireGatewayIdentity: true`, so an unauthenticated
    `list_tools` is refused, the CONTROL fails, and the sweep correctly reports INVALID
    and exits 2 -- correct behaviour, useless outcome. Measured against deployed latest
    before this was added: `CONTROL FAIL  MCPError: Server returned an error response`.

    The key is read from MEMOTRON_PROBE_KEY, falling back to LITELLM_API_KEY. An
    unarmed target ignores the header entirely, so sending it always is harmless and
    means one invocation works against both.
    """
    key = os.environ.get("MEMOTRON_PROBE_KEY") or os.environ.get("LITELLM_API_KEY") or ""
    if not key:
        return Client(url)
    return Client(StreamableHttpTransport(url, headers={FORWARDED_KEY_HEADER: key}))


async def control(url, label):
    """Can this server be reached and enumerated AT ALL? Returns (ok, detail).

    THE CONTROL IS THE POINT (#257). A sweep classifies every tool as OK / EMPTY / ERROR,
    and all three are indistinguishable from "the probe never reached the server" unless
    something that MUST succeed is measured first. The recorded near-miss:

        a bare `tools/list` returned 0 tools for EVERY server, and it was nearly filed
        as a Memotron defect before a known-good server produced the identical zero.

    A direct-to-server sweep has no sibling server to compare against the way
    `probe_gateway_mcp.py` uses `--control jedai_postgres`, so the control here is the
    two things that must hold before any classification means anything: the client
    connects, and the server enumerates at least one tool. A run that fails this is
    reported as INVALID with exit 2 -- never as "every tool is broken".
    """
    try:
        async with _client(url) as session:
            tools = await session.list_tools()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if not tools:
        return False, "connected, but the server enumerated 0 tools"
    return True, f"{len(tools)} tools enumerated"


async def main():
    if len(sys.argv) < 3:
        print(__doc__.strip().splitlines()[0] if __doc__ else "")
        print("\n  usage: sweep_mcp.py <governance_mcp_url> <agentmem_mcp_url>")
        print("  Both are DIRECT server URLs (…/mcp), not the gateway.")
        return 2

    out = {}
    for url, label in ((sys.argv[1], "governance"), (sys.argv[2], "agentmem")):
        print(f"\n===== {label} =====")
        ok, detail = await control(url, label)
        print(f"  CONTROL  {label:12s} {'OK ' if ok else 'FAIL'}  {detail}")
        if not ok:
            print(
                f"\nSWEEP INVALID: {label} could not be enumerated ({detail}).\n"
                "Nothing can be concluded about its tools from this run -- every EMPTY and\n"
                "ERROR below would be the transport, not the tool. Fix the URL or the server\n"
                "first. NOTE the 307: /mcp redirects to /mcp/, and a client that does not\n"
                "follow it reads the redirect as the result."
            )
            return 2
        lbl, res = await sweep(url, label)
        out[lbl] = res

    print("\n===== TALLY =====")
    verdict = 0
    for lbl, res in out.items():
        c = {}
        for _, v, _ in res:
            c[v] = c.get(v, 0) + 1
        print(f"  {lbl:12s} " + "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
        # A sweep where EVERY tool errored passed the control but is still not a set of
        # findings -- it is one finding about the server. Distinguished here rather than
        # left for a reader to notice in 39 identical lines.
        if c.get("ERROR", 0) and c.get("OK", 0) == 0:
            print("    ^ every tool ERRORED and none succeeded -- read this as ONE server-level")
            print(f"      failure, not {c['ERROR']} tool defects.")
            verdict = 1

    json.dump({k: [list(x) for x in v] for k, v in out.items()}, open("/tmp/sweep_results.json", "w"), indent=1)
    print("  -> /tmp/sweep_results.json")
    return verdict


sys.exit(asyncio.run(main()))
