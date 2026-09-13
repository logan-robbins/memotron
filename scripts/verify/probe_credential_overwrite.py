"""T1-26: does migrating a tenant silently replace the DESTINATION's LLM credentials?

Filed claim (AGENT-SOURCED): `_migrate_llm_credentials` (`migration.py:539-555`) calls
`set_tenant_llm_credentials` on the destination with no check that the destination already has
one, and the SQL is `ON CONFLICT(tenant_id) DO UPDATE SET ... api_key_sealed = excluded...`
(`storage/sqlite/_governance.py:787` `set_tenant_llm_credentials`, upsert at `:851`). The destination then runs extraction on the SOURCE project's sealed
key indefinitely, because `seed_tenant_llm_credentials_from_env` returns early when a row
already exists (`runtime.py:398-400`) and so never repairs it.

Seed BOTH tenants with distinct credentials, migrate, and read the destination back. This is a
credential-flow claim, so it is measured rather than read.

    uv run python scripts/verify/probe_credential_overwrite.py

exit 0 = the destination keeps its own credentials  ·  1 = they are replaced by the source's
exit 2 = the precondition failed (a tenant did not hold the credential we set)

CITATIONS REPAIRED 2026-08-31: every `file.py:NNN` below was rewritten after the module
split dissolved the god files they named. Each now carries the SYMBOL as well as the line,
so the next move makes them findable by grep rather than silently wrong.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MEMOTRON_ALLOW_EPHEMERAL_KEK", "1")

from memotron.storage import open_storage

SRC, DST = "alpha", "bravo"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dwcred-"))
    src_path, dst_path = tmp / "source.sqlite", tmp / "dest.sqlite"

    src = open_storage(str(src_path))
    dst = open_storage(str(dst_path))

    # Distinct, unmistakable credentials on each side.
    src.set_tenant_llm_credentials(
        tenant_id=SRC,
        provider="openai",
        api_key="SOURCE-PROJECT-KEY",
        base_url="https://source.example/v1",
        model="source-model",
        embedding_provider="openai",
        embedding_base_url="https://source.example/v1",
        embedding_model="source-embed",
    )
    dst.set_tenant_llm_credentials(
        tenant_id=DST,
        provider="openai",
        api_key="DEST-OWN-KEY",
        base_url="https://dest.example/v1",
        model="dest-model",
        embedding_provider="openai",
        embedding_base_url="https://dest.example/v1",
        embedding_model="dest-embed",
    )

    before = dst.tenant_llm_credentials(DST)
    print(f"\n  destination BEFORE migrate : model={before['model']!r} base_url={before['base_url']!r}")
    print(f"  source holds               : model={src.tenant_llm_credentials(SRC)['model']!r}")

    # PRECONDITION: if the destination never actually held its own credential, an
    # "overwrite" afterwards proves nothing -- there was nothing to overwrite.
    if before is None or before["model"] != "dest-model":
        print("\n  PRECONDITION FAILED: the destination did not hold the credential we set.")
        return 2

    from memotron.migration import migrate_tenant_memory

    migrate_tenant_memory(src, source_tenant_id=SRC, dest_tenant_id=DST, dest_store=dst)

    after = dst.tenant_llm_credentials(DST)
    print(f"  destination AFTER migrate  : model={after['model']!r} base_url={after['base_url']!r}")

    replaced = after["model"] == "source-model" or after["api_key"] == "SOURCE-PROJECT-KEY"
    print()
    if replaced:
        print("  === CONFIRMED: the destination now runs on the SOURCE project's key ===")
        print("  Nothing reported it. runtime.py:398-400 returns early when a row exists,")
        print("  so environment-based seeding will never repair this.")
        return 1
    print("  === REFUTED: the destination kept its own credentials ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
