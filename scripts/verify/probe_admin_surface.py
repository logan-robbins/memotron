#!/usr/bin/env python3
"""The admin arm of the live lane: start the real server, sweep all 53 routes, diff the tally.

    uv run python scripts/verify/probe_admin_surface.py

Exit 0 if the sweep matches its recorded baseline, 1 on drift, 2 if it could not run.

Why this exists separately from sweep_admin.py
----------------------------------------------
``sweep_admin.py`` takes a URL and sweeps whatever is already listening -- that is the
by-hand breadth tool and it stays that way. The live lane needs something it can invoke
with no arguments and no running service, so this wraps it: fresh graph, real server,
sweep, shut down.

Why it launches the CLI instead of importing the handler
--------------------------------------------------------
``memotron-admin-server`` is exactly what the deployed admin pod runs. Importing
``MemoryGraphHandler`` and wiring it by hand would verify a handler this probe assembled
itself, which is a different thing from the one that ships -- and the difference is where
T1b-9 lives (``handler.platform`` is None because **no CLI flag sets it**, a fact only the
real entry point can demonstrate). Subprocessing the console script keeps the thing under
test the thing that deploys.

What it pins, and what it deliberately does not
-----------------------------------------------
It pins the OUTCOME TALLY across all 53 routes, plus the T1b-9 invariant that all 18
``/api/platform/*`` routes 503. It does not assert response bodies: this is breadth, and
``probe_core_loop.py`` is the depth probe. A body-level baseline here would be 53 more
numbers nobody re-derives, which is the failure #140 was filed about.

The graph is FRESH each run, on purpose. The OK/HTTP400 split depends on graph contents,
so sweeping a dogfood graph would make the baseline drift with whatever happened to be
stored -- a baseline that changes for reasons unrelated to the code is one people mute.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
SWEEP = REPO / "scripts/verify/sweep_admin.py"
SCOPE = "tenant:memotron-dogfood"
STARTUP_TIMEOUT_S = 45


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_health(base: str, proc: subprocess.Popen[bytes], log: pathlib.Path) -> bool:
    """Poll /health until it answers, the process dies, or we give up.

    Checks `proc.poll()` every iteration rather than only sleeping: a server that exits
    immediately (bad argument, port in use) would otherwise burn the full timeout and then
    report as "slow to start", which is the wrong diagnosis and the expensive one -- a hung
    job and a slow job look identical from outside unless something checks.
    """
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"  server exited with {proc.returncode} before serving. Log:")
            print("\n".join(f"        {line}" for line in log.read_text().splitlines()[-12:]))
            return False
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.3)
    print(f"  server did not answer /health within {STARTUP_TIMEOUT_S}s")
    return False


def main() -> int:
    if not SWEEP.exists():
        print(f"  MISSING: {SWEEP}")
        return 2

    with tempfile.TemporaryDirectory(prefix="dw-admin-probe-") as tmp:
        workdir = pathlib.Path(tmp)
        graph = workdir / "admin.sqlite"
        log = workdir / "server.log"
        port = _free_port()
        base = f"http://127.0.0.1:{port}"

        env = dict(os.environ, MEMOTRON_ALLOW_EPHEMERAL_KEK="1")
        # The gateway key is scrubbed for the same reason tests/conftest.py scrubs it: a
        # stray credential turns this into a billed, network-dependent probe by accident.
        env.pop("LITELLM_API_KEY", None)

        # The server REFUSES to create its own graph -- `admin_server:main` raises
        # SystemExit("graph path does not exist") before binding. So the graph is created
        # here first, which is also what an operator has to do. Worth knowing for the
        # deployment story: the admin pod cannot bootstrap an empty volume by itself.
        from memotron import Memotron

        Memotron(graph_path=graph)
        if not graph.exists():
            print(f"  could not create a graph at {graph}")
            return 2

        print(f"  starting memotron-admin-server on {base} (fresh graph)")
        with log.open("wb") as sink:
            proc = subprocess.Popen(
                [
                    "uv",
                    "run",
                    "--no-sync",
                    "memotron-admin-server",
                    "--graph-path",
                    str(graph),
                    "--scope",
                    SCOPE,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                stdout=sink,
                stderr=subprocess.STDOUT,
                cwd=REPO,
                env=env,
            )
        try:
            if not _wait_for_health(base, proc, log):
                return 2
            print("  server healthy; sweeping all discovered routes")
            result = subprocess.run(
                ["uv", "run", "--no-sync", "python", str(SWEEP), base],
                cwd=REPO,
                env=env,
                check=False,
            )
            return result.returncode
        finally:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            if proc.poll() is None:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
