"""The coupling gate's decision logic: cycles, tiers, and what it refuses to gate on.

Why this gate was rewritten
---------------------------
It counted CALL SITES -- 279 `self.graph.*` in `client/`, 78 `reveal`, 72
`graph_state_hash`. None moved across the seven-module split, and the conclusion drawn
was that restructuring does not help. That conclusion outran the instrument: a call count
cannot distinguish heavy use of a correct abstraction from a leak through a wrong one,
and `models`/`config` have fan-in 80/42 precisely because they are shared vocabulary.

What replaced it is structure that is wrong regardless of how often it is used -- import
cycles and upward tier edges -- and these tests pin the two properties that make those
worth gating on:

* the cycle metric catches things the tier metric structurally cannot (a same-tier cycle),
* `TYPE_CHECKING` imports are excluded, because deferring an import is how you FIX a cycle
  and a metric that punished the fix would be worse than no metric.

Tests build synthetic graphs rather than reading `src/`. Asserting the live tree has
exactly 3 cycles would make this a change-detector for the tree, not a test of the gate --
`coupling_report.BASELINE` is already that, deliberately, and it is one number to edit.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "verify" / "coupling_report.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("coupling_report", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["coupling_report"] = module
    spec.loader.exec_module(module)
    return module


cr = _load()


# ------------------------------------------------------------------------- cycles
def test_an_acyclic_graph_has_no_components() -> None:
    """The control. Without it, 'report everything as a cycle' passes every other test."""
    assert cr._cycles({"a": {"b"}, "b": {"c"}, "c": set()}) == []


def test_a_two_module_cycle_is_found() -> None:
    assert cr._cycles({"a": {"b"}, "b": {"a"}}) == [["a", "b"]]


def test_a_long_cycle_is_reported_as_one_component_not_many() -> None:
    """The live tree's largest is 8 modules. Reporting that as 8 findings would bury it."""
    found = cr._cycles({"a": {"b"}, "b": {"c"}, "c": {"d"}, "d": {"a"}})
    assert found == [["a", "b", "c", "d"]]


def test_a_self_import_is_not_a_cycle() -> None:
    """A module importing itself is impossible via `_import_graph` (it skips `target == owner`),
    so if one ever appears it is a bug in the graph builder, not a finding to report."""
    assert cr._cycles({"a": {"a"}}) == []


def test_disjoint_cycles_are_counted_separately() -> None:
    found = cr._cycles({"a": {"b"}, "b": {"a"}, "x": {"y"}, "y": {"x"}, "lone": set()})
    assert sorted(found) == [["a", "b"], ["x", "y"]]


def test_the_walk_does_not_recurse_into_the_interpreter_limit() -> None:
    """Tarjan here is iterative, and that was not a style choice.

    The graph includes the facade, which reaches most of the tree; the first recursive
    implementation hit the recursion limit on the real graph. A 2,000-module chain is well
    past any recursive walk and finishes instantly here.
    """
    chain = {f"m{i}": {f"m{i + 1}"} for i in range(2000)}
    chain["m2000"] = {"m0"}
    found = cr._cycles(chain)
    assert len(found) == 1
    assert len(found[0]) == 2001


# --------------------------------------------------------------------------- tiers
def test_every_tiered_module_has_exactly_one_tier() -> None:
    """A module in two tiers would make `RANK` order-dependent and the gate unstable."""
    seen: dict[str, str] = {}
    for tier, modules in cr.TIERS.items():
        for module in modules:
            assert module not in seen, f"{module} is in both {seen[module]} and {tier}"
            seen[module] = tier
    assert set(cr.RANK) == set(seen)


def test_the_tier_order_is_the_declared_one() -> None:
    assert cr.TIER_ORDER == ("core", "infra", "application", "delivery")
    assert [cr.RANK[m] for m in ("models", "storage", "client", "cli")] == [0, 1, 2, 3]


