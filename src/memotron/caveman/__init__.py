"""Caveman memory: a bounded compressed node graph over an append-only ledger.

A self-contained subsystem (issue #251, ``docs/design/251-caveman-memory.md``).
It does **not** extend the typed pipeline in ``memotron.dreaming`` — no
``MemoryType``, no truth slots, no supersession gates, no quarantine — and
nothing outside this package imports it.

Three invariants the module layout enforces by construction:

* **The dreamer is the only writer of fact text and the only enforcer of N and M.**
  ``extract`` only appends to the ledger; ``reconcile`` only routes, marks dirty
  and records provisional typed relations -- the type comes from the scope's
  bounded vocabulary or is a new ``UPPER_SNAKE`` word the matcher coins, and the
  claim is the ledger claim verbatim; only ``dream`` calls ``replace_facts`` /
  ``merge_nodes`` / ``split_node`` and only it compresses an edge's claim or
  holds the edge-type ceiling.
* **The graph is fully regenerable from the ledger.** Erasure is "delete the
  episode's entries, re-dream the nodes they touched".
* **Motive enters at extract and dream, never at reconcile.** Stage 2 is the
  persona-independent truth layer: one graph per scope, motive is a policy over
  it rather than a per-motive graph.

Five leaf seams are reused from the wider tree and nothing else:
:mod:`memotron.gateway` (chat transport base, retry policy, endpoint
defaults), :mod:`memotron.synthesis` (the ``SynthesisTransport`` Protocol and
``strip_markdown_fences``), :mod:`memotron.embedding`
(``EmbeddingTransport``, ``LocalEmbeddingTransport``, ``cosine_similarity``),
and ``memotron.extraction.parse_first_json_object``. Serving the MCP surface
over HTTP is not the package's: ``examples/caveman_mcp_server.py`` hands the
:mod:`memotron.caveman.mcp` object to ``memotron.runtime.serve_mcp_http``,
the repo's single answer to binding a host and a ``Host`` allowlist together.

**There are deliberately no re-exports here.** Every consumer imports the
submodule it needs (``from memotron.caveman.render import render_node``), which
keeps ``tests/api_surface.golden.txt`` churn confined to the modules that
actually change and means no work package has to touch this file.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
