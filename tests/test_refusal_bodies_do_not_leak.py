"""Every refusal the caller can elicit must say only what a stranger may read.

#263. `GatewayIdentityRefusalError` carries two messages on purpose: `detail` for our log,
`public_detail` for the caller. `public_detail` **defaults to `detail`**, and that default is
deliberate -- several refusals are written to be read by a stranger and duplicating the string
would be worse. The cost of the default is that omitting it is silent, and one of the seven
construction sites omitted it while interpolating the operational store's exception:

    503 {"error": "principal lookup failed for 'a1': connection to server at
     \\"dw-operational-pg.jedai-memotron.svc.cluster.local\\" (10.154.9.31), port 5432
     failed: FATAL:  password authentication failed for user \\"memotron_rw\\""}

Internal DNS name, pod IP, port, database username -- to an **unauthenticated** caller,
reachable whenever the store is unhealthy. A refusal is the one response an attacker can
always elicit, which is what makes it the last place to relay somebody else's error text.

The class already existed *because* of an earlier leak of exactly this shape: LiteLLM's
auth-error body carrying `Received API Key = sk-...` plus the key's verification-table hash.
So this is the second occurrence of one defect, and the reason these tests are structural
rather than a list of cases.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from memotron.gateway_identity import (
    GatewayIdentity,
    GatewayIdentityRefusalError,
    GatewayPrincipalResolver,
)

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "src" / "memotron" / "gateway_identity.py"

#: Sites whose `detail` is safe to hand a stranger verbatim, so the default is correct.
#: Keyed by the literal text that starts the message, NOT by line number -- a line number
#: baseline goes stale on the first edit above it and then silently allowlists whatever
#: moved into its place.
#:
#: To add an entry: the message must contain NO interpolated exception, hostname, DSN,
#: credential or store detail. If it interpolates anything the caller did not already
#: supply, it does not belong here -- pass `public_detail` instead.
DETAIL_IS_PUBLIC_SAFE = {
    # States a fact about the caller's own key. DW-027: ~40% of gateway keys carry no
    # identity, and the caller needs to know that is why they were refused.
    "the gateway asserts no key_alias",
    # Names the header the caller themselves sent.
    "is present but empty; not falling back",
}


def _refusal_sites() -> list[ast.Call]:
    """Every `GatewayIdentityRefusalError(...)` construction in the module."""
    tree = ast.parse(SOURCE.read_text())
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "GatewayIdentityRefusalError"
    ]


def _detail_text(call: ast.Call) -> str:
    """The literal parts of the `detail` argument, joined. Interpolations are dropped."""
    if len(call.args) < 2:
        return ""
    detail = call.args[1]
    if isinstance(detail, ast.Constant) and isinstance(detail.value, str):
        return detail.value
    if isinstance(detail, ast.JoinedStr):
        return "".join(v.value for v in detail.values if isinstance(v, ast.Constant))
    return ""


def test_there_are_refusal_sites_to_check() -> None:
    """The control. Every assertion below is vacuous if the walk finds nothing --
    a renamed class or a moved file would otherwise turn this file green and silent."""
    sites = _refusal_sites()
    assert len(sites) >= 6, f"found only {len(sites)} refusal sites; the AST walk is not finding them"


def test_every_refusal_either_scrubs_or_is_declared_safe() -> None:
    """THE GUARD. A new site that forgets `public_detail` fails here, not in production.

    Passing `public_detail` is the normal answer. The alternative -- relying on the default
    -- requires the message to be listed in `DETAIL_IS_PUBLIC_SAFE`, which is a deliberate
    edit with a stated reason rather than an omission.
    """
    offenders = []
    for call in _refusal_sites():
        if any(kw.arg == "public_detail" for kw in call.keywords):
            continue
        text = _detail_text(call)
        if any(safe in text for safe in DETAIL_IS_PUBLIC_SAFE):
            continue
        offenders.append(f"line {call.lineno}: {text[:80]!r}")

    assert not offenders, (
        "refusal site(s) with no `public_detail` and no entry in DETAIL_IS_PUBLIC_SAFE:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe caller receives `detail` verbatim when `public_detail` is omitted. If that "
        "message interpolates an exception, a hostname, a DSN or anything the caller did not "
        "supply, pass a scrubbed `public_detail`. Only add to DETAIL_IS_PUBLIC_SAFE if the "
        "text is written to be read by a stranger."
    )


def test_an_interpolated_exception_never_reaches_the_caller() -> None:
    """The behavioural half, on the path that actually leaked.

    The AST test above proves the *shape*; this proves the *outcome*, through the real
    resolver, with a realistic psycopg message. Both are needed: the AST test would pass on
    a site that passes `public_detail=f"...{error}"`.
    """
    boom = (
        'connection to server at "dw-operational-pg.jedai-memotron.svc.cluster.local" '
        "(10.154.9.31), port 5432 failed: FATAL:  password authentication failed for "
        'user "memotron_rw"'
    )
    identity = GatewayIdentity(key_alias="a1", team_id=None, user_id=None)

    def exploding_lookup(alias: str) -> dict[str, object]:
        raise RuntimeError(boom)

    resolver = GatewayPrincipalResolver(resolver=lambda key: identity, principal_lookup=exploding_lookup)

    with pytest.raises(GatewayIdentityRefusalError) as caught:
        resolver.principal_for_identity(identity)

    # Read the body ONCE into a variable. `x not in ""` is always True, so a negative
    # assertion against a value re-derived per check can pass because the value is empty.
    public = caught.value.public_detail
    internal = str(caught.value)

    assert public, "public_detail is empty -- every assertion below would pass vacuously"
    for secret in ("dw-operational-pg", "10.154.9.31", "5432", "memotron_rw", "FATAL"):
        assert secret not in public, f"{secret!r} reached the caller in: {public!r}"

    # The positive half: it must still be 503, still actionable, and the detail must still
    # reach the log. Scrubbing that also destroyed the diagnosis would be its own defect.
    assert caught.value.status == 503
    assert "unavailable" in public
    assert "dw-operational-pg" in internal, "the full text must still reach the log"
