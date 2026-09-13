"""T2-10: a missing admin UI build must be visible at startup, not five days later.

The incident
------------
The dogfood admin server returned **HTTP 503 on ``GET /`` for about five days** while ``/api/*``
answered normally, so the service looked healthy. Its ``--static-dir`` resolved into a git
worktree that was deleted the day after the process started.

Nothing reported it, and each of these is independently sufficient to hide it:

* ``main()`` validates the GRAPH path and exits when it is missing, and does not validate the
  static dir at all -- it prints the path and starts serving.
* ``HttpApiError`` carries only a status; ``log_message`` is silenced; the module has no logger.
* ``/health`` is an unconditional ``{"status": "ok"}`` that never consults the static dir, and
  every Kubernetes probe and the GCLB check target ``/health``.

So the fix is a warning at the one moment someone is looking -- startup -- and these tests pin
both halves: what the warning says, and the 503 it is warning about.

Why ``index.html`` and not the directory
----------------------------------------
``_send_static_asset`` serves ``index.html`` for ``/``. A directory that exists but is empty
therefore yields a bare ``404 {"error": "not found"}``, identical to the path-traversal
rejection -- the harder of the two states to diagnose, and the one a plain ``exists()`` check
would sail past. ``test_a_directory_without_index_html_is_still_a_warning`` is that case.

Why tmp_path everywhere
-----------------------
``ui/admin/dist`` is gitignored and no lane of ``check.sh`` builds it, so it is absent in a fresh
clone and in CI. A test that depended on it would pass on the author's machine and skip-or-fail
everywhere else, which is the class of thing this repo keeps finding.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest

from memotron.admin_server import (
    DEFAULT_ADMIN_STATIC_DIR,
    MemoryGraphHandler,
    static_build_warning,
    warn_if_admin_build_missing,
)


def _built(tmp_path: Path) -> Path:
    """A directory that looks like a real vite build to the code that reads it."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Memotron Admin</title>")
    return dist


# ------------------------------------------------------------------ the warning
def test_a_missing_directory_warns_and_says_what_to_do(tmp_path: Path) -> None:
    warning = static_build_warning(tmp_path / "never-built")

    assert warning is not None
    assert "never-built" in warning, "must name the path, or the reader cannot act on it"
    assert "npm --prefix ui/admin run build" in warning, "must name the remedy"
    assert "503" in warning, "must connect the warning to the symptom someone will actually see"


def test_a_directory_without_index_html_is_still_a_warning(tmp_path: Path) -> None:
    """The case a plain ``exists()`` check would miss, and the harder one to diagnose.

    An empty directory produces a 404 that is byte-identical to the path-traversal rejection,
    so there is nothing in the response to tell the two apart.
    """
    empty = tmp_path / "dist"
    empty.mkdir()

    warning = static_build_warning(empty)

    assert warning is not None
    assert "no index.html" in warning, "the message must distinguish this from a missing directory"


def test_a_real_build_produces_no_warning(tmp_path: Path) -> None:
    """The control. Without it, 'warn always' would satisfy every assertion above."""
    assert static_build_warning(_built(tmp_path)) is None


def test_the_check_resolves_user_paths(tmp_path: Path) -> None:
    """`--static-dir` is a user-supplied string; both entry points pass it through unresolved.

    Pinned because the warning quotes the path back, and quoting an unexpanded `~/...` at
    someone hunting a 503 sends them to a directory that is not the one being served.
    """
    dist = _built(tmp_path)
    assert static_build_warning(Path(str(dist))) is None
    warning = static_build_warning(Path("~/definitely-not-a-real-memotron-dist"))
    assert warning is not None
    assert "~" not in warning, "the path must be resolved in the message, not echoed raw"


