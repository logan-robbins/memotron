"""`memotron key` — the operator write path for the per-key registry. (#168)

The registry shipped with a read path and no writer: `bind_key_principal` had zero callers
outside its own test, so `principal_for_key_alias` returned ``None`` for every alias.
#126 / #137 could both have been finished and every caller would still have been anonymous.

**Every test here runs on BOTH engines.** #149 is the standing lesson in this repo -- a method
exercised on the engine that never runs in production and not on the one that does -- and
#163/#164 were both that same shape, found within a day of each other.

The test that matters most is `test_a_bound_alias_resolves_end_to_end`: #168 explicitly asked
for a positive control, because "an unbound alias returns None" passes whether or not writing
works at all. A write path with a broken writer and a working reader is indistinguishable from
an empty registry, which is the exact condition #168 was filed about.
"""

from __future__ import annotations

import json
import os
import pathlib
from collections.abc import Iterator

import pytest

from memotron import key_registry_cli
from memotron.cli import main


def _postgres_dsn_or_skip() -> str:
    dsn = os.environ.get("MEMOTRON_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("set MEMOTRON_TEST_POSTGRES_DSN to run: needs a live Postgres")
    return dsn


def _reset_postgres_schema(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def store_args(request: pytest.FixtureRequest, tmp_path: pathlib.Path) -> Iterator[list[str]]:
    """The CLI flags that point at a store, one per engine.

    Postgres gets ``--migrate`` because the CLI deliberately does NOT migrate by default:
    an operator laptop must not silently migrate a production store as a side effect of a
    lookup. SQLite applies its DDL on open, so it needs nothing.
    """
    if request.param == "postgres":
        dsn = _postgres_dsn_or_skip()
        _reset_postgres_schema(dsn)
        yield ["--dsn", dsn, "--migrate"]
    else:
        yield ["--graph-path", str(tmp_path / "registry.sqlite")]


def _run(argv: list[str]) -> int:
    """Invoke the real CLI entry point and return its exit code.

    ``SystemExit`` carries either a numeric status or a MESSAGE. A message means the CLI
    refused the invocation and printed why, which is an operator error rather than a
    command result -- so it is re-raised for the caller to assert on, not coerced into a
    number. An earlier version of this helper did ``int(exc.code)`` and blew up on exactly
    those cases, reporting a helper bug as two product failures.
    """
    import sys

    argv_backup = sys.argv
    sys.argv = ["memotron", *argv]
    try:
        main()
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        raise
    finally:
        sys.argv = argv_backup
    return 0


def _bind(store_args: list[str], alias: str, tenant: str, *extra: str) -> int:
    return _run(
        [
            "key",
            "bind",
            "--alias",
            alias,
            "--principal-id",
            f"{tenant}-svc",
            "--tenant-id",
            tenant,
            *extra,
            *store_args,
        ]
    )


class TestTheWritePathThatWasMissing:
    def test_a_bound_alias_resolves_end_to_end(self, store_args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        """THE POSITIVE CONTROL #168 asked for.

        A negative-only assertion ("an unknown alias returns None") passes even if binding
        writes nothing at all -- which is precisely the state #168 was filed about. This
        writes through the CLI and reads back through the CLI.
        """
        assert _bind(store_args, "dw-acme-01", "acme", "--role", "admin", "--agent-id", "claude-code") == 0
        capsys.readouterr()

        assert _run(["key", "show", "--alias", "dw-acme-01", "--json", *store_args]) == 0

        row = json.loads(capsys.readouterr().out)
        assert row["tenant_id"] == "acme"
        assert row["principal_id"] == "acme-svc"
        assert row["role"] == "admin"
        assert row["agent_id"] == "claude-code"

    def test_show_on_an_unbound_alias_exits_1_and_does_not_error(
        self, store_args: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unauthenticated is an answer, not a fault. A script checking $? must be able to
        tell "no such caller" from "the store is broken", so this is exit 1, not a traceback."""
        assert _run(["key", "show", "--alias", "never-issued", *store_args]) == 1
        assert "unbound" in capsys.readouterr().out

    def test_scope_lists_survive_the_round_trip(
        self, store_args: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--allowed-scope-key` is repeatable and the order/content must survive argparse,
        storage and JSON rendering -- it is the allowlist an authorization decision reads."""
        assert (
            _bind(
                store_args,
                "dw-acme-02",
                "acme",
                "--default-scope-key",
                "tenant:acme",
                "--allowed-scope-key",
                "tenant:acme",
                "--allowed-scope-key",
                "customer:acme:wdw",
            )
            == 0
        )
        capsys.readouterr()
        _run(["key", "show", "--alias", "dw-acme-02", "--json", *store_args])
        row = json.loads(capsys.readouterr().out)
        assert row["default_scope_key"] == "tenant:acme"
        assert set(row["allowed_scope_keys"]) == {"tenant:acme", "customer:acme:wdw"}


class TestRotationAndRefusal:
    def test_rebinding_within_one_tenant_is_rotation_and_succeeds(
        self, store_args: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """DW-027: regenerating a gateway key KEEPS its alias and changes only the key
        material. So a re-bind is the rotation path and must not read as a collision."""
        assert _bind(store_args, "dw-rot", "acme", "--role", "user") == 0
        assert _bind(store_args, "dw-rot", "acme", "--role", "admin") == 0
        out = capsys.readouterr().out
        assert "rebound (rotation)" in out, "the operator must be told this replaced a binding"

        _run(["key", "show", "--alias", "dw-rot", "--json", *store_args])
        assert json.loads(capsys.readouterr().out)["role"] == "admin"

    def test_binding_an_alias_held_by_another_tenant_is_REFUSED(
        self, store_args: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """THE SECURITY PROPERTY. `key_alias` is globally unique because the gateway makes it
        so (DW-027 attempted forgery three ways; all three were refused). If the CLI let one
        tenant take another's alias, the lookup would resolve a caller to the wrong tenant."""
        assert _bind(store_args, "dw-taken", "acme") == 0
        capsys.readouterr()

        assert _bind(store_args, "dw-taken", "beta-corp") != 0, "cross-tenant bind must not succeed"

        capsys.readouterr()
        _run(["key", "show", "--alias", "dw-taken", "--json", *store_args])
        assert json.loads(capsys.readouterr().out)["tenant_id"] == "acme", (
            "a refused claim must leave the incumbent binding untouched"
        )

    def test_unbind_is_idempotent_and_says_which(
        self, store_args: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _bind(store_args, "dw-gone", "acme") == 0
        capsys.readouterr()

        assert _run(["key", "unbind", "--alias", "dw-gone", *store_args]) == 0
        assert "unbound" in capsys.readouterr().out

        assert _run(["key", "unbind", "--alias", "dw-gone", *store_args]) == 0
        assert "no binding existed" in capsys.readouterr().out

        assert _run(["key", "show", "--alias", "dw-gone", *store_args]) == 1


class TestOperatorErgonomics:
    def test_no_store_configured_explains_the_three_ways_to_supply_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator with no DSN must get an instruction, not a stack trace."""
        monkeypatch.delenv("MEMOTRON_OPERATIONAL_STORE_DSN", raising=False)
        with pytest.raises(SystemExit) as excinfo:
            _run(["key", "show", "--alias", "x"])
        assert "no store configured" in str(excinfo.value)

    def test_dsn_and_graph_path_together_is_refused(self, tmp_path: pathlib.Path) -> None:
        """Two stores named at once is an operator error with no safe interpretation.

        Neither store is ever opened -- the refusal happens first -- but the path comes from
        `tmp_path` rather than a hardcoded /tmp name so the test cannot collide with a
        parallel run or be read as endorsing a predictable temp path.
        """
        with pytest.raises(SystemExit) as excinfo:
            _run(
                [
                    "key",
                    "show",
                    "--alias",
                    "x",
                    "--dsn",
                    "host=x",
                    "--graph-path",
                    str(tmp_path / "x.sqlite"),
                ]
            )
        assert "not both" in str(excinfo.value)


@pytest.mark.postgres
class TestTheEphemeralKekReasoningStillHolds:
    """The tripwire named in `key_registry_cli`'s docstring. If it fires, that reasoning broke.

    The module builds a Postgres backend with an EXPLICIT ephemeral key manager instead of
    asking operators to set MEMOTRON_ALLOW_EPHEMERAL_KEK, on the stated grounds that the
    registry stores plaintext rows and this process seals nothing. Two ways that can rot:
    someone adds a sealing command here, or someone removes the explicit key manager and
    reintroduces dependence on the process-wide flag.
    """

    def test_it_works_with_the_process_wide_flag_UNSET(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If this fails, the CLI has started depending on MEMOTRON_ALLOW_EPHEMERAL_KEK --
        a flag whose blast radius is every process that inherits it, not one invocation."""
        dsn = _postgres_dsn_or_skip()
        _reset_postgres_schema(dsn)
        monkeypatch.delenv("MEMOTRON_ALLOW_EPHEMERAL_KEK", raising=False)

        assert (
            _run(
                [
                    "key",
                    "bind",
                    "--alias",
                    "dw-noflag",
                    "--principal-id",
                    "p",
                    "--tenant-id",
                    "acme",
                    "--dsn",
                    dsn,
                    "--migrate",
                ]
            )
            == 0
        )
        assert _run(["key", "show", "--alias", "dw-noflag", "--dsn", dsn]) == 0

    def test_no_command_here_seals_anything(self) -> None:
        """A source-level tripwire, because the runtime one cannot see a future command.

        The ephemeral-KEK argument is ONLY valid while nothing here seals. If a sealing call
        appears, the key manager stops being decorative and an ephemeral one starts destroying
        data on the next process -- silently, which is what #123's guard exists to prevent.
        """
        source = pathlib.Path(key_registry_cli.__file__).read_text()
        body = source.split('"""', 2)[-1]  # skip the module docstring, which discusses sealing
        for forbidden in ("wrap_dek", "unwrap_dek", "seal_", "get_or_create_governance_key"):
            assert forbidden not in body, (
                f"{forbidden!r} appears in key_registry_cli: this module now seals something, so "
                "the explicit-ephemeral-KEK reasoning in its docstring is no longer valid. "
                "Either require a real key manager or set MEMOTRON_ALLOW_EPHEMERAL_KEK "
                "deliberately -- do not leave the ephemeral default in place."
            )
