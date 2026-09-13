"""P1 -- the importable surface must not change without someone saying so.

Runs inside the ordinary ``pytest -q``, so it reaches CI through the existing
``scripts/ci-build-check.sh`` with no pipeline change. Measured cost: ~0.6s on a
~36s suite.

This is the counterpart to ``scripts/verify/pure_move.py``. That proves *nothing
changed*; this proves *nothing disappeared*, which a bytecode comparison cannot
see -- a symbol that stops being importable has identical bytecode wherever it
now lives.

If this fails during the module split, the usual cause is a re-export missed in
the new package's ``__init__.py``. Read the diff before blessing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify" / "api_surface.py"


def test_public_api_surface_matches_the_golden() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        cwd=SCRIPT.parents[2],
    )
    assert result.returncode == 0, (
        "the importable surface of memotron changed, or two mixins shadow a "
        "name.\n\n" + result.stdout + result.stderr
    )
