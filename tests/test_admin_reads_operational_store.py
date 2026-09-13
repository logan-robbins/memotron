"""The admin surface must be able to read the store the system actually uses.

Until this landed, `admin_server` could only open a SQLite **file**: `--graph-path` was
required and validated with `graph_path.exists()`, and the client was built as
`Memotron(graph_path=...)`. In every deployed environment that meant the operator
surface served a demo graph seeded into the container at start.

Measured on `latest` 2026-09-07, against the real admin ingress:

    /api/overview   -> graph_path "/tmp/memory_graph_demo.sqlite", tenant "wdpr-demo"
    /api/dream-runs -> runs dated 2026-04-20, job "dream-consolidation [wdpr-demo/...]"

while the real store sat in Postgres with 10 episodes and 263 formation runs. **An
operator surface showing plausible fabricated data is worse than one showing nothing**,
because nothing prompts a second look. To answer "is the queue draining" today you have to
exec into a pod and write SQL, which is not a thing anyone does at 2am.

`Memotron` has always accepted `storage=`. Four layers between it and the admin server
threaded only `graph_path`, so this widens each to take either. The tests below are about
that threading, because it is shared code: `build_transports_from_tenant_graph` has five
existing callers and `build_agent_memory_client` is the base of the agent-memory surface.
"""

from __future__ import annotations

import pytest

import memotron.admin_server as admin
from memotron import Memotron
from memotron.agent_memory import AgentMemoryPlatform
from memotron.agent_memory._builders import build_agent_memory_client
from memotron.runtime import build_transports_from_tenant_graph
from memotron.storage import open_storage


class TestTheHelpersAcceptAnOpenStore:
    def test_transports_can_be_built_from_a_store_the_caller_owns(self) -> None:
        store = open_storage(":memory:")
        extraction, _agent = build_transports_from_tenant_graph(store=store, tenant_id="t")
        assert extraction is not None

    def test_a_caller_supplied_store_is_NOT_closed(self) -> None:
        """THE SHARP ONE. The path variant opens a store and closes it in a `finally`.

        Reusing that shape for an injected store would close the caller's backend -- in a
        deployment that is a shared Postgres pool, so the admin server would tear down its
        own connection pool on the first tenant lookup and every later read would fail.
        The caller owns what it passes in.
        """
        store = open_storage(":memory:")
        build_transports_from_tenant_graph(store=store, tenant_id="t")
        # Still usable: a closed backend raises here instead.
        assert store.scopes() is not None

    def test_a_client_can_be_built_on_an_injected_storage(self) -> None:
        store = open_storage(":memory:")
        client = build_agent_memory_client(storage=store, project_id="proj")
        assert client.graph is store, "the client must use the storage it was handed, not open its own"

    def test_the_platform_threads_storage_all_the_way_down(self) -> None:
        """`AgentMemoryPlatform.create` is the layer the admin server actually calls."""
        store = open_storage(":memory:")
        platform = AgentMemoryPlatform.create(storage=store, project_id="proj")
        assert platform.client.graph is store


class TestTheOldPathIsUnchanged:
    """Five existing callers pass `graph_path`. None of them may change behaviour."""

    def test_transports_still_build_from_a_path(self) -> None:
        extraction, _agent = build_transports_from_tenant_graph(graph_path=":memory:", tenant_id="t")
        assert extraction is not None

    def test_a_client_still_builds_from_a_path(self) -> None:
        client = build_agent_memory_client(graph_path=":memory:", project_id="proj")
        assert client.graph is not None


class TestExactlyOneSourceOfTruth:
    """Neither-or-both is a programming error, and it must say so rather than pick one.

    Silently preferring one would make a caller that passes both -- easy, since the
    parameters sit next to each other -- read from a store it did not intend, which is
    precisely the class of bug this whole change exists to remove.
    """

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"graph_path": ":memory:", "store": "not-a-store"},
        ],
    )
    def test_transports_refuse_ambiguity(self, kwargs: dict) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            build_transports_from_tenant_graph(tenant_id="t", **kwargs)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"graph_path": ":memory:", "storage": "not-a-store"},
        ],
    )
    def test_the_client_builder_refuses_ambiguity(self, kwargs: dict) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            build_agent_memory_client(project_id="proj", **kwargs)


