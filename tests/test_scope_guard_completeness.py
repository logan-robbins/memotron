"""Every public method that takes a scope must check it — or say why it doesn't.

This is the structural half of T3-9. It exists because of T3-8: four public entry
points on ``Memotron`` accept a ``scope`` and never check it against
``authorized_scope_keys``, and nothing noticed for the life of the project.

The root cause was not the four methods. It was that the scope guard's coverage was a
**hand-maintained list** — ``tests/test_promotion.py`` carries a literal ``attempts``
list of entry points to probe, and ``_archive.py`` was simply never added to it. A
hand-maintained list of "things to check" silently stops being complete the moment
someone adds a method, and there is no signal when that happens.

So this test does not enumerate anything by hand. It derives the population from the
class itself: every public method whose signature takes ``scope`` or ``scopes``. A new
entry point is therefore in scope for this test the moment it is written, whether or
not anyone remembers to add it anywhere.

How "guarded" is decided
------------------------
A method counts as guarded if it can reach one of ``_require_authorized_scope``,
``_require_explicit_authorized_scope`` or ``_check_scope_not_read_only`` through
``self.<name>()`` calls, transitively. The transitive part is load-bearing rather than
generous: ``search`` contains no guard at all and is a three-line delegation to
``search_context``, which does. Checking only direct calls reports 14 false positives,
and **a gate with false positives is one people learn to bless past** — which is how
this repo lost a different gate once already.

Limits, stated so this is not over-trusted
------------------------------------------
* **Reachability is "can reach", not "always reaches".** A guard called on one branch
  of an ``if`` satisfies this test. Proving a guard is unconditional needs dataflow
  analysis this does not do.
* **It sees ``self.x()`` calls only** — not decorators, not dynamic dispatch, not a
  guard applied by a caller.
* **It proves a guard is CALLED, never that the guard WORKS.** A guard gutted to
  ``return None`` passes here. That is covered by ``pure_move`` (bytecode identity) and
  by ``scripts/verify/api_surface.py``'s GUARD OWNERSHIP check, which asserts the five
  guards are defined by ``Memotron`` itself and by no mixin.

Together those three cover: does it exist, is it called, is it unmodified.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from memotron.client import Memotron

#: The three enforcement helpers. All five scope/operator guards live on the composer
#: (asserted separately by api_surface.py); these are the three that take a scope and
#: refuse it.
GUARDS = frozenset(
    {
        "_require_authorized_scope",
        "_require_explicit_authorized_scope",
        "_check_scope_not_read_only",
    }
)

#: Public entry points that take a scope and do NOT check it. This is T3-8, recorded
#: here as an executable finding rather than prose, and the list is a RATCHET: the test
#: fails if an entry leaves it (fix landed -> delete the line) and fails if a new
#: unguarded method appears. It may only ever shrink.
#:
#: All four verified PRE-EXISTING: the guard set is empty for each at the branch base
#: as well as at the tip, so the module split neither introduced nor fixed them.
#: **Empty as of 2026-08-29 — T3-8 is closed.** It held four entries for one commit:
#: ``archived_matches``, ``restore_archived_memory``, ``resolve_policy`` and
#: ``resolve_policy_for_principal``. All four now call the guard as their first
#: executable statement, and the ratchet below is what forced them off this list rather
#: than letting the list become a permanent excuse.
#:
#: Keep the mechanism even though the dict is empty. Its value is not the entries — it
#: is that a future unguarded method has to be written down here, with a reason, by
#: someone who has to explain it in review.
KNOWN_UNGUARDED: dict[str, str] = {}

#: Type-only Protocol stubs. They redeclare real method names with `...` bodies, so
#: leaving this module in makes a stub shadow the real implementation in the name->body
#: map and reports the real method as unguarded. Found the hard way: it put `add_memory`
#: -- which does call `_check_scope_not_read_only` -- on the unguarded list.
EXCLUDED_MODULES = {"_protocol.py"}

_PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "src" / "memotron" / "client"


def _self_call_graph() -> tuple[dict[str, set[str]], dict[str, bool]]:
    """name -> the `self.x()` names it calls, and name -> does it call a guard directly."""
    calls: dict[str, set[str]] = {}
    direct: dict[str, bool] = {}
    for path in sorted(_PACKAGE.glob("*.py")):
        if path.name in EXCLUDED_MODULES:
            continue
        for cls in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for fn in cls.body:
                if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                names = {
                    node.func.attr
                    for node in ast.walk(fn)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                }
                calls[fn.name] = names
                direct[fn.name] = bool(names & GUARDS)
    return calls, direct


def _reaches_guard(name: str, calls: dict[str, set[str]], direct: dict[str, bool]) -> bool:
    seen: set[str] = set()
    stack = [name]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if direct.get(current):
            return True
        stack.extend(calls.get(current, ()))
    return False


def _public_scope_methods() -> list[str]:
    out = []
    for name in dir(Memotron):
        if name.startswith("_"):
            continue
        # getattr_static: never trigger a descriptor or property on the class.
        attr = inspect.getattr_static(Memotron, name)
        if not callable(attr):
            continue
        try:
            signature = inspect.signature(attr)
        except (TypeError, ValueError):  # pragma: no cover - builtins
            continue
        if {"scope", "scopes"} & set(signature.parameters):
            out.append(name)
    return sorted(out)


def test_the_population_is_not_empty() -> None:
    """A derived population that silently becomes empty would pass every test below.

    This repo has been bitten twice by a gate reporting PASS over nothing.
    """
    methods = _public_scope_methods()
    assert len(methods) > 50, f"only {len(methods)} scope-taking methods found -- discovery broke"


def test_every_public_scope_method_checks_its_scope() -> None:
    calls, direct = _self_call_graph()
    unguarded = [m for m in _public_scope_methods() if not _reaches_guard(m, calls, direct)]

    unexpected = sorted(set(unguarded) - set(KNOWN_UNGUARDED))
    assert not unexpected, (
        f"{len(unexpected)} public method(s) take a scope and never check it: {unexpected}. "
        "Call self._require_authorized_scope(scope) -- or, if the method genuinely must not "
        "guard, add it to KNOWN_UNGUARDED with the reason. Do not delete this test."
    )


def test_the_known_unguarded_list_only_shrinks() -> None:
    """If a T3-8 entry point gets fixed, this fails until the list is updated.

    Without this, KNOWN_UNGUARDED rots into a permanent excuse -- the exact failure mode
    of the hand-maintained list that caused T3-8 in the first place.
    """
    calls, direct = _self_call_graph()
    fixed = sorted(name for name in KNOWN_UNGUARDED if _reaches_guard(name, calls, direct))
    assert not fixed, (
        f"{fixed} now check their scope. Delete them from KNOWN_UNGUARDED -- the list is a ratchet and may only shrink."
    )


def test_known_unguarded_entries_still_exist() -> None:
    """A renamed or deleted method must not leave a stale excuse behind."""
    population = set(_public_scope_methods())
    missing = sorted(set(KNOWN_UNGUARDED) - population)
    assert not missing, f"KNOWN_UNGUARDED names methods that no longer exist: {missing}"


def test_delegation_is_followed() -> None:
    """Guard `search` specifically, because it is why this analysis is transitive.

    `search` contains no guard call of its own -- it is a delegation to `search_context`.
    A future refactor that made this test direct-only would silently start reporting 14
    false positives, and the fix would look like adding 14 exemptions.
    """
    calls, direct = _self_call_graph()
    assert not direct.get("search"), "search now guards directly -- this test's premise moved"
    assert _reaches_guard("search", calls, direct), "search no longer reaches a guard"
