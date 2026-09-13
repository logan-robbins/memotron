"""The admin HTTP surface must not return internal exception text to a client. (#154)

Both request handlers used to end with::

    except Exception as exc:
        self._send_json({"error": str(exc)}, status=400)

so any unhandled exception -- a psycopg message, a file path, a key name, whatever a
driver puts in its exception -- was serialised into the response body verbatim.

Distinct from #126 and it survives that issue being fixed: authentication controls
*who* reaches this surface; this controls *what an error tells them*. An authenticated
operator still should not receive a driver's internal string.

WHAT THIS DOES **NOT** CHANGE, and the reason the first attempt at this fix was wrong.
Four routes in the live sweep (`/api/evidence`, `/api/neighborhood`, `/api/timeline`,
`/api/tenant-prompts/rerun`) reach that handler carrying DELIBERATE validation messages
-- "relationship_uuid is required", "entity is required" -- raised as plain
``ValueError`` rather than ``HttpApiError``. Replacing the whole branch with a generic
500 turned those useful 400s into opaque errors, and the repo's own admin sweep caught
it as tally drift (`HTTP400=13` -> `HTTP400=9 HTTP500=4`). ``HttpApiError`` IS a
``ValueError`` subclass (`_errors.py:13`), so the ValueError branch is the same family
without an explicit status, and it keeps its message at 400.
"""

from __future__ import annotations

import json
from pathlib import Path
from queue import Queue
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from memotron import Memotron, MemoryScope, ScopeKind, admin_server

#: A string that must never appear in a response body. Stands in for the things a real
#: exception carries: connection strings, absolute paths, table and column names.
SECRET = "psycopg-internal-detail-/var/secrets//memotron.key"


def _serve(handler_cls, graph_path: Path, scope: MemoryScope) -> tuple[str, object]:
    queue: Queue = Queue()

    def run() -> None:
        from http.server import HTTPServer

        handler_cls.client = Memotron(graph_path=graph_path)
        handler_cls.default_scope = scope
        handler_cls.graph_path = graph_path
        server = HTTPServer(("127.0.0.1", 0), handler_cls)
        queue.put(server)
        server.serve_forever()

    Thread(target=run, daemon=True).start()
    server = queue.get(timeout=10)
    return f"http://127.0.0.1:{server.server_port}", server


@pytest.fixture
def admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="disclosure")
    graph_path = tmp_path / "disclosure.sqlite"
    Memotron(graph_path=graph_path)  # the server refuses to create its own graph
    handler_cls = admin_server.MemoryGraphHandler
    base_url, server = _serve(handler_cls, graph_path, scope)
    yield base_url, handler_cls, monkeypatch
    server.shutdown()


def _body(exc: HTTPError) -> str:
    """Read the error body ONCE.

    ``HTTPError.read()`` consumes the stream, so a second call returns ``""`` -- and
    ``assert "KeyError" not in ""`` passes for the wrong reason. An earlier draft of this
    file did exactly that: the assertion held because the body was empty, not because the
    text was absent. Call this once per response and assert against the variable.
    """
    return exc.read().decode("utf-8")


class TestAnUnexpectedErrorTellsTheClientNothing:
    def test_internal_exception_text_is_not_in_the_response(self, admin) -> None:
        """THE REGRESSION. Without the fix the response body IS the exception string."""
        base_url, handler_cls, monkeypatch = admin

        def boom(self) -> object:
            raise RuntimeError(SECRET)

        monkeypatch.setattr(handler_cls, "_scope_payload", boom, raising=True)

        with pytest.raises(HTTPError) as caught:
            urlopen(f"{base_url}/api/scopes", timeout=10)

        body = _body(caught.value)
        assert SECRET not in body, f"the admin server returned internal exception text to the client: {body!r}"
        assert "psycopg" not in body and "/var/secrets" not in body
        assert json.loads(body) == {"error": "internal error"}
        assert caught.value.code == 500, "an unexpected internal error is not a client error"

    def test_the_generic_body_carries_no_detail_at_all(self, admin) -> None:
        """A generic message that still names the exception type would leak the stack shape."""
        base_url, handler_cls, monkeypatch = admin

        def boom(self) -> object:
            raise KeyError("tenant_llm_credentials")

        monkeypatch.setattr(handler_cls, "_scope_payload", boom, raising=True)

        with pytest.raises(HTTPError) as caught:
            urlopen(f"{base_url}/api/scopes", timeout=10)

        body = _body(caught.value)
        assert json.loads(body) == {"error": "internal error"}
        assert "KeyError" not in body
        assert "tenant_llm_credentials" not in body


class TestDeliberateValidationStillReachesTheCaller:
    """The half the first attempt broke. These messages exist to be read."""

    def test_deliberate_validation_uses_httpapierror_and_reaches_the_caller(self, admin) -> None:
        """All 7 former `raise ValueError` validation sites now raise HttpApiError(400).

        Before that conversion the handler needed an interim `except ValueError -> 400`
        branch to keep those messages reaching callers. With one type for the whole
        contract the branch is gone, and this asserts the messages still arrive.
        """
        base_url, handler_cls, monkeypatch = admin

        def boom(self) -> object:
            raise admin_server.HttpApiError(400, "entity is required")

        monkeypatch.setattr(handler_cls, "_scope_payload", boom, raising=True)

        with pytest.raises(HTTPError) as caught:
            urlopen(f"{base_url}/api/scopes", timeout=10)

        assert caught.value.code == 400, "deliberate validation must stay a client error"
        assert json.loads(_body(caught.value)) == {"error": "entity is required"}

    def test_a_domain_valueerror_still_reaches_the_caller(self, admin) -> None:
        """The DOMAIN boundary. Removing this branch was tried and reverted.

        admin_server's own validation raises HttpApiError, but the layers underneath
        raise plain ValueError for caller-facing validation and cannot do otherwise --
        `config/_tenancy.py:281` raises ValueError("unknown agent_id ... for tenant ...")
        and config/ cannot import from admin_server/. Translating that into a client
        error is the HTTP layer's job.

        Deleting the branch turned /api/tenant-prompts/rerun's message into an opaque
        500; the admin sweep caught it as tally drift (HTTP400=13 -> 12, HTTP500=1).
        """
        base_url, handler_cls, monkeypatch = admin

        def boom(self) -> object:
            raise ValueError("unknown agent_id 'claude-code' for tenant 'wdpr-demo'")

        monkeypatch.setattr(handler_cls, "_scope_payload", boom, raising=True)

        with pytest.raises(HTTPError) as caught:
            urlopen(f"{base_url}/api/scopes", timeout=10)

        body = _body(caught.value)
        assert caught.value.code == 400, "domain validation must reach the caller as a client error"
        assert json.loads(body) == {"error": "unknown agent_id 'claude-code' for tenant 'wdpr-demo'"}
