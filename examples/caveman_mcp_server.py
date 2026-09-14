"""Entry point for the caveman-memory MCP server (#251 amendment D).

Serves the ten caveman tools over Streamable HTTP:
  POST /mcp
  GET  /health

Ten tools, every one returning plain text: memory_contract, memory_brief,
memory_read, memory_node, memory_neighbors, memory_explain, memory_ingest,
memory_dream, memory_erase, memory_replay. `memory_contract` returns the shipped
`AGENTS.md`, which is the whole contract an agent needs.

Environment variables
---------------------
CAVEMAN_LEDGER_PATH      sqlite file holding BOTH append-only streams -- the claim
                         ledger and the event journal (default:
                         .memotron/caveman.sqlite, relative to the cwd; its
                         directory is created on the first tool call if absent).
                         The graph itself is in memory and is a bounded
                         compression of these, so this file is the durable state.
CAVEMAN_MOTIVE           engineering | assistant (default: engineering). The
                         persona whose rubric drives extraction and whose budgets
                         bound a read. A name outside those two is a hard failure
                         at startup, not a fallback.
CAVEMAN_CHAT_MODEL       Undated gateway model alias for extract, reconcile and
                         dream (default: claude-haiku-4-5).
CAVEMAN_EMBEDDING_MODEL  Gateway embedding model (default: text-embedding-3).
LITELLM_API_KEY          JedAI Gateway virtual key. `main()` first loads the
                         repo-root gitignored `.env` (never overriding a value
                         already in the process environment), then the key is
                         resolved at CALL time -- so this process starts without
                         one, and memory_read, memory_ingest and memory_dream then
                         each answer with one line naming this variable. There is
                         no offline mode.
LITELLM_API_BASE         Gateway base URL, including /v1 (default:
                         https://preview.jedai-gateway.wdprapps.disney.com/v1).
MCP_HOST                 Bind host (default: 127.0.0.1), read by `main()` through
                         `memotron.runtime.mcp_bind_from_env`.
MCP_PORT                 Bind port (default: 8020).
MEMOTRON_MCP_ALLOWED_HOSTS
                         Comma-separated extra Host/Origin values for the
                         DNS-rebinding allowlist, or `*` to disable the
                         protection explicitly. Localhost is always allowed. Set
                         this to the ingress hostname when deploying behind one,
                         or every MCP request gets 421 while /health stays 200.
LOG_LEVEL                DEBUG | INFO | WARNING | ERROR (default: INFO).

Usage
-----
  uv run examples/caveman_mcp_server.py
  (with LITELLM_API_KEY exported, or in the repo-root gitignored .env)

Codex CLI (streamable HTTP, this process must already be running):
  codex mcp add caveman-memory --url http://127.0.0.1:8020/mcp
  then on that table set tool_timeout_sec = 600 (ingest and dream outlive the
  60 s default) and default_tools_approval_mode = "approve" (these tools carry
  no readOnlyHint annotation, so Codex otherwise asks before every call and
  `codex exec` refuses them outright), and copy
  src/memotron/caveman/guidance/SKILL.md to ~/.codex/skills/caveman-memory/SKILL.md
  so `$caveman-memory` routes the verbs.
"""

from __future__ import annotations

import os
from pathlib import Path

from memotron.caveman.mcp import mcp
from memotron.runtime import load_env_file, mcp_bind_from_env, serve_mcp_http

MCP_HOST = "127.0.0.1"
MCP_PORT = 8020
REPO_ROOT = Path(__file__).resolve().parents[1]
"""Anchors the ``.env`` lookup to this checkout, never to the caller's cwd (T1-14)."""


def main() -> None:
    """Serve over HTTP through the repo's one supported path.

    No ``build_*_from_env()`` call first: ``memotron.caveman.mcp`` builds its
    runtime lazily on the first tool call, so a process that is started and never
    called opens no sqlite file and resolves no credential.

    Bind address, ``Host`` allowlist and the uvicorn server are all decided inside
    ``memotron.runtime.serve_mcp_http`` from one call, so they cannot disagree --
    the disagreement that once bound ``0.0.0.0`` while accepting only localhost
    ``Host`` headers (421 on every ingress request, 200 on ``/health``) is not
    expressible. No identity middleware: caveman memory takes its scope as a call
    argument and has no per-caller principal, so there is nothing for a wrapper
    to resolve.

    The repo-root ``.env`` is loaded first, the way ``examples/caveman_demo.py``
    loads it: anchored to this file, and never overriding a value the process
    already carries. A server launched by a tool that does not export the key --
    a Codex or Claude MCP client, a ``nohup`` from another directory -- therefore
    reaches the same gateway the demo does, from the same gitignored file.
    """
    load_env_file(REPO_ROOT / ".env")
    host, port = mcp_bind_from_env(default_host=MCP_HOST, default_port=MCP_PORT)
    serve_mcp_http(mcp, host=host, port=port, log_level=os.environ.get("LOG_LEVEL", "INFO"))


if __name__ == "__main__":
    main()
