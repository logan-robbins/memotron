"""The suite must not be able to reach a real LLM.

Why this file exists
--------------------
``tests/conftest.py`` scrubs provider credentials from every test's environment.
Before it did, that job belonged to whichever test remembered to do it, and one
did not: ``test_project_mode_switch_changes_durable_memory_owner`` deletes
``ANTHROPIC_API_KEY`` and ``OPENAI_API_KEY`` and leaves ``LITELLM_API_KEY``, the
one :func:`memotron.runtime._env_endpoint` prefers over both.  Observed
2026-08-31 with that variable exported: the hermetic lane went from 1170 passed
in 35s to a single test blocking past 10 minutes, because the resolver promoted
rule-based extraction to a live gateway call and the gateway's split-horizon DNS
resolves to an RFC1918 address that black-holes off VPN.  On VPN the same run
would have passed -- slowly, nondeterministically, and billed.

The scrub alone is a list, and this repo has learned repeatedly that a
hand-maintained list rots separately from the thing it guards (the scope-guard
allow-list, the coverage floors, the mypy suppressions).  So the list is not
trusted here: :func:`test_scrub_covers_every_credential_the_resolver_reads`
re-derives the credential names from the resolver's own source, and fails if a
provider is added to it without being added to the scrub.
"""

from __future__ import annotations

import ast
import inspect
import os
import pathlib

import pytest

import memotron
import memotron.runtime as runtime
from conftest import CREDENTIAL_ENVS
from memotron.runtime import _env_endpoint

_SRC = pathlib.Path(memotron.__file__).parent


def _module_level_strings() -> dict[str, str]:
    """Every module-level ``NAME = "literal"`` across the package, name -> value.

    Needed to resolve ``os.environ.get(GATEWAY_API_KEY_ENV, "")``, where the argument is a
    bare name imported from another module. Collected package-wide rather than per-file
    because the constant is defined in ``gateway.py`` and used in three others.
    """
    found: dict[str, str] = {}
    for path in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a parse failure is a lint problem, not ours
            continue
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found[target.id] = node.value.value
    return found