def test_the_facade_is_deliberately_untiered() -> None:
    """Ranking it would manufacture four false violations.

    `admin_server`, `local_platform`, `mcp_server` and `worker` all import the public
    facade, which is exactly what a delivery tier should do. Where the facade IS a problem
    is when a module it imports imports it back — and that is a cycle, which the other
    metric already catches.
    """
    assert cr.FACADE not in cr.RANK


def test_every_top_level_source_module_is_tiered() -> None:
    """An untiered module is invisible to the tier metric — it can neither violate nor be
    violated. If a new top-level module appears, this fails until someone places it, which
    is the point: placement is a decision, not a default."""
    top_level = {
        cr._owner(path) for path in cr.SRC.rglob("*.py") if not path.name.startswith("_") or path.name == "__init__.py"
    }
    untiered = {name for name in top_level if name != cr.FACADE and name not in cr.RANK}
    assert not untiered, f"untiered top-level modules: {sorted(untiered)}"


# ------------------------------------------------------- what is gated, and what is not
def test_the_call_site_counts_are_not_gated() -> None:
    """The whole reason for the rewrite. These must be printed and must not fail a build."""
    for informational in ("reveal sites", "graph_state_hash sites", "client.py storage sites"):
        assert informational in cr.BASELINE, f"{informational} should still be reported"
        assert informational not in cr.GATED, f"{informational} must not gate — see the module docstring"


def test_the_structural_metrics_are_gated() -> None:
    for gated in ("import cycles", "modules in a cycle", "largest cycle", "upward tier edges"):
        assert gated in cr.GATED
        assert gated in cr.BASELINE, "a gated metric with no baseline cannot be compared"


def test_every_gated_metric_has_a_baseline_and_vice_versa() -> None:
    """A gated key absent from BASELINE would KeyError at report time; a baseline key that
    `measure()` never produces would do the same. Both are startup bugs, not findings."""
    assert set(cr.GATED) <= set(cr.BASELINE)
    measured = cr.measure()
    assert set(cr.BASELINE) <= set(measured), f"baseline keys never measured: {set(cr.BASELINE) - set(measured)}"


def test_rising_is_better_only_for_the_two_metrics_where_it_is() -> None:
    """`postgres coverage %` was once printed as `+86.8 WORSE` the first time it had a
    value. A report that calls its own success criterion a regression gets disbelieved
    exactly when it starts working."""
    assert frozenset({"mypy modules strict", "postgres coverage %"}) == cr.HIGHER_IS_BETTER


# ------------------------------------------------------- TYPE_CHECKING is excluded
def test_type_checking_imports_are_excluded_from_the_graph(tmp_path: pathlib.Path) -> None:
    """Deferring an import under TYPE_CHECKING is how a cycle gets FIXED.

    `session.py:36` already does it, which is why `session` appears in the cycle only when
    deferred imports are counted. A metric that punished that fix would push people toward
    leaving the runtime cycle in place.
    """
    package = tmp_path / "memotron"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "alpha.py").write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from memotron.beta import Thing\n"
    )
    (package / "beta.py").write_text("from __future__ import annotations\nfrom memotron.alpha import other\n")

    original = cr.SRC
    try:
        cr.SRC = package
        edges, _ = cr._import_graph()
    finally:
        cr.SRC = original

    assert edges.get("beta") == {"alpha"}, "a runtime import must appear"
    assert "beta" not in edges.get("alpha", set()), "a TYPE_CHECKING import must not"
    assert cr._cycles(edges) == [], "deferring one side of a pair must break the cycle"


@pytest.mark.parametrize("gated_key", ["import cycles", "upward tier edges"])
def test_the_live_tree_is_at_or_under_its_baseline(gated_key: str) -> None:
    """The ratchet itself, so a regression fails the suite and not only the lane.

    Asserts `<=`, never `==`: an improvement must never fail. The lane prints improvements
    and asks for the baseline to be lowered in the same commit.
    """
    assert cr.measure()[gated_key] <= cr.BASELINE[gated_key], (
        f"{gated_key} regressed against the baseline in coupling_report.py"
    )
