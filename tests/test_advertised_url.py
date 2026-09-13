"""#242: the integration contract must advertise where callers REACH us.

`/api/platform/integration-contract` is the machine-readable "how to integrate"
document. On every hosted deployment it published `platform_api_url:
http://127.0.0.1:8765/` and an empty `mcp_url`, because the value was derived from the
BIND address and the container binds `0.0.0.0`. A client that fetched the contract and
followed it dialled localhost.

**Every other field in that contract was correct**, which is what made it dangerous: the
document was trustworthy everywhere except the part saying where to connect, so it
invited being followed.

The process cannot discover its own external hostname, so `--public-url` is the only
thing that can be right behind an ingress. The chart sets it from
`MEMOTRON_PUBLIC_URL` per environment.
"""

from __future__ import annotations

import ast
import ipaddress
import pathlib
import urllib.parse

import pytest

from memotron.admin_server import _advertised_url

HOSTED = "https://latest.jedai-memotron-admin.wdprapps.disney.com"


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def test_the_advertised_host_is_not_loopback_when_a_public_url_is_given() -> None:
    """The acceptance criterion from #242, stated the way the issue states it.

    Deliberately NOT `assert url` or `assert url != ""`. **A non-emptiness check passes
    on the defect** -- `http://127.0.0.1:8765/` is non-empty, well-formed, and exactly
    what shipped. The assertion has to be about the HOST being reachable by someone
    other than the process itself, which is the property that was actually violated.
    """
    url = _advertised_url(public_url=HOSTED, host="0.0.0.0", port=8765)
    assert not _is_loopback(url), f"advertised a loopback address to remote callers: {url}"
    assert url.startswith(HOSTED)


def test_binding_a_wildcard_without_a_public_url_still_advertises_loopback() -> None:
    """The un-fixed shape, pinned on purpose.

    This is what a HOSTED deployment gets if nobody sets `--public-url`, and it is the
    defect. It stays correct for LOCAL development, where bind and public are the same
    address -- so the fallback is kept rather than made an error, and this test records
    that the fallback is a local-dev affordance, not a hosted one. If someone later
    decides a wildcard bind with no public URL should fail fast, this test is the one to
    change, and changing it should be a decision rather than a surprise.
    """
    url = _advertised_url(public_url="", host="0.0.0.0", port=8765)
    assert _is_loopback(url)
    assert url == "http://127.0.0.1:8765/"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (HOSTED, f"{HOSTED}/"),
        (f"{HOSTED}/", f"{HOSTED}/"),
        (f"  {HOSTED}  ", f"{HOSTED}/"),
    ],
)
def test_the_advertised_url_always_ends_in_a_slash(given: str, expected: str) -> None:
    """Consumers join paths onto this. `…disney.com` + `api/...` loses a segment."""
    assert _advertised_url(public_url=given, host="0.0.0.0", port=8765) == expected


def test_main_actually_calls_it() -> None:
    """Guard-the-guard: extracting the decision is only useful if `main()` uses it.

    `_advertised_url` could be perfect and orphaned, and every test above would still
    pass while the contract published the bind address again. That is not hypothetical
    in this file's neighbourhood: `handler.platform` was never assigned in `main()` at
    all, and an entire test file passed because it replicated the wiring instead of
    driving it (tests/test_admin_platform_enabled.py). AST-walk for the CALL, not a
    substring -- a docstring mentioning the name would satisfy a grep.
    """
    source = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "memotron" / "admin_server" / "__init__.py"
    ).read_text()
    main_fn = next(
        node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    calls = {
        node.func.id for node in ast.walk(main_fn) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_advertised_url" in calls, (
        "main() does not call _advertised_url(), so the contract's URL is being computed "
        "somewhere else and #242 can regress without any test above failing"
    )