class TestTheStoreDecisionItself:
    """`build_admin_client` is the decision, extracted from `main()` so it is testable
    without binding a port.

    It was the untested half of this change, and an untested store-selection is exactly
    how a deployed admin surface ends up serving a demo SQLite graph while the real data
    sits in Postgres.
    """

    def test_a_configured_DSN_wins_over_an_explicit_graph_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """The precedence that matters. Both are present; the operational store must win,
        mirroring `mcp_server` where the DSN beats MEMOTRON_GRAPH_PATH. If the path won,
        a deployment that set both would silently serve the file."""
        sqlite_file = tmp_path / "decoy.sqlite"
        Memotron(graph_path=sqlite_file).graph.close()

        sentinel = object()
        monkeypatch.setattr(admin, "storage_settings_from_env", lambda: "settings")
        monkeypatch.setattr(admin, "key_manager_from_env", lambda: None)
        monkeypatch.setattr(admin, "create_storage_backend", lambda settings, key_manager=None: sentinel)
        monkeypatch.setattr(admin, "Memotron", lambda **kw: type("C", (), {"kw": kw})())

        client, store, graph_path = admin.build_admin_client(str(sqlite_file))
        assert store is sentinel
        assert graph_path is None, "the operational path must not also report a file path"
        assert "storage" in client.kw and "graph_path" not in client.kw

    def test_no_DSN_falls_back_to_the_graph_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """The control. Without it, a decision that always chose the operational store
        would pass the test above and break every local and hermetic caller."""
        sqlite_file = tmp_path / "real.sqlite"
        Memotron(graph_path=sqlite_file).graph.close()
        monkeypatch.setattr(admin, "storage_settings_from_env", lambda: None)

        client, store, graph_path = admin.build_admin_client(str(sqlite_file))
        assert store is None, "the SQLite path lets the client own its backend"
        assert graph_path == sqlite_file
        assert client.graph is not None

    def test_neither_a_DSN_nor_a_path_is_a_refusal_with_a_sentence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not a traceback. An operator who forgot both should be told which two things
        would fix it."""
        monkeypatch.setattr(admin, "storage_settings_from_env", lambda: None)
        with pytest.raises(SystemExit) as exit_info:
            admin.build_admin_client("")
        message = str(exit_info.value)
        assert "--graph-path" in message
        assert "MEMOTRON_OPERATIONAL_STORE_DSN" in message

    def test_a_missing_graph_file_is_still_refused(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """Pre-existing behaviour that must survive the refactor."""
        monkeypatch.setattr(admin, "storage_settings_from_env", lambda: None)
        with pytest.raises(SystemExit, match="does not exist"):
            admin.build_admin_client(str(tmp_path / "absent.sqlite"))


class TestTheHandlerRoutesToWhicheverStoreItHas:
    def test_store_kwargs_prefers_the_operational_backend(self) -> None:
        handler = admin.MemoryGraphHandler
        sentinel = object()
        try:
            handler.store = sentinel  # type: ignore[assignment]
            handler.graph_path = None
            assert admin.MemoryGraphHandler._store_kwargs(handler) == {"store": sentinel}
        finally:
            handler.store = None

    def test_store_kwargs_falls_back_to_the_path(self, tmp_path) -> None:
        """Every helper call site used to hardcode `graph_path=self.graph_path`, which is
        None on the operational store. This is the one place that decision now lives."""
        handler = admin.MemoryGraphHandler
        handler.store = None
        handler.graph_path = tmp_path / "g.sqlite"
        assert admin.MemoryGraphHandler._store_kwargs(handler) == {"graph_path": tmp_path / "g.sqlite"}