def _credential_envs_read_anywhere_in_src() -> dict[str, set[str]]:
    """Credential variables the PACKAGE reads from the environment, name -> where.

    Scans every module, not one function. The narrow version of this check parsed only
    ``_env_endpoint``, and an independent verifier found the hole: the same two-line
    precedence probe is duplicated three times -- ``runtime.py:71,77``,
    ``adoption.py:418,420`` and ``admin_server/_tenant.py:45,47`` -- so a fourth provider
    added to either of the latter two passed the gate. Deriving from the whole tree removes
    the need to keep a list of places to look, which is the same failure this file exists to
    prevent, one level up.

    Recognises ``environ.get(X)``, ``environ[X]`` and ``getenv(X)``. ``getenv`` has no uses
    in ``src/`` today; it is here so that adding one does not create a blind spot.
    """
    consts = _module_level_strings()

    def resolve(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        return None

    found: dict[str, set[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            name: str | None = None
            if isinstance(node, ast.Call) and node.args:
                target = node.func
                if isinstance(target, ast.Attribute) and target.attr in {"get", "getenv"}:
                    base = target.value
                    is_environ = isinstance(base, ast.Attribute) and base.attr == "environ"
                    is_os_getenv = target.attr == "getenv" and isinstance(base, ast.Name) and base.id == "os"
                    if is_environ or is_os_getenv:
                        name = resolve(node.args[0])
            elif isinstance(node, ast.Subscript):
                base = node.value
                if isinstance(base, ast.Attribute) and base.attr == "environ":
                    name = resolve(node.slice)
            if name and name.endswith("_API_KEY"):
                found.setdefault(name, set()).add(str(path.relative_to(_SRC.parent.parent)))
    return found


def _env_names_read_by(func: object) -> set[str]:
    """Every environment variable name ``func`` passes to ``os.environ.get``.

    String literals are taken as-is; bare names are resolved against the module
    the function came from, so ``os.environ.get(GATEWAY_API_KEY_ENV, "")``
    contributes ``"LITELLM_API_KEY"`` rather than being skipped.  A name that
    resolves to something other than a string is ignored rather than guessed at.
    """
    tree = ast.parse(inspect.getsource(func))  # type: ignore[arg-type]
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        target = node.func
        if not (
            isinstance(target, ast.Attribute)
            and target.attr == "get"
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "environ"
        ):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found.add(arg.value)
        elif isinstance(arg, ast.Name):
            resolved = getattr(runtime, arg.id, None)
            if isinstance(resolved, str):
                found.add(resolved)
    return found


def test_no_provider_credential_is_visible_to_a_test() -> None:
    """The ambient environment is a guarantee, not a coincidence.

    This is the assertion that would have caught the original defect: it fails
    in exactly the situation that produced it -- a developer who followed the
    instruction at the top of ``.env`` and ran ``set -a; . ./.env; set +a``.
    """
    leaked = sorted(name for name in CREDENTIAL_ENVS if os.environ.get(name, "").strip())
    assert leaked == [], (
        f"{leaked} reached a test. The conftest scrub is not doing its job, and "
        "any test that builds a real transport will make a live, billed call."
    )


def test_the_resolver_therefore_configures_no_endpoint() -> None:
    """The property that actually matters, one level below the variable names.

    ``None`` is what makes extraction rule-based and disables the dream agent.
    Asserting it here means a future change that resolves an endpoint by some
    route other than these three variables still fails.
    """
    assert _env_endpoint() is None


def test_scrub_covers_every_credential_read_anywhere_in_the_package() -> None:
    """The real gate: scan the whole package, not one resolver.

    Found by an independent verifier reviewing the narrow version below, which parsed only
    ``_env_endpoint`` -- so a provider added to ``adoption.py`` or ``admin_server/_tenant.py``,
    which carry duplicate copies of the same precedence probe, would have passed.
    """
    reads = _credential_envs_read_anywhere_in_src()
    assert reads, "parsed no credential reads out of src/ at all -- the scanner is broken"
    uncovered = {name: sorted(where) for name, where in reads.items() if name not in CREDENTIAL_ENVS}
    assert not uncovered, (
        f"these credential variables are read by the package but not scrubbed by "
        f"tests/conftest.py: {uncovered}. Add them to CREDENTIAL_ENVS -- a provider the "
        "suite does not scrub is a provider the suite can accidentally call."
    )


def test_scrub_covers_every_credential_the_resolver_reads() -> None:
    """Adding a fourth provider must fail here rather than widen the hole.

    Derived from ``_env_endpoint``'s source, not from a second copy of the list.
    The ``_API_KEY`` suffix is the filter because the resolver also reads model
    and base-URL variables, which are inert without a key.
    """
    credentials = {n for n in _env_names_read_by(_env_endpoint) if n.endswith("_API_KEY")}
    assert credentials, "parsed no credential reads out of _env_endpoint -- the parser is wrong"
    uncovered = sorted(credentials - CREDENTIAL_ENVS)
    assert uncovered == [], (
        f"_env_endpoint reads {uncovered}, which tests/conftest.py does not scrub. "
        "Add them to CREDENTIAL_ENVS; a provider the suite does not scrub is a "
        "provider the suite can accidentally call."
    )


def test_the_scrub_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove the guard above is not vacuous.

    A test asserting "no endpoint is configured" passes for free if the resolver
    can never configure one.  Setting the key must flip it, or the two tests
    above prove nothing.
    """
    assert _env_endpoint() is None
    monkeypatch.setenv("LITELLM_API_KEY", "sk-not-a-real-key")
    resolved = _env_endpoint()
    assert resolved is not None
    assert resolved[0] == "LITELLM_API_KEY"
