"""``memotron key`` — the operator write path for the per-key registry. (#168)

DW-030 built `key_principals` and DW-026 reads it, but **nothing could write a row**:
`bind_key_principal` had zero callers outside its own test, so `principal_for_key_alias`
returned ``None`` for every alias and a fully-wired authentication path would still have
authenticated nobody. #126 / #137 could both have been completed in full and every caller
would have remained anonymous. This module is the missing write path.

WHY A CLI AND NOT AN ENDPOINT
-----------------------------
#168 named three options. The obvious one — an `admin_server` route — is the one that
cannot be taken yet: `admin_server` authenticates nobody (its principal is a class
attribute fixed at process start, `admin_server/__init__.py:2221`), so an endpoint that
binds aliases to tenants would let any caller bind *its own* key to *any* tenant. That is
strictly worse than having no write path at all.

The third option, deriving a row from whatever the gateway asserts the first time a key
appears, is **forbidden**: DW-018 refuses a tenant claim in team or key metadata because
both are self-asserted, and DW-027 measured that `/key/generate` and `/key/update` are not
admin-only, so a caller can set its own metadata. It is named here only so it is not
rediscovered as a shortcut.

So: an operator tool with no network surface, authorised by possession of database
credentials. Whoever can run this can already read and write every row in the store
directly; this adds no privilege, it only makes the operation correct and repeatable
instead of hand-written SQL. **That is the authorization rule, stated explicitly as #168
asked: there is no application-level authorization here, and there is deliberately no
remote surface to attack.**

ON THE EPHEMERAL-KEK GUARD (#123 / #130)
----------------------------------------
Constructing a Postgres backend without a key manager raises
(`storage/postgres/__init__.py:126-139`) — an ephemeral KEK is random per process, so
sealed content would be unreadable after any restart.

That guard does not apply to what this tool does, and the reason is worth stating rather
than worked around: **the registry stores plaintext rows and this process seals nothing.**
Verified by reading the constructor — the key manager is assigned to `self.key_manager` and
is not used at construction; no governance key is created or wrapped. So this module passes
an explicit ephemeral key manager rather than asking operators to set
``MEMOTRON_ALLOW_EPHEMERAL_KEK``, which is a process-wide flag whose blast radius is much
larger than one CLI invocation. Pass ``--kek-file`` to supply a real one.

If a future command here ever seals anything, that reasoning breaks and this must change.
`test_key_registry_cli.py::TestTheEphemeralKekReasoningStillHolds` is the tripwire.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from memotron.config import PrincipalRole, storage_settings_from_env
from memotron.storage.factory import create_storage_backend
from memotron.storage.settings import EngineSettings, StorageSettings

#: Commands that only read. Used to keep the "no writes on a read" promise checkable.
READ_ONLY_COMMANDS = frozenset({"show"})


def add_parser(commands: Any) -> None:
    """Register the ``key`` command group on an existing subparsers object."""
    key = commands.add_parser(
        "key",
        help="Bind a gateway key_alias to the principal it resolves to (operator only).",
        description=(
            "Operator write path for the per-key registry (#168). Authorised by possession "
            "of database credentials; there is no network surface and no application-level "
            "authorization. Reads the store from MEMOTRON_OPERATIONAL_STORE_DSN unless "
            "--dsn or --graph-path is given."
        ),
    )
    key_commands = key.add_subparsers(dest="key_command", required=True)

    for name, help_text in (
        ("bind", "Bind or re-bind an alias. Re-binding within one tenant is the rotation path."),
        ("show", "Print the principal an alias resolves to, or exit 1 if unbound."),
        ("unbind", "Revoke a binding. Exit 0 whether or not one existed; says which."),
    ):
        sub = key_commands.add_parser(name, help=help_text)
        sub.add_argument("--alias", required=True, help="The gateway key_alias. Globally unique.")
        sub.add_argument("--dsn", default="", help="Postgres DSN. Overrides the environment.")
        sub.add_argument("--graph-path", default="", help="SQLite path instead of Postgres.")
        sub.add_argument(
            "--kek-file",
            default="",
            help="Real key manager. Not needed: this tool seals nothing (see module docstring).",
        )
        sub.add_argument(
            "--migrate",
            action="store_true",
            help=(
                "Run pending migrations first. OFF by default: an operator laptop should not "
                "silently migrate a production store as a side effect of a lookup."
            ),
        )
        sub.add_argument("--json", action="store_true", help="Emit JSON instead of text.")

    bind = key_commands.choices["bind"]
    bind.add_argument("--principal-id", required=True)
    bind.add_argument("--tenant-id", required=True)
    bind.add_argument("--agent-id", default="")
    bind.add_argument(
        "--role",
        default=PrincipalRole.USER.value,
        choices=[r.value for r in PrincipalRole],
    )
    bind.add_argument("--default-scope-key", default="", help='e.g. "tenant:acme"')
    bind.add_argument(
        "--allowed-scope-key",
        action="append",
        default=[],
        metavar="KEY",
        help="Repeatable. The scope allowlist for this principal.",
    )


def _open_store(args: argparse.Namespace) -> Any:
    """Build the backend the operator asked for, or explain what is missing.

    Precedence mirrors the application: an explicit DSN, then the environment, then
    SQLite. `--graph-path` is for local verification, not for a deployed store.
    """
    if args.graph_path and args.dsn:
        raise SystemExit("error: pass --dsn or --graph-path, not both")

    key_manager = None
    if args.kek_file:
        from memotron.crypto import LocalKeyManager

        key_manager = LocalKeyManager.from_file(args.kek_file)

    if args.graph_path:
        settings = StorageSettings(backend=EngineSettings(engine="sqlite", url=args.graph_path))
    elif args.dsn:
        settings = StorageSettings(backend=EngineSettings(engine="postgres", url=args.dsn))
    else:
        from_env = storage_settings_from_env()
        if from_env is None:
            raise SystemExit(
                "error: no store configured. Set MEMOTRON_OPERATIONAL_STORE_DSN, or pass\n"
                "       --dsn '<postgres dsn>' for a deployed store, or --graph-path <file>\n"
                "       for a local SQLite one."
            )
        settings = from_env

    backend = settings.backend
    if backend is not None and backend.engine == "postgres":
        options = dict(backend.options)
        options["migrate"] = bool(args.migrate)
        if key_manager is None:
            # Safe here and ONLY here: this tool seals nothing. See the module docstring.
            from memotron.crypto import LocalKeyManager

            key_manager = LocalKeyManager.ephemeral()
        settings = StorageSettings(backend=EngineSettings(engine="postgres", url=backend.url, options=options))

    return create_storage_backend(settings, key_manager=key_manager)


def _emit(payload: dict[str, Any] | None, text: str, *, as_json: bool) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else text)


def _missing_table_hint(exc: Exception) -> str | None:
    """Turn the raw driver error for an unmigrated store into an instruction."""
    message = str(exc)
    if "key_principals" in message and ("does not exist" in message or "no such table" in message):
        return (
            "error: the key_principals table does not exist in this store.\n"
            "       It arrives in Postgres migration 9. Re-run with --migrate to apply\n"
            "       pending migrations, having confirmed that is safe for this database."
        )
    return None


def run(args: argparse.Namespace) -> int:
    """Execute one ``key`` subcommand. Returns the process exit code."""
    store = _open_store(args)
    try:
        if args.key_command == "show":
            found = store.principal_for_key_alias(args.alias)
            if found is None:
                _emit(None, f"unbound: {args.alias!r} resolves to no principal", as_json=args.json)
                return 1
            _emit(dict(found), _format_principal(found), as_json=args.json)
            return 0

        if args.key_command == "unbind":
            removed = store.unbind_key_principal(args.alias)
            _emit(
                {"key_alias": args.alias, "removed": removed},
                f"{'unbound' if removed else 'no binding existed for'} {args.alias!r}",
                as_json=args.json,
            )
            return 0

        # bind
        before = store.principal_for_key_alias(args.alias)
        row = store.bind_key_principal(
            key_alias=args.alias,
            principal_id=args.principal_id,
            tenant_id=args.tenant_id,
            agent_id=args.agent_id or None,
            role=args.role,
            default_scope_key=args.default_scope_key or None,
            allowed_scope_keys=tuple(args.allowed_scope_key),
        )
        verb = "rebound (rotation)" if before is not None else "bound"
        _emit(dict(row), f"{verb}: {_format_principal(row)}", as_json=args.json)
        return 0
    except Exception as exc:  # the operator needs the reason, not a traceback
        hint = _missing_table_hint(exc)
        if hint:
            print(hint, file=sys.stderr)
            return 2
        raise
    finally:
        store.close()


def _format_principal(row: dict[str, Any]) -> str:
    scopes = ",".join(row.get("allowed_scope_keys") or ()) or "-"
    return (
        f"{row['key_alias']} -> principal={row['principal_id']} tenant={row['tenant_id']} "
        f"role={row['role']} agent={row.get('agent_id') or '-'} "
        f"default_scope={row.get('default_scope_key') or '-'} allowed_scopes={scopes}"
    )
