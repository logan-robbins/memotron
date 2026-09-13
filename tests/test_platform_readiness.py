"""#217: `platform_api_ready` must be capable of being false.

It tested `bool(platform_api_url)`, and that string is assigned unconditionally in
`main()` — an f-string before #242, `_advertised_url(...)` after, which also always
returns a value. So the flag could not be false, and the console asserted the platform
API was ready in exactly the environments where all 18 `/api/platform/*` routes returned
503 "not enabled for this server".

That is this repo's recurring defect: **a check whose passing condition is satisfied by
the very problem it should surface.** The fix is to test the same predicate `_platform()`
uses to decide the 503, so readiness and behaviour cannot disagree.

**The test that matters is the FALSE case.** A test asserting `platform_api_ready is
True` when a platform is set would have passed against the defect too — it passed for
everyone, always. Only the false arm distinguishes the fix from the bug, which is why it
comes first here.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "memotron" / "admin_server" / "__init__.py"


def _readiness_expr(field: str) -> str:
    """The source of the value assigned to `field` in the readiness dict."""
    tree = ast.parse(SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if isinstance(key, ast.Constant) and key.value == field:
                    return ast.unparse(value)
    raise AssertionError(f"{field!r} not found in any dict literal in {SRC.name}")


def test_platform_api_ready_does_not_test_a_string_that_is_always_set() -> None:
    """The regression, stated as the thing that must NOT come back.

    `platform_api_url` derives from `ui_url`, which `main()` always assigns. Any
    readiness expression referring to it is unfalsifiable by construction.
    """
    expr = _readiness_expr("platform_api_ready")
    assert "platform_api_url" not in expr, (
        f"platform_api_ready is computed from platform_api_url again: {expr!r}. "
        "That string is assigned unconditionally in main(), so the flag cannot be false "
        "and the console will report the API ready where every route 503s (#217)."
    )


def test_platform_api_ready_tests_the_same_thing_the_503_tests() -> None:
    """Readiness and behaviour must not be able to disagree.

    `_platform()` raises 503 when the platform is absent. Readiness must key off the
    same attribute, or the console can report ready while every route refuses.
    """
    expr = _readiness_expr("platform_api_ready")
    assert "platform" in expr and "None" in expr, (
        f"platform_api_ready should test the platform's presence, as _platform() does. Got: {expr!r}"
    )


def test_the_extractor_can_fail() -> None:
    """Guard-the-guard: if `_readiness_expr` silently returned '' the tests above pass.

    Both assertions are substring checks, and both are satisfied by an empty string in
    one direction. Prove the extractor actually finds a real expression, and that it
    raises rather than returning a default for a field that does not exist.
    """
    expr = _readiness_expr("platform_api_ready")
    assert expr.strip(), "extractor returned an empty expression — the checks above are vacuous"

    try:
        _readiness_expr("a_field_that_does_not_exist")
    except AssertionError:
        pass
    else:  # pragma: no cover - only runs if the extractor stops failing closed
        raise AssertionError("extractor returned a value for a nonexistent field")
