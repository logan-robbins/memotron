"""Demo-only launcher for the Memotron CLI.

Nothing under ``src/memotron/`` is modified by the demo. This shim exists so
that the MCP server and the Claude Code lifecycle hooks — which Claude Code
launches as separate processes — get the demo's transport choice, which is the
single ``DEMO_EXTRACTION_MODE`` switch in ``demo/demo_config.py``:

* ``deterministic`` — hermetic ``RuleBasedExtractionTransport`` for extraction,
  ``LocalDreamAgentTransport`` for dream-agent decisions, deterministic theme
  labels. Free, offline, byte-identical on every rehearsal.
* ``llm`` — extraction, dream-agent decisions, and theme synthesis all run on
  the sealed tenant credential through the JedAI Gateway, with nothing
  substituted.

The swap lives in ``demo/_demo.py`` so the in-process demo scripts and these
out-of-process launches behave identically.

Invoked through ``demo/bin/dw-demo``, which is what the demo workspace's
``.mcp.json`` and ``.claude/settings.json`` hooks point at.
"""

from __future__ import annotations

import sys

import _demopath  # noqa: F401  MUST BE FIRST — unshadows stdlib `inspect`
from _demo import apply_demo_transports, load_repo_env


def main() -> None:
    load_repo_env()
    apply_demo_transports()

    from memotron.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    sys.exit(main())
