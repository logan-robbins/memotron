"""Why does gateway extraction yield nothing where rule-based yields a fact?

D-45 observed `relationships = []` from a post-compact episode once a cwd `.env` routed
extraction to the JedAI Gateway (T1-14), while the deterministic rule-based path produced
the expected HAS_STATE. Two explanations were open:

  H1  the gateway call fails and the error is swallowed
  H2  the call succeeds and extraction genuinely returns no candidates

H1 is already ruled out at the transport level: `POST $LITELLM_API_BASE/chat/completions`
returns 200 with a valid key. This probe settles it at the APPLICATION level by feeding the
SAME episode text to BOTH transports and printing what each returns, plus any exception.

**Holds one variable: the transport.** Same text, same request, same process.

    set -a; . ./.env; set +a
    uv run python scripts/verify/probe_extraction_transport.py

exit 0 = both transports produced candidates · 1 = a transport produced none · 2 = error
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback

# The exact compact_summary from
# tests/test_adoption.py::test_post_compact_hook_persists_only_sanitized_continuity
EPISODE = "Implement simple mode. password=do-not-store-this and notify engineer@example.com."

# A richer, more realistic compaction summary — a one-line episode may simply be too thin
# to extract from, which would be a property of the INPUT rather than of the transport.
EPISODE_RICH = (
    "Session summary: we decided the Memotron operational store will be Postgres rather "
    "than SQLite for the latest environment. Ryan requires that the KEK come from Cloud KMS, "
    "not an ephemeral per-process key. The team should run the parity gate before any "
    "cutover. Blocker: the agent-memory surface cannot reach Postgres today."
)


async def run(label: str, transport, text: str) -> tuple[str, int, str]:
    from memotron.extraction import ExtractionRequest

    kwargs = {"episode_body": text}
    req = None
    for attempt in (
        lambda: ExtractionRequest(**kwargs),
        lambda: ExtractionRequest(episode_body=text, instructions=None),
        lambda: ExtractionRequest(text=text),
    ):
        try:
            req = attempt()
            break
        except Exception:
            continue
    if req is None:
        return (
            label,
            -1,
            f"could not build ExtractionRequest; fields={list(getattr(ExtractionRequest, 'model_fields', {}))}",
        )

    t0 = time.perf_counter()
    try:
        out = await asyncio.wait_for(transport.extract_memories(req), timeout=120)
    except Exception as e:
        return label, -1, f"{type(e).__name__}: {e}"
    ms = (time.perf_counter() - t0) * 1000

    if hasattr(out, "memories"):
        items = list(out.memories or [])
        shape = "CookbookEnvelope"
    else:
        items = list(out or [])
        shape = "list"
    preview = json.dumps(items[:2], default=str)[:200] if items else "(empty)"
    return label, len(items), f"{shape} in {ms:.0f}ms  {preview}"


async def main() -> int:
    from memotron.extraction import RuleBasedExtractionTransport
    from memotron.runtime import build_transports_from_env

    print(
        f"  LITELLM_API_KEY set: {bool(os.environ.get('LITELLM_API_KEY'))}   "
        f"LITELLM_API_BASE set: {bool(os.environ.get('LITELLM_API_BASE'))}\n"
    )

    gateway, _dream = build_transports_from_env()
    rule = RuleBasedExtractionTransport()
    print(f"  transport from env : {type(gateway).__name__}")
    print(f"  rule-based         : {type(rule).__name__}")
    if type(gateway).__name__ == type(rule).__name__:
        print("\n  NOTE: env produced the rule-based transport — no gateway credential in scope.")
        print("  Run with: set -a; . ./.env; set +a")

    rows = []
    for text, tag in ((EPISODE, "test episode (1 line)"), (EPISODE_RICH, "realistic compaction summary")):
        print(f"\n  === {tag} ===")
        for label, transport in (("rule-based", rule), ("from-env (gateway)", gateway)):
            lbl, n, detail = await run(label, transport, text)
            rows.append((tag, lbl, n))
            mark = "ERROR" if n < 0 else ("NONE " if n == 0 else f"{n:<5}")
            print(f"    {lbl:20s} candidates={mark} {detail[:150]}")

    empties = [(t, line) for t, line, n in rows if n <= 0]
    print(f"\n=== extraction: {'FAIL' if empties else 'PASS'} ===")
    for text, label in empties:
        print(f"  - {label} produced no candidates for: {text}")
    return 1 if empties else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        sys.exit(2)
