"""Shared plumbing for the Memotron demo kit.

Every demo script (``demo_config.py``, ``dream.py``, ``inspect.py``) imports
this module so there is exactly ONE definition of:

  * where the isolated demo graph and workspace live,
  * how the demo platform is constructed (with the deterministic dream agent),
  * the isolation assertion that keeps the demo off the real spymaster graph,
  * the terminal formatting used on screen.

Nothing here writes outside ``demo/``.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import _demopath  # noqa: F401  MUST BE FIRST — unshadows stdlib `inspect`

DEMO_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEMO_DIR.parent

# --- Isolated demo identity -------------------------------------------------
# A dedicated tenant id AND a dedicated graph file.  tenant_id is baked into
# node/relationship identity keys and per-scope content keys, so a different
# tenant in a different file cannot touch anything else on this machine.
DEMO_PROJECT_ID = "memotron-demo"
DEMO_PROJECT_NAME = "Memotron Demo"
DEMO_GRAPH_PATH = DEMO_DIR / ".memotron" / "demo-memory.sqlite"
DEMO_WORKSPACE = DEMO_DIR / "workspace"
SNAPSHOT_DIR = DEMO_DIR / "snapshots"

# Graphs the demo must never open.  The real one is ~150 MB of `spymaster`
# memory in the repository's own .memotron/ directory.
FORBIDDEN_GRAPH_PATHS = (
    (REPO_ROOT / ".memotron" / "spymaster.sqlite").resolve(),
    (REPO_ROOT / ".memotron" / "agent-memory.sqlite").resolve(),
    (Path("~/.memotron/memory.sqlite").expanduser()).resolve(),
)

# The one agent identity the demo uses, matching what `memotron init` pins
# for Claude Code.
DEMO_AGENT_ID = "claude-code"
DEMO_AGENT_NAME = "Claude Code"

WIDTH = 96


# ---------------------------------------------------------------------------
# Terminal formatting (no external deps)
# ---------------------------------------------------------------------------


def banner(title: str, subtitle: str = "") -> None:
    print()
    print("=" * WIDTH)
    print(f" {title}")
    if subtitle:
        print(f" {subtitle}")
    print("=" * WIDTH)


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, WIDTH - len(title) - 5))


def kv(key: str, value: Any, width: int = 30) -> None:
    print(f"  {key:<{width}} {value}")


def bullet(text: str) -> None:
    print(f"  - {text}")


def table(headers: list[str], rows: list[list[str]], indent: str = "  ") -> None:
    """Aligned fixed-width table.  Long cells are truncated, never wrapped."""

    if not rows:
        print(f"{indent}(none)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    # Keep the whole line inside the terminal banner width.
    budget = WIDTH - len(indent) - 2 * (len(headers) - 1)
    while sum(widths) > budget:
        widest = widths.index(max(widths))
        if widths[widest] <= 8:
            break
        widths[widest] -= 1

    def render(cells: list[str]) -> str:
        parts = []
        for cell, width in zip(cells, widths, strict=True):
            text = cell if len(cell) <= width else cell[: width - 1] + "…"
            parts.append(f"{text:<{width}}")
        return indent + "  ".join(parts).rstrip()

    print(render(headers))
    print(indent + "  ".join("-" * w for w in widths))
    for row in rows:
        print(render(row))


def short(uuid: str, size: int = 8) -> str:
    return uuid[:size] if uuid else "-"


def fail(message: str) -> None:
    print(f"\nERROR: {message}\n", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def assert_isolated(graph_path: Path) -> None:
    """Fail loudly if the demo ever resolves onto a non-demo graph."""

    resolved = Path(graph_path).expanduser().resolve()
    if resolved != DEMO_GRAPH_PATH.resolve():
        fail(
            "demo graph path is not the isolated demo graph\n"
            f"  resolved : {resolved}\n"
            f"  expected : {DEMO_GRAPH_PATH.resolve()}"
        )
    for forbidden in FORBIDDEN_GRAPH_PATHS:
        if resolved == forbidden:
            fail(f"demo resolved onto a protected graph: {forbidden}")


def workspace_or_fail() -> Path:
    if not (DEMO_WORKSPACE / ".memotron.yaml").is_file():
        fail(f"the demo workspace is not set up yet\n  run: {DEMO_DIR / 'setup.sh'}")
    return DEMO_WORKSPACE


# ---------------------------------------------------------------------------
# Platform construction
# ---------------------------------------------------------------------------


def demo_extraction_mode() -> str:
    """The single extraction switch, defined in ``demo/demo_config.py``.

    Imported lazily so ``demo_config`` can import this module at the top level
    without a cycle.
    """

    from demo_config import DEMO_EXTRACTION_MODE

    return DEMO_EXTRACTION_MODE


def apply_demo_transports() -> None:
    """Install the demo's transport choices. Idempotent.

    One switch, ``DEMO_EXTRACTION_MODE`` in ``demo/demo_config.py``, decides
    all three transports together:

    * ``deterministic`` — extraction is the hermetic
      ``RuleBasedExtractionTransport``, which parses the
      ``Memory: subject=...; predicate=...; object=...`` lines the demo's
      ``agent_guidance`` tells the agent to publish; the dream agent falls back
      to ``LocalDreamAgentTransport`` (returning ``None`` is what makes
      DreamEngine do that) and theme synthesis is the deterministic label.
      Zero network, zero cost, byte-identical on every rehearsal.
    * ``llm`` — extraction, dream-agent decisions, AND theme synthesis all run
      on the sealed tenant credential through the JedAI Gateway, exactly as the
      packaged runtime wires them.  Nothing is substituted, so what the demo
      shows is what a real deployment does.
    """

    from memotron import runtime

    if getattr(runtime.build_transports_from_tenant_credentials, "_demo_patched", False):
        return

    deterministic = demo_extraction_mode() == "deterministic"
    original_transports = runtime.build_transports_from_tenant_credentials
    original_synthesis = runtime.build_synthesis_transport_from_credentials

    def _demo_transports(credentials: dict[str, Any] | None):
        from memotron.extraction import RuleBasedExtractionTransport

        if deterministic:
            return RuleBasedExtractionTransport(), None
        return original_transports(credentials)

    def _demo_synthesis(credentials: dict[str, Any] | None):
        # No live model -> no LLM theme summary and no session-outcome judge.
        # Both degrade to their documented defaults: deterministic theme labels
        # and an explicit `no_judge_configured` skip.
        if deterministic:
            return None
        return original_synthesis(credentials)

    _demo_transports._demo_patched = True  # type: ignore[attr-defined]
    runtime.build_transports_from_tenant_credentials = _demo_transports
    runtime.build_synthesis_transport_from_credentials = _demo_synthesis


def load_repo_env() -> None:
    """Load the gitignored repository ``.env`` (names only are ever printed)."""

    from memotron.runtime import load_env_file

    load_env_file(REPO_ROOT / ".env")


def build_demo_platform():
    """Build the demo ``AgentMemoryPlatform`` from the demo workspace config.

    Uses the exact same entry point (``build_platform_from_project``) that
    ``memotron mcp`` and the Claude Code hooks use, so what these scripts
    see is what the live session sees.
    """

    load_repo_env()
    apply_demo_transports()

    from memotron.adoption import build_platform_from_project

    workspace = workspace_or_fail()
    platform, config = build_platform_from_project(workspace)
    assert_isolated(config.graph_path())
    platform.register_agent(
        agent_id=DEMO_AGENT_ID,
        agent_name=DEMO_AGENT_NAME,
        source="demo-script",
    )
    return platform, config


def scopes_for(platform) -> dict[str, Any]:
    """The three scopes the demo talks about, by display name."""

    return {
        "project": platform.project_scope,
        "personal": platform.user_scope,
        "continuity": platform.agent_scope(DEMO_AGENT_ID),
    }


def terminal_width_note() -> None:
    columns = shutil.get_terminal_size((WIDTH, 24)).columns
    if columns < WIDTH:
        print(f"  (tip: widen the terminal to >= {WIDTH} columns for aligned output; currently {columns})")


def env_var_present(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())