# --------------------------------------------------- the 503 the warning is about
class _Recorder(MemoryGraphHandler):
    """Drive `_send_static_asset` without a socket.

    `MemoryGraphHandler` is a `BaseHTTPRequestHandler`, whose `__init__` immediately begins
    reading a request off a connection. Constructing it for real would need a live server --
    which `probe_admin_surface.py` already does for the breadth sweep. What is under test here
    is one method's contract, so the instance is built without `__init__` and given only the
    attributes that method touches.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[int, dict[str, Any]]] = []
        self.status: int | None = None
        self.headers_sent: dict[str, str] = {}
        self.body = b""

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:  # type: ignore[override]
        self.sent.append((status, payload))

    # The success path writes a real HTTP response, so the stub has to cover it too --
    # otherwise the "a real build works" control fails on a missing `requestline` and would
    # look like the 503 it is supposed to rule out.
    def send_response(self, code: int, message: str | None = None) -> None:  # type: ignore[override]
        self.status = int(code)

    def send_header(self, keyword: str, value: str) -> None:  # type: ignore[override]
        self.headers_sent[keyword] = value

    def end_headers(self) -> None:  # type: ignore[override]
        return

    @property
    def wfile(self) -> Any:  # type: ignore[override]
        recorder = self

        class _Sink:
            def write(self, data: bytes) -> int:
                recorder.body += data
                return len(data)

        return _Sink()


def _get_root(static_dir: Path) -> tuple[int, dict[str, Any]]:
    handler = _Recorder()
    handler.static_dir = static_dir
    from memotron.admin_server._errors import HttpApiError

    try:
        handler._send_static_asset("")
    except HttpApiError as exc:
        return exc.status, {"error": str(exc)}
    return handler.status or 200, {"body": handler.body.decode(), "headers": handler.headers_sent}


def test_a_missing_build_serves_503_with_an_actionable_body(tmp_path: Path) -> None:
    """Pins the contract the startup warning points at. Nothing asserted this before.

    If this ever becomes a 404 or a bare 500, the warning's "will return 503" is a lie, and the
    warning is the only thing telling an operator what to expect.
    """
    status, body = _get_root(tmp_path / "never-built")

    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert "React admin build not found" in json.dumps(body)


def test_a_real_build_serves_the_shell(tmp_path: Path) -> None:
    """Control: the 503 is about the build being absent, not this path always failing.

    Asserts the shell actually comes back rather than merely 'not 503' -- a method that
    returned 200 with an empty body would satisfy the weaker check and serve a blank page.
    """
    status, response = _get_root(_built(tmp_path))

    assert status == HTTPStatus.OK
    assert "Memotron Admin" in response["body"]
    assert response["headers"]["Content-Type"] == "text/html"


# ------------------------------------------------------------------ the default
def test_the_shipped_default_is_reported_honestly() -> None:
    """`DEFAULT_ADMIN_STATIC_DIR` is `parents[3]/ui/admin/dist` -- correct only from a checkout.

    From an installed wheel it resolves under site-packages and does not exist, which is T2-10's
    original framing. This test does not assert which state the machine is in -- that would be a
    check calibrated against the machine running it -- only that the helper agrees with reality
    either way, so the startup warning cannot disagree with what the server will actually do.
    """
    expected_quiet = (DEFAULT_ADMIN_STATIC_DIR / "index.html").is_file()
    assert (static_build_warning(DEFAULT_ADMIN_STATIC_DIR) is None) is expected_quiet


# ------------------------------------------------------- the emission, not just the message
def test_the_emitter_prints_the_warning_and_reports_that_it_did(tmp_path: Path) -> None:
    """Split out of `main()` so this is reachable at all.

    With the check and the print inlined in both entry points, six lines of this change were
    executable only by starting a server -- `diff-cover` is what surfaced that, by measuring
    this branch's own diff for the first time once the merge-base was corrected.
    """
    lines: list[str] = []

    fired = warn_if_admin_build_missing(tmp_path / "never-built", echo=lines.append)

    assert fired is True
    assert len(lines) == 1
    assert lines[0].startswith("WARNING: "), "must use the convention both entry points already print"
    assert "npm --prefix ui/admin run build" in lines[0]


def test_the_emitter_stays_silent_and_says_so_when_the_build_is_there(tmp_path: Path) -> None:
    """The control: no output, and a False return so a caller could act on it."""
    lines: list[str] = []

    fired = warn_if_admin_build_missing(_built(tmp_path), echo=lines.append)

    assert fired is False
    assert lines == []


@pytest.mark.parametrize("entry_point", ["memotron.admin_server", "memotron.local_platform"])
def test_both_entry_points_consult_the_check(entry_point: str) -> None:
    """Both CLIs must call it. `local_platform` had no path validation of any kind before this.

    A source check rather than a subprocess run: starting either server binds a port and blocks.
    The behaviour the call performs is covered by the two emitter tests above; this only pins
    that `main()` is wired to it, which is the one line neither test can reach.
    """
    import importlib

    source = Path(importlib.import_module(entry_point).__file__).read_text()
    assert "warn_if_admin_build_missing(" in source, f"{entry_point} never calls the check"
