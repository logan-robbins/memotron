#!/usr/bin/env python3
"""P1 -- the importable surface of the package, as a blessable golden file.

    uv run python scripts/verify/api_surface.py            # print the surface
    uv run python scripts/verify/api_surface.py --bless    # rewrite the golden
    uv run python scripts/verify/api_surface.py --check    # diff against it

``tests/test_api_surface.py`` runs ``--check`` inside the ordinary pytest run, so
this reaches CI through the existing ``pytest -q`` with no pipeline change.

What it answers
---------------
``pure_move.py`` proves *nothing changed*. This proves *nothing disappeared* --
the other half of the split's contract, and the half a bytecode comparison
cannot see, because a symbol that stops being importable still has identical
bytecode wherever it now lives.

It is a RUNTIME check, deliberately. It imports each module and reads the
composed result, so it sees what a mixin split actually produces: whether
``DreamEngine`` still has all 153 methods after they are spread across ten
mixins. A source-level AST scan cannot answer that.

It also sees through the ``try/except ImportError: pass`` around ``replay`` in
``memotron/__init__.py``. An import error introduced there is swallowed, and
``import memotron`` keeps succeeding with names silently missing from the
namespace; those names are in the golden, so they go red.

What it deliberately does NOT compare
-------------------------------------
``__module__``, ``__qualname__`` and the MRO name list. All three change under a
*correct* mixin split, so including them would turn the golden red on every
correct commit -- and a golden that is red on every commit gets blessed
reflexively and stops being a guard. Ignoring ``__module__`` is safe here
because nothing in ``src/`` or ``tests/`` pickles or deep-copies these classes.

The MRO shadow check
--------------------
The one thing this checks that is not a diff: no attribute may be defined by
more than one split-introduced mixin. When two mixins define the same private
helper, the leftmost silently wins, forever, and no test notices. There is real
exposure in ``client.py``, whose shared-helper block fans in from about five
clusters.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import pkgutil
import re
import sys
from pathlib import Path
from typing import Any

GOLDEN = Path(__file__).resolve().parents[2] / "tests" / "api_surface.golden.txt"
PACKAGE = "memotron"

# Bases whose members are inherited machinery, not this package's surface.
_UNINTERESTING_BASES = {"object", "BaseModel", "Protocol", "Generic", "BaseHTTPRequestHandler"}


def _iter_modules() -> list[str]:
    package = importlib.import_module(PACKAGE)
    names = [PACKAGE]
    names.extend(info.name for info in pkgutil.walk_packages(package.__path__, prefix=f"{PACKAGE}."))
    return sorted(names)


#: Default values whose repr embeds an id() -- e.g.
#: `default=<dataclasses._MISSING_TYPE object at 0x101420500>`. The address is
#: different every process, so it must be scrubbed or the golden can never match.
_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")

#: `<class 'pkg.mod.Name'>` in a pydantic field annotation. Captured so the module
#: path can be dropped -- see the note in _pydantic_fields.
_CLASS_REPR = re.compile(r"<(?:class|enum) '([\w.]+)'>")


def _signature(obj: Any) -> str:
    try:
        return _ADDRESS.sub("", str(inspect.signature(obj)))
    except (TypeError, ValueError):
        return "(?)"


def _kind(obj: Any) -> str:
    if inspect.isclass(obj):
        return "class"
    if inspect.iscoroutinefunction(obj):
        return "async def"
    if inspect.isfunction(obj) or inspect.ismethod(obj):
        return "def"
    if isinstance(obj, str | int | float | bool | bytes | frozenset | tuple | type(None)):
        return type(obj).__name__
    return type(obj).__name__


def _class_members(cls: type) -> list[str]:
    """Members contributed by this package, collected across the composed MRO.

    Walking ``__mro__`` rather than ``vars(cls)`` is the point: after a mixin
    split the methods live on the bases, and only the composed view shows that
    the class still offers all of them.
    """
    seen: dict[str, str] = {}
    for base in cls.__mro__:
        if base.__name__ in _UNINTERESTING_BASES:
            continue
        if not getattr(base, "__module__", "").startswith(PACKAGE):
            continue
        for name, member in vars(base).items():
            if name.startswith("__") and name.endswith("__"):
                continue
            if name in seen:
                continue  # first definition in MRO order wins, as Python does
            seen[name] = f"{_kind(member)}{_signature(member) if callable(member) else ''}"
    return [f"  {name} {desc}" for name, desc in sorted(seen.items())]


def _pydantic_fields(cls: type) -> list[str]:
    """Field name, type, default and constraints for a pydantic model.

    THE REASON THIS EXISTS. ``pure_move.py`` fingerprints function bytecode, and a
    pydantic model's fields are class-level statements, not functions -- so a
    changed default, a loosened ``ge=``/``le=`` bound or a swapped
    ``default_factory`` passes it untouched. Measured 2026-08-27 by mutating
    ``GrowthPolicy.cosine_threshold`` 0.88 -> 0.99 in an already-moved module:
    pure_move PASS, this golden PASS, ruff PASS, mypy PASS -- only the test suite
    noticed. That made the suite the sole guard for exactly the content that
    dominates models/ and config/, which is the weakest place for it to be.

    Recording the fields closes it. A moved model whose defaults are intact emits
    an identical block; one that drifted does not.
    """
    fields = getattr(cls, "model_fields", None)
    if not isinstance(fields, dict):
        return []
    out: list[str] = []
    for name, field in sorted(fields.items()):
        # `<class 'memotron.config._policies.GrowthPolicy'>` -> `GrowthPolicy`.
        # The module path is stripped for the same reason __module__ is not compared
        # anywhere else here: it changes under a CORRECT move, and a golden that
        # reddens on every correct commit stops being read.
        annotation = _CLASS_REPR.sub(lambda m: m.group(1).rsplit(".", 1)[-1], str(field.annotation))
        parts = [f"    .{name}: {annotation}"]
        if field.default_factory is not None:
            factory = getattr(field.default_factory, "__name__", repr(field.default_factory))
            parts.append(f"factory={factory}")
        elif field.is_required():
            parts.append("required")
        else:
            parts.append(f"default={field.default!r}")
        if field.metadata:
            # Ge(ge=1), MaxLen(max_length=8), ... -- the validation constraints.
            parts.append("constraints=" + ",".join(sorted(repr(m) for m in field.metadata)))
        out.append(" ".join(parts))
    return out


def _mro_shadows(cls: type) -> list[str]:
    """Names defined by two mixins that are SIBLINGS -- neither an ancestor of the other.

    The failure this exists for: two concern mixins both define ``_helper``, the
    leftmost silently wins, and nothing anywhere says so. That is only possible
    between INDEPENDENT bases.

    A subclass overriding a name its own parent defines is not that -- it is ordinary
    inheritance, and reporting it is a false positive. `agent_memory/_results.py` made
    that concrete: `ProjectMemoryConfig(ProjectMemorySettings)` is plain pydantic
    subclassing, and pydantic generates `model_config` and `_abc_impl` on EVERY
    BaseModel, so the old walk flagged two names on a relationship that is completely
    normal. A gate with false positives is one people learn to bless past, which is
    worse than no gate -- so the fix is the check, not the code.

    Restricted to bases in a private submodule so legitimate overrides of
    ``StorageBackend`` or ``BaseHTTPRequestHandler`` are never reported.
    """
    owners: dict[str, list[type]] = {}
    for base in cls.__mro__:
        module = getattr(base, "__module__", "")
        if not module.startswith(PACKAGE):
            continue
        if not any(part.startswith("_") for part in module.split(".")[1:]):
            continue
        for name in vars(base):
            if name.startswith("__") and name.endswith("__"):
                continue
            owners.setdefault(name, []).append(base)

    out: list[str] = []
    for name, bases in sorted(owners.items()):
        siblings = [
            (a, b) for i, a in enumerate(bases) for b in bases[i + 1 :] if not issubclass(a, b) and not issubclass(b, a)
        ]
        if not siblings:
            continue
        named = ", ".join(f"{b.__module__}.{b.__name__}" for b in bases)
        out.append(f"{cls.__module__}.{cls.__name__}.{name} defined by {named}")
    return out


#: Members that MUST be defined by the composed class itself and by no mixin.
#:
#: This is the highest-consequence invariant in the client split, and the reason it
#: gets a check of its own rather than relying on `_mro_shadows`: if one of these is
#: shadowed by a mixin, or quietly relocated into one, the failure mode is a SILENT
#: AUTHORISATION BYPASS. Nothing crashes. The suite stays green. `_require_authorized_scope`
#: alone has 65 call sites, and a shadow that returns without raising turns every one
#: of them into an unguarded entry point.
#:
#: `_mro_shadows` catches two mixins defining the same name. It cannot catch the case
#: that matters here -- exactly ONE mixin defining it, with the composer's copy gone --
#: because from the MRO's point of view that is a perfectly ordinary single definition.
#:
#: WHAT THIS CHECK DOES NOT COVER, stated because the first draft of this comment
#: overstated it and an independent review said so: it verifies OWNERSHIP, not BEHAVIOUR.
#: A guard that stays right here on the composer and is gutted to `return None` passes
#: this check. Only `pure_move` (bytecode identity) and the permutation tests cover that,
#: and at the time of writing the permutation tests do not exist -- see TAKEOVER-BACKLOG
#: T2-GUARD-TESTS. So: this rules out relocation and shadowing, and nothing else.
#:
#: `_checkpoint_operator_run` earned its place the hard way: the first cut of the split
#: left it in `_runtime` while its twin `_begin_operator_run` stayed on the composer, and
#: nine mixins were reaching sideways into a leaf for their audit checkpoint. Every gate
#: passed. An R-A2 sweep found it, not a test.
GUARD_MEMBERS = (
    "_require_authorized_scope",
    "_require_explicit_authorized_scope",
    "_check_scope_not_read_only",
    "_begin_operator_run",
    "_checkpoint_operator_run",
)
GUARDED_CLASS = "memotron.client.Memotron"


def _guard_owner_violations() -> list[str]:
    """Is every scope/operator guard still owned by the composer, and only by it?"""
    module_name, _, class_name = GUARDED_CLASS.rpartition(".")
    cls = getattr(importlib.import_module(module_name), class_name, None)
    if cls is None:  # pragma: no cover - only if the class is renamed
        return [f"{GUARDED_CLASS} does not exist -- the guard check is not running"]

    out: list[str] = []
    for name in GUARD_MEMBERS:
        owners = [b for b in cls.__mro__ if name in vars(b)]
        if not owners:
            out.append(f"{name}: DEFINED NOWHERE on {class_name} -- 65 call sites for the first one")
            continue
        if owners[0] is not cls:
            where = f"{owners[0].__module__}.{owners[0].__name__}"
            out.append(f"{name}: resolves to {where}, NOT {class_name} itself")
        extra = [b for b in owners if b is not cls]
        if extra:
            named = ", ".join(f"{b.__module__}.{b.__name__}" for b in extra)
            out.append(f"{name}: also defined by {named}")
    return out


def build_surface() -> tuple[str, list[str]]:
    module_lines: list[str] = []
    class_blocks: dict[str, list[str]] = {}
    shadows: list[str] = []

    for module_name in _iter_modules():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # a module that cannot import IS the finding
            module_lines.append(f"{module_name} !! IMPORT FAILED: {type(exc).__name__}: {exc}")
            continue
        for name, obj in sorted(vars(module).items()):
            if name.startswith("__"):
                continue
            if inspect.ismodule(obj):
                continue
            if inspect.isclass(obj) and obj.__module__.startswith(PACKAGE):
                members = _pydantic_fields(obj) + _class_members(obj)
                body = "\n".join(members)
                digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
                # Keyed by DIGEST, not by f"{__module__}.{__name__}". Keying on the
                # module contradicted this file's own rule about not comparing
                # __module__: moving a class to a submodule re-keyed its block, so an
                # 18-class extraction produced a ~250-line diff of pure churn. A
                # golden that reddens on every correct commit gets blessed
                # reflexively. With a content key, a moved-but-unchanged class emits
                # a byte-identical block, and the MODULES line above still pins where
                # the class is importable from -- which is the question that matters.
                class_blocks[f"{digest} {obj.__name__}"] = members
                module_lines.append(f"{module_name}::{name} class {obj.__name__} {digest}")
                shadows.extend(_mro_shadows(obj))
            elif callable(obj):
                module_lines.append(f"{module_name}::{name} callable{_signature(obj)}")
            else:
                module_lines.append(f"{module_name}::{name} {_kind(obj)}")

    out = ["# MODULES", *sorted(set(module_lines)), "", "# CLASSES"]
    for key in sorted(class_blocks):
        out.append(f"class {key}")
        out.extend(class_blocks[key])
    return "\n".join(out) + "\n", sorted(set(shadows))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bless", action="store_true", help="rewrite the golden file")
    parser.add_argument("--check", action="store_true", help="diff against the golden file")
    args = parser.parse_args(argv)

    surface, shadows = build_surface()

    if args.bless:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(surface, encoding="utf-8")
        print(f"blessed {GOLDEN} ({len(surface.splitlines())} lines, {len(surface) // 1024} KB)")
        if shadows:
            print(f"WARNING: {len(shadows)} MRO shadow(s) -- blessing does not make these acceptable")
        return 0

    if args.check:
        import difflib

        if not GOLDEN.exists():
            print(f"golden missing: {GOLDEN}\nrun: uv run python {Path(__file__).name} --bless")
            return 2
        expected = GOLDEN.read_text(encoding="utf-8")
        problems = 0
        if expected != surface:
            diff = list(
                difflib.unified_diff(
                    expected.splitlines(),
                    surface.splitlines(),
                    fromfile="golden",
                    tofile="actual",
                    lineterm="",
                    n=1,
                )
            )
            removed = [d for d in diff if d.startswith("-") and not d.startswith("---")]
            added = [d for d in diff if d.startswith("+") and not d.startswith("+++")]
            print(f"API SURFACE CHANGED: {len(removed)} line(s) gone, {len(added)} added")
            for line in diff[:60]:
                print("   ", line)
            if len(diff) > 60:
                print(f"    ... and {len(diff) - 60} more diff lines")
            print("\nIf every change is intended, re-bless:")
            print(f"    uv run python scripts/verify/{Path(__file__).name} --bless")
            problems += 1
        if shadows:
            print(f"\nMRO SHADOWING: {len(shadows)} name(s) defined by more than one mixin")
            for line in shadows[:30]:
                print("   ", line)
            print("The leftmost base silently wins. Move the helper onto the composed class.")
            problems += 1
        guards = _guard_owner_violations()
        if guards:
            print(f"\nGUARD OWNERSHIP: {len(guards)} scope/operator guard(s) are not owned by the composer")
            for line in guards:
                print("   ", line)
            print("A shadowed or relocated scope guard is a SILENT authorisation bypass:")
            print("nothing raises, nothing fails, and every call site behind it is unguarded.")
            problems += 1
        if problems:
            return 1
        print(
            f"API SURFACE: PASS ({len(surface.splitlines())} lines, no MRO shadowing, "
            f"{len(GUARD_MEMBERS)} guards owned by the composer)"
        )
        return 0

    sys.stdout.write(surface)
    if shadows:
        sys.stderr.write(f"\n{len(shadows)} MRO shadow(s)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
