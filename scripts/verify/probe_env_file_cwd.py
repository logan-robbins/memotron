"""T1-14: do the env-driven builders read a `.env` from whatever directory they run in?

Filed claim: `runtime.py:177,232,320,385` call the bare `load_env_file()`, whose default is the
cwd-relative `".env"` (`runtime.py:34,92`). Two consequences, both measured here:

  1. Credentials are picked up from an unrelated directory, silently promoting deterministic
     rule-based extraction to a gateway-billed call.
  2. It writes into `os.environ`, so it DEFEATS EXPLICIT CLEARING — deleting `LITELLM_API_KEY`
     does not stop it being re-read from disk a moment later. That is why PR #44's
     `monkeypatch.delenv(...)` hardening could not work.

Hermetic and offline: these builders only CONSTRUCT transports, they never call the gateway, so
the returned type is a pure signal and the fake key below is never sent anywhere.

The only variable that changes between arms is the process working directory.

    uv run python scripts/verify/probe_env_file_cwd.py

exit 0 = `.env` is not read from cwd  ·  1 = it is (T1-14 live)  ·  2 = precondition failed
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

os.environ.setdefault("MEMOTRON_ALLOW_EPHEMERAL_KEK", "1")

from memotron.extraction import RuleBasedExtractionTransport
from memotron.runtime import (
    build_embedding_transport_from_env,
    build_synthesis_transport_from_env,
    build_transports_from_env,
    load_env_file,
    seed_tenant_llm_credentials_from_env,
)

# Never a real credential. It is only ever compared as a string; no request is made with it.
FAKE_KEY = "probe-t114-not-a-real-key"  # pragma: allowlist secret

# Every variable the stray .env sets or that _env_endpoint reads. Anything missing here leaks
# from one arm into the next and quietly couples them -- the exact defect this probe exists for.
KEY_VARS = (
    "LITELLM_API_KEY",
    "LITELLM_API_BASE",
    "OPENAI_API_KEY",
    "OPENAI_API_URL",
    "MEMOTRON_LLM_MODEL",
    "MEMOTRON_DREAM_AGENT_MODEL",
    "MEMOTRON_EMBEDDING_PROVIDER",
    "MEMOTRON_EMBEDDING_MODEL",
    "MEMOTRON_EMBEDDING_BASE_URL",
    "MEMOTRON_EMBEDDING_API_KEY_ENV",
)

failures: list[str] = []


def check(label: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
    if not ok:
        failures.append(label)


@contextmanager
def clean_cwd(env_body: str | None):
    """Run in a fresh directory, with the credential vars genuinely absent."""
    saved_env = {k: os.environ.pop(k, None) for k in KEY_VARS}
    saved_cwd = Path.cwd()
    tmp = Path(tempfile.mkdtemp(prefix="dwenv-"))
    if env_body is not None:
        (tmp / ".env").write_text(env_body, encoding="utf-8")
    os.chdir(tmp)
    try:
        yield tmp
    finally:
        os.chdir(saved_cwd)
        for key, value in saved_env.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        # Leave nothing behind. Under the OLD code these temp dirs were themselves stray
        # `.env` files lying around for the next process to pick up.
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    stray = f"LITELLM_API_KEY={FAKE_KEY}\nMEMOTRON_EMBEDDING_PROVIDER=litellm\n"
    stray += "MEMOTRON_EMBEDDING_MODEL=text-embedding-3\n"

    # ---- Arm B (control) — no `.env` anywhere. Must be green before AND after the fix; a probe
    # that cannot pass is not evidence.
    print("\nArm B — control: cwd has no .env")
    with clean_cwd(None):
        extraction, dream_agent = build_transports_from_env()
        check(
            "B1 no .env -> rule-based",
            isinstance(extraction, RuleBasedExtractionTransport),
            f"{type(extraction).__name__}, dream_agent={type(dream_agent).__name__}",
        )
        check(
            "B2 no .env -> no synthesis",
            build_synthesis_transport_from_env() is None,
            repr(build_synthesis_transport_from_env()),
        )
        check(
            "B3 no .env -> no embedding",
            build_embedding_transport_from_env() is None,
            repr(build_embedding_transport_from_env()),
        )

    # ---- Arm A — a stray, unrelated `.env` sits in the working directory. Nothing about this
    # process asked to use it.
    print("\nArm A — a stray .env sits in cwd (nothing asked for it)")
    with clean_cwd(stray) as tmp:
        if not (tmp / ".env").is_file():
            print("  PRECONDITION FAILED: .env was not written")
            return 2
        extraction, dream_agent = build_transports_from_env()
        check(
            "A1 stray .env must NOT select a gateway transport",
            isinstance(extraction, RuleBasedExtractionTransport),
            f"{type(extraction).__name__}, dream_agent={type(dream_agent).__name__}",
        )
        check(
            "A2 stray .env must NOT select a synthesis transport",
            build_synthesis_transport_from_env() is None,
            type(build_synthesis_transport_from_env()).__name__,
        )
        check(
            "A3 stray .env must NOT select an embedding transport",
            build_embedding_transport_from_env() is None,
            type(build_embedding_transport_from_env()).__name__,
        )
        check(
            "A4 stray .env must NOT leak into os.environ",
            os.environ.get("LITELLM_API_KEY") is None,
            f"LITELLM_API_KEY={os.environ.get('LITELLM_API_KEY')!r}",
        )

    # ---- Arm C — the testability consequence. A caller explicitly clears the credential, the
    # way monkeypatch.delenv does, and the loader puts it straight back from disk.
    print("\nArm C — explicit clearing, then a build call (the PR #44 shape)")
    with clean_cwd(stray):
        os.environ["LITELLM_API_KEY"] = "set-then-explicitly-deleted"  # pragma: allowlist secret
        del os.environ["LITELLM_API_KEY"]
        before = os.environ.get("LITELLM_API_KEY")
        build_transports_from_env()
        after = os.environ.get("LITELLM_API_KEY")
        check(
            "C1 explicit deletion must survive a build call",
            after is None,
            f"before={before!r} after={after!r}",
        )

    # ---- Arm D — the one that SEALS. A stray .env must not be written durably into a graph;
    # the early return on an existing row means a wrong seal here is not repairable.
    print("\nArm D — seed_tenant_llm_credentials_from_env must not seal a stray .env")
    with clean_cwd(stray) as tmp:
        state = seed_tenant_llm_credentials_from_env(graph_path=tmp / "seed-stray.sqlite", tenant_id="probe-t114")
        check(
            "D1 stray .env must NOT be sealed into the graph",
            state is None,
            f"returned {state if state is None else state.get('base_url')!r}",
        )

    # ---- Arm E — the entry-point route must STILL WORK. Removing implicit loading is only
    # correct if an explicit, anchored load still seals; otherwise this is a regression, not a
    # fix. This arm is what `memotron-local-platform --env-file` and
    # `_ensure_project_llm_credentials` rely on.
    print("\nArm E — an EXPLICIT anchored load must still seal (guards against over-fixing)")
    with clean_cwd(stray) as tmp:
        load_env_file(tmp / ".env")
        state = seed_tenant_llm_credentials_from_env(graph_path=tmp / "seed-explicit.sqlite", tenant_id="probe-t114")
        if state is None:
            check("E1 explicit load must still seal", False, "returned None — REGRESSION")
        else:
            check(
                "E1 explicit load must still seal",
                state.get("has_api_key") is True,
                f"provider={state.get('provider')!r} has_api_key={state.get('has_api_key')}",
            )
            check(
                "E2 sealed state must not expose the key",
                FAKE_KEY not in repr(state),
                "key absent from state repr",
            )

    print(
        f"\n{'FAIL' if failures else 'PASS'}: {len(failures)} failing check(s)"
        + (f" -> {', '.join(failures)}" if failures else "")
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
