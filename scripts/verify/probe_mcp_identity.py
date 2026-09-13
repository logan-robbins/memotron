"""#126: does a deployed Memotron MCP server actually authenticate its callers?

Every claim about the identity guard so far is a test claim: fakes for the gateway, an
in-memory store, an ASGI stack driven by TestClient. That is the right instrument for the
decision logic and it cannot answer the deployment question, which is whether a real virtual
key, forwarded over a real ingress, resolves to the principal the operator bound it to.

    uv run python scripts/verify/probe_mcp_identity.py \\
        --url https://latest.jedai-memotron.wdprapps.disney.com/mcp \\
        --alpha-key <virtual key bound to tenant A> \\
        --bravo-key <virtual key bound to tenant B>

exit 0 = every arm behaved  ·  1 = a security arm failed  ·  2 = a precondition failed

DIFFERENTIAL BY DESIGN, and that is the whole point
---------------------------------------------------
A single key proves nothing here. If header forwarding were broken and the server fell back
to its own ``LITELLM_API_KEY``, or if the middleware resolved one principal and cached it for
everybody, a lone positive arm would still pass and report success. So the probe needs TWO
keys bound to DIFFERENT tenants, and the load-bearing assertion is that they **disagree**:

  A1  alpha writes to alpha's scope           -> allowed
  A2  bravo  writes to bravo's scope          -> allowed
  A3  alpha writes to BRAVO's scope           -> refused
  A4  bravo's scope is UNCHANGED after A3     -> the refusal was real

A3 without A4 is not enough. A refusal message is a string the server chose to print; the
absence of the write is the fact. A guard that refuses in the response and writes anyway
passes A3 and fails A4.

Both keys must be ``--role user``. An ADMIN principal would pass A3 for the wrong reason if
the ADMIN bypass (``config/_control_plane.py``) were ever wired into this path, and a probe
that cannot distinguish "refused because not allowed" from "refused because admin" is
measuring nothing.

Preconditions this probe does NOT create for you
------------------------------------------------
It does not mint gateway keys and it does not bind principals; it needs an admin credential
for the first and store access for the second, and a verification tool that provisions its
own subject can pass by provisioning something the real path never sees. Do both by hand:

    memotron key bind --alias <alias> --principal-id <id> --tenant-id <tenant> \\
        --role user --allowed-scope-key tenant:<tenant>

It also does not turn the guard on. ``MEMOTRON_REQUIRE_GATEWAY_IDENTITY`` must already be
set in the target environment -- and arm P0 below exists because with the flag OFF every
positive arm passes while the server authenticates nobody, which is the single most likely
way to read a green run as proof of something it is not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

FORWARDED_KEY_HEADER = "x-litellm-api-key"


class Arm:
    """One measured claim. Named so the report reads as findings, not as log output."""

    def __init__(self, name: str, detail: str) -> None:
        self.name = name
        self.detail = detail
        self.ok: bool | None = None
        self.note = ""

    def record(self, ok: bool, note: str = "") -> None:
        self.ok = ok
        self.note = note

    def line(self) -> str:
        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[self.ok]
        return f"  {mark}  {self.name:<28} {self.detail}" + (f"\n         {self.note}" if self.note else "")


def _http_status(url: str, key: str | None) -> int:
    """Status of a bare `initialize` POST. No MCP client involved, and that is the point.

    The negative arms CANNOT go through `streamablehttp_client`. A refused request never
    becomes an MCP session: the middleware answers 401 before the handshake completes, the
    client's TaskGroup unwinds, and what surfaces is a `BaseExceptionGroup` -- which
    `except Exception` does not catch, because `BaseExceptionGroup` derives from
    `BaseException`. The first version of this probe wrapped the keyless arm in
    `except Exception` and crashed on it, which is a probe that cannot report the very
    refusal it exists to confirm.

    Checking the status code directly is also better evidence: "401 at the door" is a claim
    about HTTP, so measure HTTP.
    """
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe-mcp-identity", "version": "1"},
            },
        }
    ).encode()
    request = urllib.request.Request(url, data=payload, method="POST")
    request.add_header("Accept", "application/json, text/event-stream")
    request.add_header("Content-Type", "application/json")
    if key:
        request.add_header(FORWARDED_KEY_HEADER, key)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


async def _call(url: str, key: str | None, tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    """Call one tool with a key that is expected to WORK. Transport failures raise.

    Only for arms where the caller is authenticated. For refusals see `_http_status`.
    """
    headers = {FORWARDED_KEY_HEADER: key} if key else {}
    # The transport is named explicitly because this probe needs `headers` -- the whole
    # point of it is which key the request carries. `Client(url)` would infer the same
    # transport but has nowhere to put them.
    #
    # `raise_on_error=False` is load-bearing, not a preference. fastmcp's client raises
    # on a tool error by default; this function's contract is to RETURN whether the tool
    # errored so the caller can assert on it. Letting it raise would turn every expected
    # refusal into a crashed probe, and a probe that crashes on the behaviour it is
    # testing cannot report that behaviour.
    async with Client(StreamableHttpTransport(url, headers=headers)) as session:
        result = await session.call_tool(tool, arguments, raise_on_error=False)
        text = "".join(getattr(block, "text", "") for block in result.content)
        return bool(result.is_error), text


async def _search_count(url: str, key: str, tenant: str, needle: str) -> int:
    """How many memories in `tenant` match `needle`, read WITH that tenant's own key.

    Read as the owner, never as the attacker: asking alpha whether its write landed in
    bravo's scope would be refused by the same guard under test, and a refusal would be
    indistinguishable from an empty result. That confusion is exactly how an absence-of-write
    check turns into a check that cannot fail.
    """
    is_error, text = await _call(
        url,
        key,
        "search",
        {"query": needle, "scope_kind": "tenant", "scope_id": tenant},
    )
    if is_error:
        raise RuntimeError(f"precondition: reading {tenant} with its own key failed: {text[:200]}")
    return text.count(needle)


async def run(url: str, alpha_key: str, bravo_key: str, alpha_tenant: str, bravo_tenant: str) -> int:
    marker = f"probe-126-{uuid.uuid4().hex[:12]}"

    p0 = Arm("P0 guard is armed", "an unauthenticated call is refused")
    a1 = Arm("A1 alpha -> alpha", "the owner may write its own scope")
    a2 = Arm("A2 bravo -> bravo", "and so may the other one, independently")
    a3 = Arm("A3 alpha -> bravo", "naming another tenant is REFUSED")
    a4 = Arm("A4 bravo unchanged", "and the write did not land anyway")
    a5 = Arm("A5 no key", "401 at the door")
    a6 = Arm("A6 health", "probes still work with no credential")
    a7 = Arm("A7 bogus key", "a non-empty but invalid key is refused too")
    arms = [p0, a1, a2, a3, a4, a5, a6, a7]

    def memory(scope_id: str, tag: str) -> dict[str, Any]:
        return {
            "subject": f"{marker}-{tag}",
            "predicate": "PROBED",
            "object": marker,
            "relationship_type": "REQUIRES",
            "scope_kind": "tenant",
            "scope_id": scope_id,
            "source_text": f"#126 identity probe {marker}",
        }

    # ---- P0 / A5. Before anything else: is the guard even on? --------------------------
    # With the flag OFF every positive arm below passes and proves nothing, so a green
    # report would mean "the server let everyone in". Running this first makes that
    # impossible.
    #
    # Measured at the HTTP layer, not through the MCP client -- see `_http_status`. It is
    # also a read-shaped request that never reaches a tool, so nothing is written into
    # bravo's scope before the probe has established it may touch anything.
    keyless = _http_status(url, None)
    p0.record(
        keyless == 401, "" if keyless == 401 else f"UNAUTHENTICATED initialize returned {keyless} -- guard is OFF"
    )
    a5.record(keyless == 401, f"status={keyless}")

    # A bogus key must ALSO be refused, and this is not the same claim. Without it, a server
    # that accepted any non-empty header value would pass the keyless arm and still
    # authenticate nobody.
    bogus = _http_status(url, "sk-probe-not-a-real-key")
    a7.record(bogus == 401, f"status={bogus}")

    if p0.ok is False:
        # Everything after this would be measuring an unguarded server.
        print("\n#126 MCP identity probe -- ABORTED\n")
        for arm in arms:
            print(arm.line())
        print("\nMEMOTRON_REQUIRE_GATEWAY_IDENTITY is not set on the target. Nothing below is meaningful.\n")
        return 2

    # ---- A1 / A2. The positive controls, one per key. -----------------------------------
    #
    # THESE ARE A GATE, NOT A ROW (#257). They used to be recorded and then walked past,
    # which made the whole probe unable to tell "refused correctly" from "nothing works":
    # if alpha cannot write to its OWN scope, A3's refusal of a cross-scope write is
    # exactly what a broken server, a wrong URL or an unbound key also produces. A probe
    # that cannot produce a success cannot interpret a failure -- the same rule that
    # makes `probe_gateway_mcp.py` exit 2 when its control server returns no tools.
    is_error, text = await _call(url, alpha_key, "add_memory", memory(alpha_tenant, "alpha"))
    a1.record(not is_error, text[:160] if is_error else "")
    is_error, text = await _call(url, bravo_key, "add_memory", memory(bravo_tenant, "bravo"))
    a2.record(not is_error, text[:160] if is_error else "")

    if a1.ok is False or a2.ok is False:
        print("\n#126 MCP identity probe -- INVALID\n")
        for arm in arms:
            print(arm.line())
        print(
            "\nA positive control failed: a key could not write to its OWN scope. Every\n"
            "refusal this probe would go on to assert is indistinguishable from that, so\n"
            "nothing below A2 is reported. Check the key bindings (key_principals) and the\n"
            "tenant ids before re-running.\n"
        )
        return 2

    # ---- A3 / A4. The claim. --------------------------------------------------------
    before = await _search_count(url, bravo_key, bravo_tenant, marker)
    is_error, text = await _call(url, alpha_key, "add_memory", memory(bravo_tenant, "crossing"))
    a3.record(is_error, text[:200] if is_error else "ALPHA WROTE INTO BRAVO'S SCOPE AND WAS NOT REFUSED")
    after = await _search_count(url, bravo_key, bravo_tenant, marker)
    a4.record(
        after == before,
        "" if after == before else f"bravo's scope gained {after - before} row(s) from a call that was refused",
    )

    # ---- A6. The one whose regression looks like a crashloop. ---------------------------
    import urllib.request

    health_url = url.rsplit("/mcp", 1)[0] + "/health"
    try:
        with urllib.request.urlopen(health_url, timeout=10) as response:
            body = response.read().decode().strip()
            a6.record(response.status == 200 and body == "ok", f"{response.status} {body[:40]}")
    except Exception as error:
        a6.record(False, f"{type(error).__name__}: {error}")

    print(f"\n#126 MCP identity probe -- {url}")
    print(f"  marker {marker}   alpha={alpha_tenant}  bravo={bravo_tenant}\n")
    for arm in arms:
        print(arm.line())

    failed = [arm.name for arm in arms if arm.ok is False]
    print("\n" + ("ALL ARMS PASSED" if not failed else f"FAILED: {', '.join(failed)}") + "\n")
    print(f"NOTE: this probe leaves its rows behind under marker {marker}. Delete them if the")
    print("target is not a scratch environment; it does not clean up, because a cleanup path")
    print("that runs as one of the two principals would need cross-scope rights to finish.\n")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="MCP endpoint, e.g. https://<host>/mcp")
    parser.add_argument("--alpha-key", required=True, help="virtual key bound to --alpha-tenant, role user")
    parser.add_argument("--bravo-key", required=True, help="virtual key bound to --bravo-tenant, role user")
    parser.add_argument("--alpha-tenant", default="probe-tenant-alpha")
    parser.add_argument("--bravo-tenant", default="probe-tenant-bravo")
    args = parser.parse_args()

    if args.alpha_key == args.bravo_key:
        print("two DIFFERENT keys are required; the same key twice cannot show a disagreement", file=sys.stderr)
        return 2
    if args.alpha_tenant == args.bravo_tenant:
        print("two DIFFERENT tenants are required; A3 would be a same-scope write", file=sys.stderr)
        return 2

    try:
        return asyncio.run(run(args.url, args.alpha_key, args.bravo_key, args.alpha_tenant, args.bravo_tenant))
    except RuntimeError as error:
        print(f"\nPRECONDITION FAILED: {error}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
