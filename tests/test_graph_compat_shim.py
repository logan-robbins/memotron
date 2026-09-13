"""`memotron.graph` is a compatibility shim, and its promises are dynamic.

The module forwards every unknown attribute to ``memotron.storage.sqlite`` via
``__getattr__``, and its own docstring names ``DREAM_CLAIM_STALE_SECONDS`` and
``ScopeStateHashTracker`` as things that keep resolving. That forwarding is exactly
what the API-surface golden CANNOT see: the golden enumerates ``vars(module)``, and
a ``__getattr__``-provided name is in no module dict anywhere.

Found the hard way during the sqlite split. Moving ScopeStateHashTracker into
sqlite/_graph.py silently broke ``from memotron.graph import
ScopeStateHashTracker`` -- the golden flagged the sqlite-side removal, I checked
for importers with grep, found only docstrings, and shipped it. The grep could not
see the shim either. The next extraction moved DREAM_CLAIM_STALE_SECONDS and broke
``admin_server.py:51``, which does import through the shim, loudly enough that
collection failed.

So this file pins the shim itself. Anything the split moves out of
``storage/sqlite/__init__.py`` must stay re-exported from it, or these fail.
"""

from __future__ import annotations

import importlib

import pytest

#: Names `memotron.graph` is documented to keep resolving. The first two are
#: named in its docstring; the last two are in its explicit __all__.
FORWARDED = [
    "DREAM_CLAIM_STALE_SECONDS",
    "LLM_CREDENTIAL_SUBJECT_KEY",
    "ScopeStateHashTracker",
    "PropertyGraphStore",
    "normalize_key",
]


@pytest.mark.parametrize("name", FORWARDED)
def test_legacy_graph_import_still_resolves(name: str) -> None:
    graph = importlib.import_module("memotron.graph")
    assert hasattr(graph, name), (
        f"memotron.graph.{name} no longer resolves. The shim forwards to "
        "memotron.storage.sqlite, so a name moved into a private submodule must "
        "be re-exported from storage/sqlite/__init__.py."
    )


def test_admin_server_imports_through_the_shim() -> None:
    """The one runtime consumer, asserted by name rather than incidentally.

    admin_server.py does `from memotron.graph import DREAM_CLAIM_STALE_SECONDS`
    at module scope, so breaking the shim breaks importing the admin server at all.
    """
    admin = importlib.import_module("memotron.admin_server")
    assert isinstance(admin.DREAM_CLAIM_STALE_SECONDS, float)


def test_the_shim_really_is_dynamic() -> None:
    """Guards the guard: prove __getattr__ forwarding is what is being tested.

    If `memotron.graph` ever stops forwarding, the tests above would pass
    trivially for anything that happened to be a real module attribute.
    """
    graph = importlib.import_module("memotron.graph")
    assert hasattr(graph, "__getattr__"), "the shim's forwarding was removed"
    with pytest.raises(AttributeError):
        _ = graph.definitely_not_a_real_name_on_the_sqlite_backend
