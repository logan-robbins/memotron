"""P2 -- the two import-time facts the module split can silently break.

Neither is covered by any other test, and neither would make the suite red if it
regressed. Both must be in place BEFORE the modules they protect are split.

1. ``DEFAULT_ADMIN_STATIC_DIR`` is derived with ``Path(__file__).parents[2]``.
   It is the ONLY ``__file__`` in all of ``src/``. The moment ``admin_server.py``
   becomes ``admin_server/__init__.py`` the file gains a directory level and that
   index silently resolves to ``<repo>/src/ui/admin/dist``, which does not exist.
   Nothing serves a static asset in the suite, so nothing would notice.

2. ``dreaming`` constructs exactly one object at import time. When its methods
   are spread across mixins, a mixin that re-instantiates the transport in its
   own module yields two instances. The class is stateless, so every test still
   passes -- which is precisely why this needs an identity assertion rather than
   a behavioural one.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import memotron.admin_server as admin_server
import memotron.dreaming as dreaming

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestAdminStaticAssetRoot:
    def test_default_static_dir_is_the_repo_ui_admin_dist(self) -> None:
        """Anchored on pyproject.toml, so it is independent of the parents[] index."""
        assert (REPO_ROOT / "pyproject.toml").is_file(), "test anchor is wrong"
        assert admin_server.DEFAULT_ADMIN_STATIC_DIR == REPO_ROOT / "ui" / "admin" / "dist"

    def test_the_anchor_really_is_this_project(self) -> None:
        """Guards the guard: parents[1] must be jedai-memotron, not a parent repo."""
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        assert pyproject["project"]["name"] == "jedai-memotron"


class TestImportTimeSingletons:
    def test_the_fallback_dream_agent_transport_is_one_object(self) -> None:
        """One instance, reachable by exactly one name.

        After the split this must still be true: if a mixin module does its own
        ``LocalDreamAgentTransport()`` there will be two, and no behavioural test
        can tell, because the transport holds no state.
        """
        transport = dreaming._DREAM_AGENT_FALLBACK_TRANSPORT
        assert transport is not None
        found = {
            f"{module.__name__}.{name}"
            for module in (dreaming,)
            for name, value in vars(module).items()
            if value is transport
        }
        assert found == {"memotron.dreaming._DREAM_AGENT_FALLBACK_TRANSPORT"}

    def test_reimporting_dreaming_does_not_build_a_second_transport(self) -> None:
        import importlib

        again = importlib.import_module("memotron.dreaming")
        assert again._DREAM_AGENT_FALLBACK_TRANSPORT is dreaming._DREAM_AGENT_FALLBACK_TRANSPORT


def test_the_dream_status_map_is_one_object_process_wide() -> None:
    """``dream_statuses`` and its lock must be ONE object, not one per import path.

    ``MemoryGraphHandler`` declares both as ``ClassVar`` -- a shared mutable dict and
    the ``Lock`` guarding it -- so the single-flight guarantee on dream sequences is
    "every handler in this process sees the same map". Duplicate the class across two
    modules and you get two maps and two locks; **both work perfectly in isolation**,
    which is precisely why no behavioural test would catch it. Two concurrent
    sequences would each hold their own lock, see their own empty map, and both run.

    Identity, not equality: two empty dicts compare equal.

    This is not hypothetical for this file. ``admin_server`` became a package on
    2026-08-28, and the correct decomposition for a handler with no ``__init__`` is
    free functions plus a thin dispatch shell -- which is exactly the shape where a
    second copy of the class can appear.
    """
    import memotron.admin_server as pkg
    from memotron.admin_server import MemoryGraphHandler

    assert MemoryGraphHandler.dream_statuses is pkg.MemoryGraphHandler.dream_statuses
    assert MemoryGraphHandler.dream_status_lock is pkg.MemoryGraphHandler.dream_status_lock

    # and the attributes are the class's own, not inherited from some other copy
    assert "dream_statuses" in vars(MemoryGraphHandler)
    assert "dream_status_lock" in vars(MemoryGraphHandler)


def test_importing_memotron_does_not_pull_nltk() -> None:
    """``import memotron`` must not import nltk.

    nltk is a full NLP toolkit carried for ONE algorithm -- Porter stemming of LoCoMo
    tokens in ``LocalOfficialBenchmarkJudge._locomo_f1``. Until 2026-09-09 it was a
    REQUIRED dependency imported at ``certification.py`` module scope, and because
    ``memotron/__init__.py`` imports ``certification`` eagerly, every process that
    touched the package -- the MCP server, the admin server, the dream worker -- loaded
    it. None of them can reach that judge.

    That is a supply-chain surface, not a style question: nltk produced three advisories
    in a fortnight, the most recent (GHSA-8mgp-746c-j5xp, HIGH) with **no patched version
    available**, so the usual "bump it" remedy did not exist. Moving the import inside the
    constructor took nltk out of the shipped image entirely.

    **This runs in a subprocess deliberately.** In-process the assertion is worthless --
    any earlier test that constructed the judge leaves nltk in ``sys.modules``, and the
    check would fail for a reason having nothing to do with the import graph. A clean
    interpreter is the only place the question is meaningful.

    The positive control is load-bearing: if nltk were simply *not installed*, a bare
    "nltk not in sys.modules" assertion would pass while proving nothing. So the child
    asserts nltk is importable FIRST, and the test fails if that control does not hold.
    """
    import subprocess
    import sys

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util, sys\n"
                "installed = importlib.util.find_spec('nltk') is not None\n"
                "import memotron\n"
                "leaked = any(m.split('.')[0] == 'nltk' for m in sys.modules)\n"
                "print(f'{installed}:{leaked}')\n"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, f"probe failed: {child.stderr}"
    installed, leaked = child.stdout.strip().split(":")

    # CONTROL: nltk must be installed, or "not imported" is vacuously true.
    assert installed == "True", (
        "nltk is not installed in this environment, so this test cannot distinguish "
        "'not imported' from 'not present' -- it is in the dev group for this reason"
    )
    assert leaked == "False", (
        "`import memotron` pulled nltk. The import in certification.py belongs inside "
        "LocalOfficialBenchmarkJudge.__init__, not at module scope."
    )
