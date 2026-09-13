"""Memotron admin HTTP server and platform API.

Run against an existing graph file after building the React admin UI:

  uv run -m memotron.admin_server --graph-path .memotron/support-poc.sqlite --scope customer:wdw:pinnacle-events
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation-only: `from __future__ import annotations` makes this a string at
    # runtime, so importing it at module scope would only add `Callable` to the public
    # surface for nothing -- the same needless growth caught on `Sequence` earlier today.
    from collections.abc import Callable

    from memotron.models import DreamDecisionRecord
from threading import Lock, Thread
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from memotron import (
    Memotron,
    MemoryControlPlane,
    MemoryPrincipal,
    MemoryScope,
    PrincipalRole,
    ProfilePolicy,
    ScopeKind,
    ScopeMemoryPolicy,
)
from memotron.admin_server._demo import (
    DEMO_PROMPT_PACK as DEMO_PROMPT_PACK,
)
from memotron.admin_server._demo import (
    DEMO_TENANT_ID as DEMO_TENANT_ID,
)
from memotron.admin_server._demo import (
    apply_persisted_tenant_policy as apply_persisted_tenant_policy,
)
from memotron.admin_server._demo import (
    build_demo_control_plane as build_demo_control_plane,
)
from memotron.admin_server._demo import (
    build_demo_principal as build_demo_principal,
)
from memotron.admin_server._demo import (
    operator_tenant_detail as operator_tenant_detail,
)
from memotron.admin_server._demo import (
    resolve_launch_tenant_id as resolve_launch_tenant_id,
)
from memotron.admin_server._errors import (
    HttpApiError as HttpApiError,
)
from memotron.admin_server._motive import (
    _default_motive_name as _default_motive_name,
)
from memotron.admin_server._motive import (
    _motive_source_label as _motive_source_label,
)
from memotron.admin_server._motive import (
    default_motive_payload as default_motive_payload,
)
from memotron.admin_server._motive import (
    motive_options_payload as motive_options_payload,
)
from memotron.admin_server._motive import (
    resolve_scope_motive as resolve_scope_motive,
)
from memotron.admin_server._motive import (
    scope_motive_evidence as scope_motive_evidence,
)
from memotron.admin_server._parsing import (
    first as first,
)
from memotron.admin_server._parsing import (
    parse_bool as parse_bool,
)
from memotron.admin_server._parsing import (
    parse_csv as parse_csv,
)
from memotron.admin_server._parsing import (
    parse_datetime as parse_datetime,
)
from memotron.admin_server._parsing import (
    parse_lines as parse_lines,
)
from memotron.admin_server._parsing import (
    parse_scope_key as parse_scope_key,
)
from memotron.admin_server._parsing import (
    parse_statuses as parse_statuses,
)
from memotron.admin_server._payloads import (
    discover_graph_scopes as discover_graph_scopes,
)
from memotron.admin_server._payloads import (
    graph_filter_options as graph_filter_options,
)
from memotron.admin_server._payloads import (
    persisted_agent_motive as persisted_agent_motive,
)
from memotron.admin_server._payloads import (
    scope_payload_from_client as scope_payload_from_client,
)
from memotron.admin_server._policy import (
    _platform_url as _platform_url,
)
from memotron.admin_server._policy import (
    effective_policy_payload as effective_policy_payload,
)
from memotron.admin_server._snippets import (
    MCP_NOT_CONFIGURED_SNIPPET as MCP_NOT_CONFIGURED_SNIPPET,
)
from memotron.admin_server._snippets import (
    codex_mcp_config_snippet as codex_mcp_config_snippet,
)
from memotron.admin_server._snippets import (
    mcp_lifecycle_snippet as mcp_lifecycle_snippet,
)
from memotron.admin_server._snippets import (
    sdk_lifecycle_snippet as sdk_lifecycle_snippet,
)
from memotron.admin_server._tenant import (
    _environment_llm_key_env as _environment_llm_key_env,
)
from memotron.admin_server._tenant import (
    effective_llm_status as effective_llm_status,
)
from memotron.admin_server._tenant import (
    tenant_config_payload as tenant_config_payload,
)
from memotron.admin_server._tenant import (
    tenant_prompts_payload as tenant_prompts_payload,
)
from memotron.agent_memory import (
    TENANT_ACTIVE_PROMPT_PACK,
    TENANT_ACTIVE_PROMPT_PROFILE,
    TENANT_ACTIVE_PROMPT_PROFILE_VERSION,
    AgentMemoryPlatform,
    agent_scope,
)
from memotron.config import storage_settings_from_env
from memotron.crypto import key_manager_from_env
from memotron.gateway_identity import (
    GatewayIdentityRefusalError,
    GatewayPrincipalResolver,
    IdentityCache,
    gateway_key_from_headers,
    resolve_gateway_identity,
)
from memotron.graph import DREAM_CLAIM_STALE_SECONDS
from memotron.identity import identity_required
from memotron.models import RelationshipStatus
from memotron.observability import configure_observability
from memotron.storage import StorageBackend, create_storage_backend

DEMO_AGENT_ID = "memory-admin"
# parents[3], not [2]: this file moved from src/memotron/admin_server.py to
# src/memotron/admin_server/__init__.py, which adds a directory level. The
# index is counted from THIS file to the repo root and it is fragile by
# construction -- the only `__file__` in all of src/, and nothing about a wrong
# value raises. tests/test_module_import_contract.py pins the resolved path for
# exactly that reason, and it is what caught this on the rename commit.
#
# Worth knowing separately: this is ALSO wrong in an installed wheel, where it
# resolves under site-packages and the directory does not exist. That is
# pre-existing (session 2), unrelated to the index, and is why the published
# wheel cannot serve its own admin UI.
DEFAULT_ADMIN_STATIC_DIR = Path(__file__).resolve().parents[3] / "ui" / "admin" / "dist"


def _flush_print(message: str) -> None:
    """print, flushed. The callers are servers whose stdout is a pipe, not a tty."""
    print(message, flush=True)


def static_build_warning(static_dir: Path) -> str | None:
    """Why the admin UI will not serve from `static_dir`, or None if it will. T2-10.

    Both entry points validate the GRAPH path and exit if it is missing
    (`main()` below), and neither validates the STATIC dir at all. The asymmetry cost
    five days: a dogfood server whose `--static-dir` pointed into a git worktree that was
    later deleted served `GET /` as 503 that whole time while `/api/*` answered normally.
    Nothing reported it -- `HttpApiError` carries only a status, `log_message` is silenced,
    this module has no logger, and `/health` never consults the static dir, so every probe
    stayed green.

    Checks `index.html` rather than the directory, because that is what `_send_static_asset`
    actually serves for `/`. A present-but-empty directory currently yields a bare
    `404 {"error":"not found"}` indistinguishable from the path-traversal rejection, which
    is the harder of the two to diagnose; a bare `exists()` check would miss it.

    Returns a message rather than raising, deliberately. Mirroring the graph path's
    `SystemExit` would be more consistent, but the Helm chart passes no `--static-dir` and
    relies on `parents[3]` coinciding with the Dockerfile's copy target, so a hard failure
    would turn a degraded UI into an admin-pod crashloop the first time that layout changed.
    A broken UI on a working API is not a reason to take the API down.
    """
    resolved = Path(static_dir).expanduser().resolve()
    if (resolved / "index.html").is_file():
        return None
    detail = "directory does not exist" if not resolved.is_dir() else "no index.html in it"
    return (
        f"admin UI will NOT serve: {resolved} -- {detail}. "
        f"`GET /` will return 503 while the API keeps working. "
        f"Run `npm --prefix ui/admin run build`, or pass --static-dir."
    )


def warn_if_admin_build_missing(static_dir: Path, *, echo: Callable[[str], None] = _flush_print) -> bool:
    """Emit the T2-10 warning if `static_dir` cannot serve the console. Returns whether it did.

    Split from `static_build_warning` so the EMISSION is testable, not just the message. Both
    entry points call this on one line, which keeps the part that only runs inside `main()`
    -- and which no unit test can execute, since `main()` binds a port and serves forever --
    down to that single line. `diff-cover` is what surfaced the difference: with the message
    check and the printing inlined in both `main()`s, six lines of this change were
    unreachable by any test.

    `echo` defaults to a flushing print because the default caller is a long-lived server
    whose stdout is a pipe, not a tty. See the note in `local_platform.main`.
    """
    warning = static_build_warning(static_dir)
    if warning:
        echo(f"WARNING: {warning}")
    return warning is not None


_log = logging.getLogger("memotron.admin_server")


#: The agent-memory HTTP API. Everything under this prefix is the PRODUCT's own surface --
#: `README.md` documents it as the HTTP equivalent of the MCP tools, and an agent calls
#: `bootstrap` / `start` / `search` at session startup. `--no-admin-writes` must not touch it.
AGENT_MEMORY_ROUTE_PREFIX = "/api/platform/"


def _is_tenant_admin_route(path: str) -> bool:
    """True for the tenant-ADMINISTRATION POST routes, which `--no-admin-writes` refuses.

    THE DEFAULT IS "REFUSE". The narrow, explicitly-named set is what stays *open*
    (`/api/platform/`), and everything else is administration. That direction is deliberate:
    a POST route added tomorrow is refused until someone decides otherwise, rather than being
    quietly permitted because no one updated a denylist. `test_read_only_admin.py` derives the
    full route list from the AST and asserts both halves, so a new route cannot land unnoticed
    in either direction.

    An earlier version of this guard refused **every** POST, and that was wrong for a reason
    worth recording: the 28 routes are not one population. Fifteen are the agent-memory API
    above -- including `search` and `start`, which are reads, and `bootstrap`, which is how an
    agent session begins -- so a blanket refusal took down a documented public API on every
    environment to close an exposure that lives entirely in the other thirteen.

    The objection that killed the first allowlist attempt ("classifying 28 routes correctly is
    error-prone, and `remember`/`publish` read like queries but are writes") was right about a
    SEMANTIC read/write split and does not apply here. This is a structural prefix, not a
    judgement call per route.
    """
    return not path.startswith(AGENT_MEMORY_ROUTE_PREFIX)


class MemoryGraphHandler(BaseHTTPRequestHandler):
    client: Memotron
    runtime_client: Memotron | None = None
    platform: AgentMemoryPlatform | None = None
    default_scope: MemoryScope
    # EITHER a SQLite file path OR an open operational-store backend, never both.
    # `graph_path` is None whenever the server runs on the operational store, so every
    # read of it must tolerate that -- see `_store_kwargs`.
    graph_path: Path | None = None
    store: StorageBackend | None = None
    control_plane: MemoryControlPlane
    # The FALLBACK principal, and the only one before #50: a class attribute set once by
    # `main()`, identical for every caller. Still used verbatim for the console routes and
    # whenever identity is not required -- see `_caller_principal`.
    principal: MemoryPrincipal
    # Per-request identity for `/api/platform/*` only. `None` leaves the surface exactly as
    # it was, which is the rollout default and what every existing test constructs.
    principal_resolver: GatewayPrincipalResolver | None = None
    tenant_id: str = ""
    agent_ids: tuple[str, ...] = ()
    configured_scopes: tuple[MemoryScope, ...] = ()
    tenant_warnings: tuple[str, ...] = ()
    # A KILL SWITCH, not a permission system. When true the tenant-ADMINISTRATION POST
    # routes are refused with 405 before dispatch -- see `do_POST` and
    # `_is_tenant_admin_route`. The agent-memory API under `/api/platform/` is untouched.
    # Default False so no existing caller changes.
    no_admin_writes: bool = False
    ui_url: str = "http://127.0.0.1:8765/"
    mcp_url: str = ""
    static_dir: Path = DEFAULT_ADMIN_STATIC_DIR
    # Deliberately process-wide, not per-request: one status map shared by every
    # handler instance, guarded by the lock above. ClassVar states that.
    dream_status_lock: ClassVar[Lock] = Lock()
    dream_statuses: ClassVar[dict[str, dict[str, Any]]] = {}

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path in {"/", "/memory-graph"}:
                self._send_static_asset("index.html")
            elif parsed.path == "/favicon.ico":
                self._send_no_content()
            elif parsed.path == "/health":
                # GCLB health checks require exactly 200 (204 marks the backend unhealthy)
                self._send_json({"status": "ok"})
            elif parsed.path == "/api/graph":
                self._send_json(self._graph_payload(query))
            elif parsed.path == "/api/scopes":
                self._send_json({"scopes": self._scope_payload()})
            elif parsed.path == "/api/overview":
                self._send_json(asyncio.run(self._overview_payload(query)))
            elif parsed.path == "/api/filter-options":
                self._send_json(asyncio.run(self._filter_options_payload(query)))
            elif parsed.path == "/api/profile":
                self._send_json(asyncio.run(self._profile_payload(query)))
            elif parsed.path == "/api/control-plane":
                self._send_json(self._control_plane_payload(query))
            elif parsed.path == "/api/policy-rollout":
                self._send_json(self._policy_rollout_payload(query))
            elif parsed.path == "/api/tenant-config":
                self._send_json(self._tenant_config_payload())
            elif parsed.path == "/api/tenant-prompts":
                self._send_json(self._tenant_prompts_payload(query))
            elif parsed.path == "/api/platform/status":
                self._send_json(self._platform_status_payload())
            elif parsed.path == "/api/platform/integration-contract":
                self._send_json(self._platform_integration_contract_payload())
            elif parsed.path == "/api/platform/project-memory/config":
                self._send_json(self._platform().project_memory_status())
            elif parsed.path == "/api/evidence":
                self._send_json(asyncio.run(self._evidence_payload(query)))
            elif parsed.path == "/api/timeline":
                self._send_json(asyncio.run(self._timeline_payload(query)))
            elif parsed.path == "/api/evolution":
                self._send_json(asyncio.run(self._evolution_payload(query)))
            elif parsed.path == "/api/neighborhood":
                self._send_json(asyncio.run(self._neighborhood_payload(query)))
            elif parsed.path == "/api/dream-runs":
                self._send_json(asyncio.run(self._dream_runs_payload(query)))
            elif parsed.path == "/api/dream-sequence/status":
                self._send_json(self._dream_sequence_status_payload(query))
            elif parsed.path == "/api/archive":
                self._send_json(asyncio.run(self._archive_payload(query)))
            elif parsed.path == "/api/adjudication/reviews":
                self._send_json(asyncio.run(self._pending_reviews_payload(query)))
            elif parsed.path == "/api/adjudication/entities":
                self._send_json(asyncio.run(self._pending_entity_aliases_payload(query)))
            elif parsed.path == "/api/epochs":
                self._send_json(asyncio.run(self._epochs_payload(query)))
            elif parsed.path == "/api/epochs/diff":
                self._send_json(asyncio.run(self._epoch_diff_payload(query)))
            elif parsed.path.startswith("/api/"):
                self._send_json({"error": "not found"}, status=404)
            else:
                self._send_static_asset(parsed.path.lstrip("/"))
        except HttpApiError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
        except ValueError as exc:
            # Deliberate, caller-facing validation raised as a plain ValueError:
            # "relationship_uuid is required", "scope must use kind:id format", and 5
            # others (4 in this module, 3 in _parsing). HttpApiError IS a ValueError
            # subclass (_errors.py:13), so this branch is the same family, just
            # without an explicit status. Kept at 400 WITH the message -- these are
            # written for a human and telling a caller which field is missing is the
            # point. Converting the 7 raises to HttpApiError would let this branch go.
            self._send_json({"error": str(exc)}, status=400)
        except Exception:
            self._send_internal_error()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        # BEFORE dispatch, and before the request body is read, so a refused call cannot reach
        # a payload method at all. That ordering is the point on `/api/tenant-config/purge`: a
        # guard running after dispatch would return 405 with the tenant's graph already gone.
        if self.no_admin_writes and _is_tenant_admin_route(parsed.path):
            self._send_json(
                {
                    "error": ("this admin server runs --no-admin-writes; tenant-administration routes are refused"),
                    "path": parsed.path,
                },
                status=405,
            )
            return
        try:
            if parsed.path == "/api/tenant-config/llm":
                self._send_json(self._configure_tenant_llm_payload())
            elif parsed.path == "/api/tenant-config/llm/clear":
                self._send_json(self._clear_tenant_llm_payload())
            elif parsed.path == "/api/tenant-config/purge":
                self._send_json(self._purge_tenant_state_payload())
            elif parsed.path == "/api/tenant-prompts":
                self._send_json(self._save_tenant_prompts_payload())
            elif parsed.path == "/api/tenant-prompts/rerun":
                self._send_json(asyncio.run(self._rerun_tenant_prompts_payload()))
            elif parsed.path == "/api/dream-sequence/run":
                self._send_json(self._start_dream_sequence_payload())
            elif parsed.path == "/api/platform/memory/bootstrap":
                self._send_json(asyncio.run(self._platform_memory_bootstrap_payload()))
            elif parsed.path == "/api/platform/agents/register":
                self._send_json(self._platform_agent_register_payload())
            elif parsed.path == "/api/platform/memory/start":
                self._send_json(asyncio.run(self._platform_memory_start_payload()))
            elif parsed.path == "/api/platform/memory/search":
                self._send_json(asyncio.run(self._platform_memory_search_payload()))
            elif parsed.path == "/api/platform/memory/remember":
                self._send_json(asyncio.run(self._platform_memory_remember_payload()))
            elif parsed.path == "/api/platform/memory/publish":
                self._send_json(asyncio.run(self._platform_memory_publish_payload()))
            elif parsed.path == "/api/platform/memory/promote":
                self._send_json(asyncio.run(self._platform_memory_promote_payload()))
            elif parsed.path == "/api/platform/memory/endorse-promotion":
                self._send_json(asyncio.run(self._platform_memory_endorse_promotion_payload()))
            elif parsed.path == "/api/platform/memory/set-visibility":
                self._send_json(asyncio.run(self._platform_memory_set_visibility_payload()))
            elif parsed.path == "/api/platform/project-memory/config":
                self._send_json(self._platform_project_memory_configure_payload())
            elif parsed.path == "/api/platform/memory/log":
                self._send_json(asyncio.run(self._platform_memory_log_payload()))
            elif parsed.path == "/api/platform/memory/outcome":
                self._send_json(asyncio.run(self._platform_memory_outcome_payload()))
            elif parsed.path == "/api/platform/memory/utility":
                self._send_json(asyncio.run(self._platform_memory_utility_payload()))
            elif parsed.path == "/api/platform/memory/refresh":
                self._send_json(asyncio.run(self._platform_memory_refresh_payload()))
            elif parsed.path == "/api/platform/memory/evolution":
                self._send_json(asyncio.run(self._platform_memory_evolution_payload()))
            elif parsed.path == "/api/memory/pin":
                self._send_json(asyncio.run(self._pin_memory_payload(pin=True)))
            elif parsed.path == "/api/memory/unpin":
                self._send_json(asyncio.run(self._pin_memory_payload(pin=False)))
            elif parsed.path == "/api/memory/visibility":
                self._send_json(asyncio.run(self._memory_visibility_payload()))
            elif parsed.path == "/api/adjudication/reviews/resolve":
                self._send_json(asyncio.run(self._resolve_review_payload()))
            elif parsed.path == "/api/adjudication/entities/resolve":
                self._send_json(asyncio.run(self._resolve_entity_alias_payload()))
            elif parsed.path == "/api/adjudication/incidents/resolve":
                self._send_json(asyncio.run(self._resolve_incident_payload()))
            elif parsed.path == "/api/adjudication/disambiguation/resolve":
                self._send_json(asyncio.run(self._resolve_disambiguation_payload()))
            else:
                self._send_json({"error": "not found"}, status=404)
        except HttpApiError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
        except ValueError as exc:
            # Deliberate, caller-facing validation raised as a plain ValueError:
            # "relationship_uuid is required", "scope must use kind:id format", and 5
            # others (4 in this module, 3 in _parsing). HttpApiError IS a ValueError
            # subclass (_errors.py:13), so this branch is the same family, just
            # without an explicit status. Kept at 400 WITH the message -- these are
            # written for a human and telling a caller which field is missing is the
            # point. Converting the 7 raises to HttpApiError would let this branch go.
            self._send_json({"error": str(exc)}, status=400)
        except Exception:
            self._send_internal_error()

    def _request_scope(self, query: dict[str, list[str]]) -> MemoryScope:
        raw_scope = first(query, "scope")
        requested_scope = parse_scope_key(raw_scope) if raw_scope else None
        return self._request_policy(requested_scope).scope

    def _request_policy(self, requested_scope: MemoryScope | None):
        if self.client.control_plane is None:
            self.client.control_plane = self.control_plane
        platform = getattr(self, "platform", None)
        if platform is not None:
            if requested_scope is not None and requested_scope.kind == ScopeKind.AGENT:
                platform.agent_scope(requested_scope.scope_id)
            self.control_plane = self.client.control_plane
        return self.client.resolve_policy_for_principal(
            principal=self.principal,
            scope=requested_scope,
        )

    def _register_admin_scope(self, scope: MemoryScope) -> None:
        tenant = self.control_plane.tenant(self._tenant_id())
        if scope.key in tenant.registered_scope_keys():
            return
        default_policy = tenant.scope_policy(self.default_scope)
        fallback_motive = default_policy.motive if default_policy else tenant.default_motive
        motive = fallback_motive
        if scope.kind == ScopeKind.AGENT and tenant.memory_bank is not None:
            # An agent scope is governed by its persisted assignment, never by the
            # default scope's Motive.
            candidate = persisted_agent_motive(
                self.client,
                tenant_id=tenant.tenant_id,
                agent_id=scope.scope_id,
            )
            if candidate in tenant.memory_bank.names:
                motive = candidate
        scope_policy = ScopeMemoryPolicy(
            scope=scope,
            dream_mode=default_policy.dream_mode if default_policy else None,
            prompt_pack=default_policy.prompt_pack if default_policy else None,
            motive=motive,
        )
        updated_tenant = tenant.model_copy(update={"scopes": (*tenant.scopes, scope_policy)})
        self.control_plane = MemoryControlPlane(
            base_config=self.control_plane.base_config,
            tenants=tuple(
                updated_tenant if candidate.tenant_id == tenant.tenant_id else candidate
                for candidate in self.control_plane.tenants
            ),
        )

    def _graph_payload(self, query: dict[str, list[str]]) -> dict:
        limit_raw = first(query, "limit")
        limit = int(limit_raw) if limit_raw else 250
        view = asyncio.run(
            self.client.knowledge_graph(
                scope=self._request_scope(query),
                limit=limit,
                relationship_types=parse_csv(first(query, "types")),
                as_of=parse_datetime(first(query, "as_of")),
                include_statuses=parse_statuses(first(query, "statuses")),
                include_demoted=parse_bool(first(query, "include_demoted"), default=True),
                query=first(query, "query"),
            )
        )
        return view.model_dump(mode="json")

    def _scope_payload(self) -> list[dict]:
        authorized: list[dict] = []
        seen: set[str] = set()
        for item in scope_payload_from_client(self.client):
            scope = parse_scope_key(item["key"])
            try:
                self._request_policy(scope)
            except ValueError:
                continue
            authorized.append(item)
            seen.add(item["key"])
        dynamic_agent_scopes = tuple(agent_scope(agent_id) for agent_id in self._agent_ids())
        for scope in (self.default_scope, *getattr(self, "configured_scopes", ()), *dynamic_agent_scopes):
            if scope.key in seen:
                continue
            try:
                if self.principal.role == PrincipalRole.ADMIN and scope.kind == ScopeKind.AGENT:
                    self.client.resolve_policy(
                        tenant_id=self._tenant_id(),
                        agent_id=scope.scope_id,
                        scope=scope,
                    )
                else:
                    self._request_policy(scope)
                authorized.append(
                    {
                        "key": scope.key,
                        "kind": scope.kind.value,
                        "scope_id": scope.scope_id,
                        "relationship_count": 0,
                    }
                )
                seen.add(scope.key)
            except ValueError:
                pass
        return sorted(authorized, key=lambda item: item["key"])

    def _control_plane_payload(self, query: dict[str, list[str]]) -> dict:
        policy = self._request_policy(self._request_scope(query))
        return effective_policy_payload(policy, self.principal)

    def _policy_rollout_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """Read-only certification and policy-alias evidence for the operator UI."""
        scope = self._request_scope(query)
        alias = (first(query, "alias") or "production").strip()
        if not alias:
            raise HttpApiError(400, "policy alias cannot be blank")
        rollout = self.client.policy_rollout_status(scope=scope, alias=alias)
        active = rollout["active_contract"]
        active_payload = active.model_dump(mode="json") if active is not None else None
        certification = None
        routing = None
        if active is not None:
            certification = {
                "passed": active.certification_passed,
                "corpus_name": active.certification.corpus_name,
                "corpus_digest": active.certification.corpus_digest,
                "policy_delta": active.certification.policy_delta,
                "replay_flip_rate": active.certification.replay_flip_rate,
                "stability_score": active.certification.stability_score,
                "failures": list(active.certification.failures),
                "protected_invariants": [
                    item.model_dump(mode="json") for item in active.certification.protected_invariants
                ],
            }
            routing = {
                "production_scope": scope.key,
                "motive": active.motive.name,
                "allowed_memory_types": [item.value for item in active.motive.allowed_memory_types],
                "shadow_visibility": "zero user visibility; isolated replay stores only",
            }
        return {
            "scope": {"key": scope.key, **scope.model_dump(mode="json")},
            "alias": alias,
            "active_alias": (
                {
                    "scope": {
                        "key": scope.key,
                        **rollout["alias"].scope.model_dump(mode="json"),
                    },
                    "alias": rollout["alias"].alias,
                    "contract_digest": rollout["alias"].contract_digest,
                    "previous_contract_digest": rollout["alias"].previous_contract_digest,
                    "updated_at": rollout["alias"].updated_at.isoformat(),
                }
                if rollout["alias"] is not None
                else None
            ),
            "compiled_effective_policy": active_payload,
            "expected_episode_routing": routing,
            "certification": certification,
            "shadow_stages": [item.model_dump(mode="json") for item in rollout["shadow_stages"]],
        }

    async def _filter_options_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        scope = self._request_scope(query)
        return {
            "scope": scope.model_dump(mode="json"),
            "options": await graph_filter_options(self.client, scope),
        }

    def _store_kwargs(self) -> dict[str, Any]:
        """The `graph_path=` / `store=` kwarg pair for helpers that accept either.

        Every caller below used to hardcode `graph_path=self.graph_path`, which is None
        when this server runs on the operational store. Centralised so a new call site
        cannot quietly reintroduce the file-only assumption.
        """
        if self.store is not None:
            return {"store": self.store}
        return {"graph_path": self.graph_path}

    def _tenant_config_payload(self) -> dict[str, Any]:
        platform = getattr(self, "platform", None)
        return tenant_config_payload(
            client=self.client,
            tenant_id=self._tenant_id(),
            agent_ids=self._agent_ids(),
            graph_path=self.graph_path,
            default_scope=self.default_scope,
            ui_url=self.ui_url,
            mcp_url=self.mcp_url,
            mode=platform.mode.value if platform is not None else "simple",
            user_id=(
                platform.user_scope.scope_id if platform is not None and platform.user_scope is not None else None
            ),
            warnings=tuple(getattr(self, "tenant_warnings", ()) or ()),
        )

    def _tenant_prompts_payload(
        self,
        query: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        scope = self.default_scope
        agent_id = ""
        if query:
            raw_scope = first(query, "scope")
            if raw_scope:
                scope = self._request_scope(query)
            agent_id = (first(query, "agent_id") or "").strip()
        return tenant_prompts_payload(
            self.client,
            self._tenant_id(),
            scope=scope,
            agent_id=agent_id,
        )

    async def _overview_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        tenant_config = self._tenant_config_payload()
        scopes = self._scope_payload()
        scope = self._request_scope(query)
        # Resolved ONCE and shared: `_tenant_overview_scopes` calls `export_graph`, a full
        # graph walk, so letting both consumers resolve it independently doubles that per
        # request. The storage-call golden is what surfaced the second walk.
        authorized_scopes = self._tenant_overview_scopes() if self._is_tenant_overview_scope(scope) else (scope,)
        memory_payload, evolution_payload = await self._overview_memory_and_evolution_payload(
            scope=scope,
            as_of=parse_datetime(first(query, "as_of")),
            authorized_scopes=authorized_scopes,
        )
        latest_runs = await self.client.dream_history(limit=1)
        latest_decisions = self._latest_authorized_decisions(authorized_scopes)
        llm = tenant_config["llm"]
        return {
            "tenant": tenant_config["tenant"],
            "operator": tenant_config["operator"],
            "llm": llm,
            "readiness": {
                "llm_configured": bool(llm.get("has_api_key")),
                "agents_observed": bool(tenant_config["tenant"].get("agent_ids")),
                "scopes_registered": bool(scopes),
                # #217: this tested `bool(platform_api_url)`, which CANNOT BE FALSE --
                # `ui_url` is assigned unconditionally in `main()` (an f-string before
                # #242, `_advertised_url(...)` after, which also always returns a
                # string). So the console asserted the API was ready in exactly the
                # environments where all 18 `/api/platform/*` routes answered 503.
                #
                # It now tests the SAME predicate `_platform()` uses to decide that 503
                # (`admin_server/__init__.py` -- platform absent => 503), so readiness
                # and behaviour cannot disagree. `getattr` rather than attribute access
                # because `platform` is a ClassVar default that `main()` overwrites, and
                # a bare read would raise where it is unset instead of reporting False.
                "platform_api_ready": getattr(self, "platform", None) is not None,
                # NOT the same fix, deliberately. This one is honest as written: the
                # admin process cannot know whether a REMOTE MCP server is up without
                # probing it, so the most it can report is that an endpoint is
                # advertised. The name overstates that, and #242 made it newly true on
                # `latest` by setting `--mcp-url` -- but the alternative is a liveness
                # probe from inside a console request, which is worse. Left as-is with
                # the limit stated rather than quietly renamed.
                "mcp_ready": bool(tenant_config["operator"].get("mcp_url")),
            },
            "scope": scope.model_dump(mode="json"),
            "scopes": scopes,
            "memory": memory_payload,
            "evolution": {
                "dream_run_count": evolution_payload["dream_run_count"],
                "decision_count": evolution_payload["decision_count"],
                "latest_dream_run": latest_runs[0].model_dump(mode="json") if latest_runs else None,
                "latest_decision": latest_decisions[0].model_dump(mode="json") if latest_decisions else None,
                "signals": evolution_payload["signals"],
            },
        }

    async def _overview_memory_and_evolution_payload(
        self,
        *,
        scope: MemoryScope,
        as_of: datetime | None,
        authorized_scopes: tuple[MemoryScope, ...],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._is_tenant_overview_scope(scope):
            proofs = [
                await self.client.memory_evolution(scope=candidate, as_of=as_of) for candidate in authorized_scopes
            ]
            return self._aggregate_overview_payloads(proofs)
        proof = await self.client.memory_evolution(scope=scope, as_of=as_of)
        return (
            self._memory_payload_from_proof(proof),
            {
                "dream_run_count": proof.dream_run_count,
                "decision_count": proof.decision_count,
                "signals": [signal.model_dump(mode="json") for signal in proof.signals],
            },
        )

    def _latest_authorized_decisions(self, authorized_scopes: tuple[MemoryScope, ...]) -> list[DreamDecisionRecord]:
        """The newest dream decision the CALLER's scopes can see — never the store's.

        `client.dream_decisions(limit=1)` takes no scope and returns the newest row in
        the store. `DreamDecisionRecord` carries `scope`, `summary` and `subject_name`,
        so on a surface that authenticates nobody that is a neighbouring tenant's memory
        CONTENT. Observed on deployed `latest` 2026-09-08, unauthenticated and
        off-cluster, returning a different foreign tenant on each of two probes.

        It survived #200 because #200 bounded the scope REGISTRY that `_request_policy`
        consults, and this path never consulted it — the same data reached by a second
        call path. `dream_decisions_for_scope` already existed on the storage contract.

        `dream_history` above is deliberately left alone: `DreamJobRunRecord` has no
        scope field and no scoped variant exists, so it exposes job names and counters
        rather than any tenant's content. Bounding it is a different change.
        """
        decisions: dict[str, DreamDecisionRecord] = {}
        for candidate in authorized_scopes:
            for record in self.client.graph.dream_decisions_for_scope(candidate.key):
                decisions[record.uuid] = record
        newest = sorted(decisions.values(), key=lambda record: (record.ran_at, record.uuid), reverse=True)
        return newest[:1]

    def _is_tenant_overview_scope(self, scope: MemoryScope) -> bool:
        return scope.kind == ScopeKind.TENANT and scope.scope_id == self._tenant_id()

    def _tenant_overview_scopes(self) -> tuple[MemoryScope, ...]:
        tenant_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id=self._tenant_id())
        registered_agents = set(self._agent_ids())
        scopes: dict[str, MemoryScope] = {tenant_scope.key: tenant_scope}
        for agent_id in registered_agents:
            scope = agent_scope(agent_id)
            scopes[scope.key] = scope
        for item in scope_payload_from_client(self.client):
            scope = parse_scope_key(str(item["key"]))
            if (scope.kind == ScopeKind.TENANT and scope.scope_id == self._tenant_id()) or (
                scope.kind == ScopeKind.AGENT and (not registered_agents or scope.scope_id in registered_agents)
            ):
                scopes[scope.key] = scope
        authorized: list[MemoryScope] = []
        for scope in scopes.values():
            try:
                self._request_policy(scope)
            except ValueError:
                continue
            authorized.append(scope)
        return tuple(sorted(authorized, key=lambda item: item.key))

    def _aggregate_overview_payloads(self, proofs: list[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        episode_count = sum(proof.episode_count for proof in proofs)
        pending_episode_count = sum(proof.pending_episode_count for proof in proofs)
        visible_count = sum(proof.context_visible_relationship_count for proof in proofs)
        active_count = sum(proof.active_relationship_count for proof in proofs)
        inactive_count = sum(proof.inactive_relationship_count for proof in proofs)
        rollup_count = sum(proof.rollup_relationship_count for proof in proofs)
        demoted_count = sum(proof.demoted_relationship_count for proof in proofs)
        per_type: dict[str, int] = {}
        facts = []
        for proof in proofs:
            facts.extend([*proof.active_facts, *proof.inactive_facts])
            for memory_type, count in proof.per_type_active_counts.items():
                per_type[memory_type] = per_type.get(memory_type, 0) + count
        total_observations = sum(fact.observed_count for fact in facts)
        semantic_dedup_rate = max(0, total_observations - len(facts)) / max(1, total_observations)
        memory_payload = {
            "visible_facts": visible_count,
            "active_facts": active_count,
            "inactive_facts": inactive_count,
            "episodes": episode_count,
            "pending_episodes": pending_episode_count,
            "compression_ratio": episode_count / max(1, visible_count),
            "semantic_dedup_rate": semantic_dedup_rate,
            "rollups": rollup_count,
            "demoted": demoted_count,
            "per_type_active_counts": per_type,
        }
        evolution_payload = {
            "dream_run_count": max((proof.dream_run_count for proof in proofs), default=0),
            "decision_count": sum(proof.decision_count for proof in proofs),
            "signals": [
                signal.model_dump(mode="json") for proof in proofs for signal in proof.signals if signal.observed
            ],
        }
        return memory_payload, evolution_payload

    def _memory_payload_from_proof(self, proof: Any) -> dict[str, Any]:
        return {
            "visible_facts": proof.context_visible_relationship_count,
            "active_facts": proof.active_relationship_count,
            "inactive_facts": proof.inactive_relationship_count,
            "episodes": proof.episode_count,
            "pending_episodes": proof.pending_episode_count,
            "compression_ratio": proof.compression_ratio,
            "semantic_dedup_rate": proof.semantic_dedup_rate,
            "rollups": proof.rollup_relationship_count,
            "demoted": proof.demoted_relationship_count,
            "per_type_active_counts": proof.per_type_active_counts,
        }

    def _platform_status_payload(self) -> dict[str, Any]:
        platform = self._platform()
        agent_ids = list(self._agent_ids())
        registered_agents = list(self.client.graph.tenant_agents(platform.tenant_id))
        return {
            "tenant_id": platform.tenant_id,
            "mode": platform.mode.value,
            "agent_ids": agent_ids,
            "registered_agents": registered_agents,
            "project_scope": platform.project_scope.model_dump(mode="json"),
            "project_scope_key": platform.project_scope.key,
            "personal_scope": (
                platform.user_scope.model_dump(mode="json") if platform.user_scope is not None else None
            ),
            "integration_contract_url": _platform_url(
                self.ui_url,
                "/api/platform/integration-contract",
            ),
            "llm": effective_llm_status(
                self.client.tenant_llm_credential_state(platform.tenant_id)
                or {"tenant_id": platform.tenant_id, "has_api_key": False}
            ),
        }

    def _platform_integration_contract_payload(self) -> dict[str, Any]:
        platform = self._platform()
        return platform.integration_contract(
            platform_api_url=self.ui_url,
            mcp_url=self.mcp_url,
        )

    def _platform_agent_register_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(payload, allowed={"agent_id", "agent_name"})
        result = self._platform().register_agent(
            agent_id=self._required_string(payload, "agent_id"),
            agent_name=self._required_string(payload, "agent_name"),
            source="hosted-api",
        )
        self.control_plane = self._platform().client.control_plane
        self._register_admin_scope(agent_scope(result.agent_id))
        self.client.control_plane = self.control_plane
        return result.model_dump(mode="json")

    async def _platform_memory_bootstrap_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={
                "agent_id",
                "agent_name",
                "agent_token_budget",
                "project_token_budget",
                "task_run_id",
            },
        )
        result = await self._platform().memory_bootstrap(
            agent_id=self._required_string(payload, "agent_id"),
            agent_name=self._required_string(payload, "agent_name"),
            agent_token_budget=self._optional_int(
                payload,
                "agent_token_budget",
                default=1200,
            ),
            project_token_budget=self._optional_int(
                payload,
                "project_token_budget",
                default=800,
            ),
            task_run_id=self._optional_string(payload, "task_run_id") or None,
            source="hosted-api",
        )
        self.control_plane = self._platform().client.control_plane
        self._register_admin_scope(agent_scope(result.registration.agent_id))
        self.client.control_plane = self.control_plane
        return result.model_dump(mode="json")

    async def _platform_memory_start_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={
                "agent_id",
                "agent_token_budget",
                "project_token_budget",
                "task_run_id",
            },
        )
        result = await self._platform().memory_start(
            agent_id=self._required_string(payload, "agent_id"),
            agent_token_budget=self._optional_int(payload, "agent_token_budget", default=1200),
            project_token_budget=self._optional_int(payload, "project_token_budget", default=800),
            task_run_id=self._optional_string(payload, "task_run_id") or None,
        )
        return result.model_dump(mode="json")

    async def _platform_memory_search_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={"agent_id", "query", "include_project", "limit", "task_run_id"},
        )
        result = await self._platform().memory_search(
            agent_id=self._required_string(payload, "agent_id"),
            query=self._required_string(payload, "query"),
            include_project=self._optional_bool(payload, "include_project", default=True),
            limit=self._optional_int(payload, "limit", default=10),
            task_run_id=self._optional_string(payload, "task_run_id") or None,
        )
        return result.model_dump(mode="json")

    async def _platform_memory_remember_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={
                "agent_id",
                "subject",
                "predicate",
                "object",
                "relationship_type",
                "source_text",
                "confidence",
            },
        )
        result = await self._platform().memory_remember(
            agent_id=self._required_string(payload, "agent_id"),
            subject=self._required_string(payload, "subject"),
            predicate=self._required_string(payload, "predicate"),
            object=self._required_string(payload, "object"),
            relationship_type=self._required_string(payload, "relationship_type"),
            source_text=self._optional_string(payload, "source_text"),
            confidence=self._optional_float(payload, "confidence", default=0.9),
        )
        return result.model_dump(mode="json")

    async def _platform_memory_publish_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={
                "agent_id",
                "content",
                "task_run_id",
                "source_reference",
            },
        )
        result = await self._platform().memory_publish(
            agent_id=self._required_string(payload, "agent_id"),
            content=self._required_string(payload, "content"),
            task_run_id=self._required_string(payload, "task_run_id"),
            source_reference=self._optional_string(payload, "source_reference"),
        )
        return result.model_dump(mode="json")

    async def _platform_memory_promote_payload(self) -> dict[str, Any]:
        """WS-19 T20: object-level promotion of one own-scope fact."""
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={"agent_id", "relationship_uuid", "rationale", "task_run_id"},
        )
        result = await self._platform().memory_promote(
            agent_id=self._principal_bound_agent_id(payload),
            relationship_uuid=self._required_string(payload, "relationship_uuid"),
            rationale=self._required_string(payload, "rationale"),
            task_run_id=self._optional_string(payload, "task_run_id"),
        )
        return result.model_dump(mode="json")

    async def _platform_memory_endorse_promotion_payload(self) -> dict[str, Any]:
        """WS-19 T20: one agent's vote on a pending promotion candidate."""
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={"agent_id", "candidate_episode_uuid", "rationale"},
        )
        result = await self._platform().memory_endorse_promotion(
            agent_id=self._principal_bound_agent_id(payload),
            candidate_episode_uuid=self._required_string(payload, "candidate_episode_uuid"),
            rationale=self._required_string(payload, "rationale"),
        )
        return result.model_dump(mode="json")

    async def _platform_memory_set_visibility_payload(self) -> dict[str, Any]:
        """WS-19 T22: agent-facing allowlist on a row in the caller's own scope."""
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={"agent_id", "relationship_uuid", "agents", "reason", "scope"},
        )
        agents = payload.get("agents")
        if agents is not None:
            agents = self._optional_string_list(payload, "agents")
        result = await self._platform().memory_set_visibility(
            agent_id=self._principal_bound_agent_id(payload),
            relationship_uuid=self._required_string(payload, "relationship_uuid"),
            agents=agents,
            reason=self._required_string(payload, "reason"),
            scope=self._optional_string(payload, "scope") or "default",
        )
        return result.model_dump(mode="json")

    def _platform_project_memory_configure_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={
                "project_goal",
                "memory_goal",
                "keep",
                "exclude",
                "rules",
                "allowed_memory_types",
                "protected_memory_types",
                "min_salience",
                "max_memories_per_candidate",
                "dedup_threshold",
                "min_endorsements",
                "configured_by",
            },
        )
        allowed_memory_types = (
            self._optional_string_list(payload, "allowed_memory_types")
            if "allowed_memory_types" in payload
            else [
                "requirement",
                "directive",
                "state",
                "decision",
                "incident",
                "rollup",
            ]
        )
        protected_memory_types = (
            self._optional_string_list(payload, "protected_memory_types")
            if "protected_memory_types" in payload
            else ["requirement", "decision", "incident"]
        )
        result = self._platform().configure_project_memory(
            project_goal=self._required_string(payload, "project_goal"),
            memory_goal=self._required_string(payload, "memory_goal"),
            keep=self._optional_string_list(payload, "keep"),
            exclude=self._optional_string_list(payload, "exclude"),
            rules=self._optional_string_list(payload, "rules"),
            allowed_memory_types=allowed_memory_types,
            protected_memory_types=protected_memory_types,
            min_salience=self._optional_float(
                payload,
                "min_salience",
                default=0.0,
            ),
            max_memories_per_candidate=self._optional_int(
                payload,
                "max_memories_per_candidate",
                default=12,
            ),
            dedup_threshold=self._optional_float(
                payload,
                "dedup_threshold",
                default=0.87,
            ),
            min_endorsements=self._optional_int(
                payload,
                "min_endorsements",
                default=1,
            ),
            configured_by=self._required_string(payload, "configured_by"),
        )
        self.control_plane = self._platform().client.control_plane
        self.client.control_plane = self.control_plane
        runtime_client = getattr(self, "runtime_client", None)
        if runtime_client is not None and runtime_client is not self.client:
            AgentMemoryPlatform.apply_project_memory_config_to_client(
                client=runtime_client,
                tenant_id=self._tenant_id(),
            )
        return result.model_dump(mode="json")

    async def _platform_memory_log_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={
                "agent_id",
                "summary",
                "decisions",
                "incidents",
                "blockers",
                "checkpoint_reason",
                "task_run_id",
            },
        )
        result = await self._platform().memory_log(
            agent_id=self._required_string(payload, "agent_id"),
            summary=self._optional_string(payload, "summary"),
            decisions=self._optional_string_list(payload, "decisions"),
            incidents=self._optional_string_list(payload, "incidents"),
            blockers=self._optional_string_list(payload, "blockers"),
            checkpoint_reason=self._optional_string(payload, "checkpoint_reason"),
            task_run_id=self._optional_string(payload, "task_run_id"),
        )
        return result.model_dump(mode="json")

    async def _platform_memory_outcome_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={
                "agent_id",
                "use_id",
                "verdict",
                "task_run_id",
                "idempotency_key",
                "judge_identity",
                "judge_version",
                "scope",
                "attribution_method",
            },
        )
        result = await self._platform().memory_outcome(
            agent_id=self._required_string(payload, "agent_id"),
            use_id=self._required_string(payload, "use_id"),
            verdict=self._required_string(payload, "verdict"),
            task_run_id=self._required_string(payload, "task_run_id"),
            idempotency_key=self._required_string(payload, "idempotency_key"),
            judge_identity=self._required_string(payload, "judge_identity"),
            judge_version=self._required_string(payload, "judge_version"),
            scope=self._optional_string(payload, "scope") or "default",
            attribution_method=(self._optional_string(payload, "attribution_method") or "cited_full_credit"),
        )
        return result.model_dump(mode="json")

    async def _platform_memory_utility_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={"agent_id", "relationship_uuid", "scope"},
        )
        result = await self._platform().memory_utility(
            agent_id=self._required_string(payload, "agent_id"),
            relationship_uuid=self._optional_string(payload, "relationship_uuid"),
            scope=self._optional_string(payload, "scope") or "default",
        )
        return {"items": [item.model_dump(mode="json") for item in result]}

    async def _platform_memory_refresh_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(payload, allowed={"agent_id", "include_project"})
        result = await self._platform().memory_refresh(
            agent_id=self._required_string(payload, "agent_id"),
            include_project=self._optional_bool(payload, "include_project", default=True),
        )
        return result.model_dump(mode="json")

    async def _platform_memory_evolution_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(payload, allowed={"agent_id", "include_project"})
        result = await self._platform().memory_evolution(
            agent_id=self._required_string(payload, "agent_id"),
            include_project=self._optional_bool(payload, "include_project", default=False),
        )
        return result.model_dump(mode="json")

    def _platform(self) -> AgentMemoryPlatform:
        platform = getattr(self, "platform", None)
        if platform is None:
            raise HttpApiError(503, "Memotron platform API is not enabled for this server")
        # #206 Phase 2 -- the caller check for the HTTP transport, placed HERE because
        # `_platform()` is the one accessor all 17 `/api/platform/*` handlers share.
        #
        # Two of them do NOT delegate to `AgentMemoryPlatform` and so would inherit
        # nothing from the platform-level guard: `_platform_status_payload` reads
        # `graph.tenant_agents` and `client.tenant_llm_credential_state` directly, and
        # `_platform_integration_contract_payload` calls no platform method at all.
        # Neither is a cross-tenant leak -- both read this process's own tenant -- but
        # both would otherwise answer an unauthenticated caller with the tenant's agent
        # roster and whether an LLM credential is configured.
        #
        # The other 15 are checked twice, here and again inside the platform. That is
        # deliberate: the duplicate is idempotent, and a guard that depends on which of
        # two layers you entered through is the kind that develops holes.
        #
        # This does NOT read the platform's `principal_provider`. admin_server carries
        # its own identity (`self.principal`) and imports nothing from `mcp_auth`; the
        # provider seam exists for transports that cannot be reached from the platform.
        # Delegated to the platform's guard rather than reimplemented, and the principal
        # is passed in because admin_server holds its own. That keeps ONE implementation
        # and keeps `mcp_auth` out of this module -- importing it here, even inside the
        # function, is an upward tier edge `coupling_report.py` fails on.
        # #50 -- and this is the line that makes the guard below mean something. It used
        # to read `self.principal`, a CLASS attribute set once by `main()`, so the check
        # compared a principal's tenant against itself: it could refuse nothing, because
        # there was only ever one principal and it was ours. `_caller_principal` resolves
        # the REQUEST's gateway key instead, so the tenant being compared is the caller's.
        caller = self._caller_principal()
        try:
            platform._require_caller_tenant("platform API", provider=lambda: caller)
        except Exception as exc:  # ScopeNotAuthorized / IdentityUnavailable, both 403-shaped
            raise HttpApiError(403, str(exc)) from exc
        return platform

    def _caller_principal(self) -> MemoryPrincipal:
        """The principal for THIS request, resolved from its gateway key.

        Reached only from `_platform()`, so only the 17 `/api/platform/*` routes are
        authenticated. **That boundary is deliberate and it is a product constraint, not
        an oversight**: the console is a browser app and `ui/admin/src/api.ts` sends no
        credential at all, so requiring one on `/api/overview` would blank the UI on every
        environment where the flag is armed -- `latest` today. A guard that refuses
        everything is its own outage. Browser auth is #23 (SSO); this closes the
        PROGRAMMATIC surface, which is the one that can carry a key, and which #194 cannot
        safely enable until something here authenticates a caller.

        Falls back to the configured `self.principal` when identity is not required or no
        resolver was wired -- the same rollout default as the MCP middleware, so with the
        flag unset this method is byte-for-byte the previous behaviour.

        **Deliberately not memoised on `self`.** `BaseHTTPRequestHandler` reuses one
        instance for every request on a keep-alive connection, so a cached principal would
        be served to the NEXT request on that socket -- a different caller, possibly a
        different tenant, silently inheriting the first one's identity. The gateway lookup
        it repeats is TTL-cached inside the resolver, and the principal lookup is a local
        indexed read that must stay uncached anyway so `memotron key unbind` takes
        effect on the next request.
        """
        resolver = getattr(self, "principal_resolver", None)
        if resolver is None or not identity_required():
            return self.principal

        try:
            key = gateway_key_from_headers(self._sole_header)
            if not key:
                raise GatewayIdentityRefusalError(401, "no gateway key on the request")
            identity = resolver.identity_for_key(key)
            principal = resolver.principal_for_identity(identity)
        except GatewayIdentityRefusalError as refusal:
            # Full reason to the log, `public_detail` to the caller -- the split exists
            # because the gateway's error body was observed carrying `Received API Key
            # = sk-...`. Same discipline as the ASGI middleware; `HttpApiError` is how
            # this transport renders a status, and both `do_GET` and `do_POST` catch it.
            _log.warning(
                "admin platform identity refused: status=%s path=%s reason=%s",
                refusal.status,
                self.path,
                refusal.detail,
            )
            raise HttpApiError(refusal.status, refusal.public_detail) from refusal

        _log.info(
            "admin platform identity resolved: principal=%s tenant=%s alias=%s",
            principal.principal_id,
            principal.tenant_id,
            identity.key_alias,
        )
        return principal

    def _sole_header(self, name: str) -> str | None:
        """One header value, or ``None``; refuses when the request disagrees with itself.

        The `http.server` counterpart to `mcp_auth._sole_header`, and it refuses duplicates
        for the same reason: a request carrying two different credentials has no defensible
        interpretation, and picking one silently means an audit line can name a key other
        than the one actually authorized.
        """
        values = self.headers.get_all(name) or []
        if not values:
            return None
        if len({value.strip() for value in values}) > 1:
            raise GatewayIdentityRefusalError(401, f"{name} was sent more than once with different values")
        return values[0]

    def _validate_motive_name(self, motive_name: str) -> None:
        motives = {motive["name"] for motive in motive_options_payload(self.client, self._tenant_id())}
        if motive_name not in motives:
            raise HttpApiError(400, f"unknown motive_name {motive_name!r}")

    def _resolve_dream_request(
        self,
        *,
        raw_scope: str | None,
        raw_agent_id: str | None,
    ) -> tuple[MemoryScope, str]:
        scope = parse_scope_key(raw_scope) if raw_scope else self.default_scope
        agent_id = raw_agent_id or None
        platform = getattr(self, "platform", None)
        if agent_id:
            if platform is not None:
                platform.agent_scope(agent_id)
            self._register_admin_scope(agent_scope(agent_id))
        elif scope.kind == ScopeKind.AGENT:
            agent_id = scope.scope_id
            if platform is not None:
                platform.agent_scope(agent_id)
            self._register_admin_scope(agent_scope(agent_id))
        else:
            observed_agents = self._agent_ids()
            if not observed_agents:
                raise HttpApiError(400, "dream sequence requires an observed agent_id")
            agent_id = observed_agents[0]
        if self.principal.role == PrincipalRole.ADMIN:
            self.client.resolve_policy(
                tenant_id=self._tenant_id(),
                agent_id=agent_id,
                scope=scope,
            )
        else:
            self._request_policy(scope)
        return scope, agent_id

    def _configure_tenant_llm_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={"provider", "api_key", "base_url", "model"},
        )
        provider = self._required_string(payload, "provider")
        api_key = self._required_string(payload, "api_key")
        base_url = self._optional_string(payload, "base_url")
        model = self._optional_string(payload, "model")
        self.client.configure_tenant_llm_credentials(
            tenant_id=self._tenant_id(),
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        self._apply_runtime_llm(provider=provider, api_key=api_key, base_url=base_url, model=model)
        return self._tenant_config_payload()

    def _clear_tenant_llm_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(payload, allowed=set())
        cleared = self.client.clear_tenant_llm_credentials(self._tenant_id())
        if cleared:
            self._clear_runtime_llm()
        return {"cleared": cleared, "config": self._tenant_config_payload()}

    def _purge_tenant_state_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(payload, allowed=set())
        tenant_id = self._tenant_id()
        agent_ids = self._tenant_purge_agent_ids()
        purged = self.client.graph.purge_tenant_state(tenant_id, agent_ids=agent_ids)
        AgentMemoryPlatform.clear_tenant_prompt_from_client(client=self.client, tenant_id=tenant_id)
        runtime_client = getattr(self, "runtime_client", None)
        if runtime_client is not None and runtime_client is not self.client:
            AgentMemoryPlatform.clear_tenant_prompt_from_client(client=runtime_client, tenant_id=tenant_id)
        platform = getattr(self, "platform", None)
        if platform is not None:
            agent_id_set = set(agent_ids)
            platform.agent_ids = tuple(agent_id for agent_id in platform.agent_ids if agent_id not in agent_id_set)
        with self.dream_status_lock:
            self.dream_statuses.clear()
        return {
            "tenant_id": tenant_id,
            "purged": purged,
            "config": self._tenant_config_payload(),
            "prompts": self._tenant_prompts_payload(),
        }

    def _save_tenant_prompts_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={
                "prompt_text",
                "motive_name",
                "source_profile",
                "source_profile_version",
            },
        )
        prompt_text = self._required_string(payload, "prompt_text")
        motive_name = self._optional_string(payload, "motive_name")
        if motive_name:
            self._validate_motive_name(motive_name)
        source_profile = self._optional_string(payload, "source_profile") or TENANT_ACTIVE_PROMPT_PROFILE
        source_profile_version = (
            self._optional_string(payload, "source_profile_version") or TENANT_ACTIVE_PROMPT_PROFILE_VERSION
        )
        state = self.client.graph.save_tenant_prompt_version(
            tenant_id=self._tenant_id(),
            prompt_text=prompt_text,
            motive_name=motive_name or "",
            source_profile=source_profile,
            source_profile_version=source_profile_version,
        )
        AgentMemoryPlatform.apply_tenant_prompt_to_client(
            client=self.client,
            tenant_id=self._tenant_id(),
        )
        AgentMemoryPlatform.apply_project_memory_config_to_client(
            client=self.client,
            tenant_id=self._tenant_id(),
        )
        runtime_client = getattr(self, "runtime_client", None)
        if runtime_client is not None and runtime_client is not self.client:
            AgentMemoryPlatform.apply_tenant_prompt_to_client(
                client=runtime_client,
                tenant_id=self._tenant_id(),
            )
            AgentMemoryPlatform.apply_project_memory_config_to_client(
                client=runtime_client,
                tenant_id=self._tenant_id(),
            )
        response = self._tenant_prompts_payload()
        response["saved"] = state
        return response

    async def _rerun_tenant_prompts_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(payload, allowed={"scope", "agent_id", "motive_name"})
        scope, agent_id = self._resolve_dream_request(
            raw_scope=self._optional_string(payload, "scope"),
            raw_agent_id=self._optional_string(payload, "agent_id"),
        )
        motive_name = self._optional_string(payload, "motive_name")
        if motive_name:
            self._validate_motive_name(motive_name)
        client = self.client
        prompt_pack = self._active_prompt_pack_name(client)
        result = await client.run_due_dreams(
            tenant_id=self._tenant_id(),
            agent_id=agent_id,
            scope=scope,
            motive=motive_name,
            prompt_pack=prompt_pack,
        )
        return {
            "scope": scope.model_dump(mode="json"),
            "agent_id": agent_id,
            "result": result.model_dump(mode="json"),
        }

    def _start_dream_sequence_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(payload, allowed={"scope", "agent_id", "motive_name"})
        scope, agent_id = self._resolve_dream_request(
            raw_scope=self._optional_string(payload, "scope"),
            raw_agent_id=self._optional_string(payload, "agent_id"),
        )
        requested_motive_name = self._optional_string(payload, "motive_name")
        if requested_motive_name:
            self._validate_motive_name(requested_motive_name)
        active_prompt_motive_name = self._active_prompt_motive_name()
        effective_motive_name = requested_motive_name or active_prompt_motive_name
        prompt_pack = self._active_prompt_pack_name(self.client)
        resolved_motive = self._resolve_dream_motive(
            scope=scope,
            agent_id=agent_id,
            requested_motive_name=requested_motive_name,
            active_prompt_motive_name=active_prompt_motive_name,
            prompt_pack=prompt_pack,
        )
        pending_episode_count = asyncio.run(self.client.memory_evolution(scope=scope)).pending_episode_count
        run_id = uuid4().hex
        status = {
            "run_id": run_id,
            "status": "queued",
            "running": True,
            "tenant_id": self._tenant_id(),
            "scope": scope.model_dump(mode="json"),
            "agent_id": agent_id,
            "motive_name": resolved_motive["name"],
            "motive_source": resolved_motive["source"],
            "motive_source_label": resolved_motive["source_label"],
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": None,
            "result": None,
            "error": "",
            "pending_episode_count": pending_episode_count,
            "processed_episodes": None,
            "no_work": None,
            "capability_signals": self._dream_capability_signals(),
            "events": [],
        }
        # Single-flight per tenant: the Run Dream Sequence button is idempotent
        # while a sequence is in flight.  A second POST returns the ACTIVE
        # run's run_id with status "already_running" (HTTP 200 — the client
        # polls the same run), and a fresh POST after completion starts a new
        # run.  The check and the insert share one critical section so two
        # racing POSTs cannot both start workers.  An active entry older than
        # the engine's claim staleness window no longer blocks — mirroring
        # episode-claim expiry — and the engine-level claims make even that
        # overlap safe.
        active_run = self._register_dream_run_single_flight(run_id, status)
        if active_run is not None:
            active_run["status"] = "already_running"
            return active_run
        self._append_dream_event(
            run_id,
            "queued",
            "Dream sequence queued by tenant admin UI.",
            {
                "scope": scope.key,
                "agent_id": agent_id,
                "motive_name": resolved_motive["name"],
                "motive_source": resolved_motive["source"],
            },
        )
        worker = Thread(
            target=self._run_dream_sequence_worker,
            args=(
                run_id,
                scope.model_dump(mode="json"),
                agent_id,
                effective_motive_name,
                resolved_motive["name"],
                resolved_motive["source"],
                prompt_pack,
            ),
            daemon=True,
        )
        worker.start()
        return self._dream_status(run_id)

    def _dream_sequence_status_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        run_id = first(query, "run_id")
        if run_id:
            status = self._dream_status(run_id)
            if not status:
                raise HttpApiError(404, f"unknown dream run_id {run_id!r}")
            return status
        with self.dream_status_lock:
            latest = sorted(
                self.dream_statuses.values(),
                key=lambda item: str(item.get("started_at", "")),
                reverse=True,
            )
            return {"runs": [json.loads(json.dumps(item)) for item in latest[:20]]}

    def _run_dream_sequence_worker(
        self,
        run_id: str,
        scope_payload: dict[str, Any],
        agent_id: str,
        effective_motive_name: str,
        resolved_motive_name: str,
        motive_source: str,
        prompt_pack: str | None,
    ) -> None:
        worker_platform: AgentMemoryPlatform | None = None
        try:
            scope = MemoryScope.model_validate(scope_payload)
            self._update_dream_status(run_id, status="running")
            self._append_dream_event(
                run_id,
                "policy",
                "Resolving tenant policy, active prompt pack, and Motive.",
                {
                    "prompt_pack": prompt_pack or "",
                    "motive_name": resolved_motive_name,
                    "motive_source": motive_source,
                    "policy": "Motive selects what memories are worth forming before graph mutation.",
                },
            )
            from memotron.runtime import build_transports_from_tenant_graph

            extraction_transport, dream_agent_transport = build_transports_from_tenant_graph(
                **self._store_kwargs(),
                tenant_id=self._tenant_id(),
            )
            worker_platform = AgentMemoryPlatform.create(
                **({"storage": self.store} if self.store is not None else {"graph_path": self.graph_path}),
                project_id=self._tenant_id(),
                extraction_transport=extraction_transport,
                dream_agent_transport=dream_agent_transport,
                mode=self._platform().mode,
                user_id=(self._platform().user_scope.scope_id if self._platform().user_scope is not None else None),
            )
            worker_platform.agent_scope(agent_id)
            AgentMemoryPlatform.apply_tenant_prompt_to_client(
                client=worker_platform.client,
                tenant_id=self._tenant_id(),
            )
            before = asyncio.run(worker_platform.client.memory_evolution(scope=scope))
            self._update_dream_status(run_id, pending_episode_count=before.pending_episode_count)
            self._append_dream_event(
                run_id,
                "before",
                "Captured before-state memory evolution proof.",
                {
                    "episodes": before.episode_count,
                    "pending": before.pending_episode_count,
                    "active_facts": before.active_relationship_count,
                    "compression_ratio": before.compression_ratio,
                },
            )
            self._append_dream_event(
                run_id,
                "formation",
                "Running due formation, consolidation, and pruning jobs.",
                {
                    "write_side_semantic_dedup": "paraphrases reinforce existing truth rows",
                    "temporal_truth": "single-active slots supersede stale facts",
                    "dream_agent": "offline actions record auditable decisions",
                },
            )
            result = asyncio.run(
                worker_platform.client.run_due_dreams(
                    tenant_id=self._tenant_id(),
                    agent_id=agent_id,
                    scope=scope,
                    motive=effective_motive_name or None,
                    prompt_pack=prompt_pack,
                )
            )
            for job_run in result.job_runs:
                self._append_dream_event(
                    run_id,
                    "job",
                    f"Completed {job_run.job_kind.value} job.",
                    {
                        "job_name": job_run.job_name,
                        "processed_episodes": job_run.processed_episodes,
                        "created_relationships": job_run.created_relationships,
                        "reinforced_relationships": job_run.reinforced_relationships,
                        "superseded_relationships": job_run.superseded_relationships,
                        "pruned_relationships": job_run.pruned_relationships,
                    },
                )
            if not result.job_runs:
                self._append_dream_event(
                    run_id,
                    "job",
                    "No dream jobs were due for the selected scope.",
                    {"processed_episodes": 0},
                )
            after = asyncio.run(worker_platform.client.memory_evolution(scope=scope))
            self._append_dream_event(
                run_id,
                "after",
                "Captured after-state memory evolution proof.",
                {
                    "episodes": after.episode_count,
                    "pending": after.pending_episode_count,
                    "active_facts": after.active_relationship_count,
                    "compression_ratio": after.compression_ratio,
                    "semantic_dedup_rate": after.semantic_dedup_rate,
                    "rollups": after.rollup_relationship_count,
                    "demoted": after.demoted_relationship_count,
                },
            )
            processed_episodes = result.processed_episodes
            self._update_dream_status(
                run_id,
                status="completed",
                running=False,
                completed_at=datetime.now(UTC).isoformat(),
                result=result.model_dump(mode="json"),
                # "ran, nothing to do" vs "ran, processed N" — explicit fields
                # so a client never has to derive the distinction from job runs.
                processed_episodes=processed_episodes,
                no_work=processed_episodes == 0,
                pending_episode_count=after.pending_episode_count,
            )
        except BaseException as exc:
            self._append_dream_event(
                run_id,
                "error",
                "Dream sequence failed.",
                {"error": str(exc)},
            )
            self._update_dream_status(
                run_id,
                status="error",
                running=False,
                completed_at=datetime.now(UTC).isoformat(),
                error=str(exc),
            )
        finally:
            if worker_platform is not None:
                worker_platform.client.graph.close()

    def _active_prompt_pack_name(self, client: Memotron) -> str | None:
        if (
            client.graph.active_tenant_prompt_version(self._tenant_id()) is not None
            or client.graph.tenant_prompt_override(self._tenant_id()) is not None
        ):
            return TENANT_ACTIVE_PROMPT_PACK
        return None

    def _active_prompt_motive_name(self) -> str:
        active_prompt = self.client.graph.active_tenant_prompt_version(self._tenant_id())
        if active_prompt is not None and str(active_prompt.get("motive_name") or ""):
            return str(active_prompt["motive_name"])
        return ""

    def _resolve_dream_motive(
        self,
        *,
        scope: MemoryScope,
        agent_id: str,
        requested_motive_name: str,
        active_prompt_motive_name: str,
        prompt_pack: str | None,
    ) -> dict[str, str]:
        return resolve_scope_motive(
            client=self.client,
            tenant_id=self._tenant_id(),
            scope=scope,
            agent_id=agent_id,
            requested_motive_name=requested_motive_name,
            active_prompt_motive_name=active_prompt_motive_name,
            prompt_pack=prompt_pack,
        )

    def _dream_capability_signals(self) -> list[dict[str, str]]:
        return [
            {
                "name": "Motive policy",
                "description": "A named Motive chooses the memory-making goal, allowed types, salience rubric, and dedup threshold.",
            },
            {
                "name": "Before/after proof",
                "description": "Dreaming captures memory evolution metrics before and after each sequence.",
            },
            {
                "name": "Semantic dedup",
                "description": "Similar observations reinforce durable graph facts instead of accumulating duplicates.",
            },
            {
                "name": "Temporal truth",
                "description": "Single-active truth slots retain lineage while replacing stale facts.",
            },
            {
                "name": "Dream-agent audit",
                "description": "Offline actions emit decision records for approval, pruning, consolidation, and repair.",
            },
        ]

    def _register_dream_run_single_flight(self, run_id: str, status: dict[str, Any]) -> dict[str, Any] | None:
        """Insert *status* unless the same tenant already has a live run.

        Returns a copy of the ACTIVE run's status when one exists (the caller
        answers ``already_running``), else ``None`` after registering the new
        run.  Check and insert share the lock so two racing POSTs cannot both
        register.  A run whose ``started_at`` is older than the engine's claim
        staleness window is treated as dead rather than blocking forever.
        """
        tenant_id = str(status.get("tenant_id", ""))
        stale_cutoff = datetime.now(UTC) - timedelta(seconds=DREAM_CLAIM_STALE_SECONDS)
        with self.dream_status_lock:
            for existing in self.dream_statuses.values():
                if not existing.get("running"):
                    continue
                if str(existing.get("tenant_id", "")) != tenant_id:
                    continue
                started_raw = str(existing.get("started_at", ""))
                try:
                    started_at = datetime.fromisoformat(started_raw)
                except ValueError:
                    started_at = stale_cutoff
                if started_at <= stale_cutoff:
                    continue
                return json.loads(json.dumps(existing))
            self.dream_statuses[run_id] = status
        return None

    def _dream_status(self, run_id: str) -> dict[str, Any]:
        with self.dream_status_lock:
            status = self.dream_statuses.get(run_id)
            return json.loads(json.dumps(status)) if status is not None else {}

    def _update_dream_status(self, run_id: str, **updates: Any) -> None:
        with self.dream_status_lock:
            current = self.dream_statuses.get(run_id)
            if current is None:
                return
            current.update(updates)

    def _append_dream_event(
        self,
        run_id: str,
        phase: str,
        message: str,
        details: dict[str, Any],
    ) -> None:
        event = {
            "at": datetime.now(UTC).isoformat(),
            "phase": phase,
            "message": message,
            "details": details,
        }
        with self.dream_status_lock:
            current = self.dream_statuses.get(run_id)
            if current is None:
                return
            current.setdefault("events", []).append(event)

    def _apply_runtime_llm(self, *, provider: str, api_key: str, base_url: str, model: str) -> None:
        from memotron.runtime import build_transports_from_credentials

        extraction_transport, dream_agent_transport = build_transports_from_credentials(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        for client in self._runtime_transport_clients():
            client.set_runtime_transports(
                extraction_transport=extraction_transport,
                dream_agent_transport=dream_agent_transport,
            )

    def _clear_runtime_llm(self) -> None:
        from memotron.runtime import build_transports_from_env

        extraction_transport, dream_agent_transport = build_transports_from_env()
        for client in self._runtime_transport_clients():
            client.set_runtime_transports(
                extraction_transport=extraction_transport,
                dream_agent_transport=dream_agent_transport,
            )

    def _runtime_transport_clients(self) -> tuple[Memotron, ...]:
        runtime_client = getattr(self, "runtime_client", None)
        if runtime_client is None or runtime_client is self.client:
            return (self.client,)
        return (self.client, runtime_client)

    def _read_json_body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HttpApiError(415, "Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length", "").strip()
        if not raw_length:
            raise HttpApiError(400, "JSON request body is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise HttpApiError(400, "Content-Length must be an integer") from exc
        if length <= 0:
            raise HttpApiError(400, "JSON request body is required")
        if length > 65536:
            raise HttpApiError(413, "JSON request body is too large")
        raw_body = self.rfile.read(length)
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise HttpApiError(400, "JSON request body must be UTF-8") from exc
        except json.JSONDecodeError as exc:
            raise HttpApiError(400, "request body must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise HttpApiError(400, "JSON request body must be an object")
        return parsed

    @staticmethod
    def _reject_unknown_fields(payload: dict[str, Any], *, allowed: set[str]) -> None:
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise HttpApiError(400, f"unknown field(s): {', '.join(unknown)}")

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise HttpApiError(400, f"{key} is required")
        return value.strip()

    @staticmethod
    def _optional_string(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key, "")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise HttpApiError(400, f"{key} must be a string")
        return value.strip()

    @staticmethod
    def _optional_int(payload: dict[str, Any], key: str, *, default: int) -> int:
        value = payload.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise HttpApiError(400, f"{key} must be an integer")
        if value < 0:
            raise HttpApiError(400, f"{key} must be non-negative")
        return value

    @staticmethod
    def _optional_float(payload: dict[str, Any], key: str, *, default: float) -> float:
        value = payload.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise HttpApiError(400, f"{key} must be a number")
        return float(value)

    @staticmethod
    def _optional_bool(payload: dict[str, Any], key: str, *, default: bool) -> bool:
        value = payload.get(key, default)
        if not isinstance(value, bool):
            raise HttpApiError(400, f"{key} must be a boolean")
        return value

    @staticmethod
    def _optional_string_list(payload: dict[str, Any], key: str) -> tuple[str, ...]:
        value = payload.get(key)
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise HttpApiError(400, f"{key} must be an array of strings")
        return tuple(item.strip() for item in value if item.strip())

    def _tenant_id(self) -> str:
        return getattr(self, "tenant_id", None) or self.principal.tenant_id

    def _agent_ids(self) -> tuple[str, ...]:
        graph_agents = tuple(item["agent_id"] for item in self.client.graph.tenant_agents(self._tenant_id()))
        if graph_agents:
            return graph_agents
        if getattr(self, "platform", None) is not None:
            return ()
        agent_ids = getattr(self, "agent_ids", ())
        if agent_ids:
            return tuple(agent_ids)
        if self.principal.agent_id:
            return (self.principal.agent_id,)
        return (DEMO_AGENT_ID,)

    def _tenant_purge_agent_ids(self) -> tuple[str, ...]:
        agent_ids = set(self._agent_ids())
        agent_ids.update(str(agent_id) for agent_id in getattr(self, "agent_ids", ()) if str(agent_id).strip())
        if self.principal.agent_id:
            agent_ids.add(self.principal.agent_id)
        platform = getattr(self, "platform", None)
        if platform is not None:
            agent_ids.update(platform.agent_ids)
        return tuple(sorted(agent_ids))

    async def _profile_payload(self, query: dict[str, list[str]]) -> dict:
        profile = await self.client.profile(
            scope=self._request_scope(query),
            as_of=parse_datetime(first(query, "as_of")),
            policy=ProfilePolicy(
                max_static_facts=80,
                max_dynamic_facts=40,
                include_metadata=True,
                render_mode="typed",
                reference_mode=True,
            ),
        )
        return {"profile": profile.model_dump(mode="json")}

    async def _evidence_payload(self, query: dict[str, list[str]]) -> dict:
        relationship_uuid = first(query, "relationship_uuid")
        if not relationship_uuid:
            raise ValueError("relationship_uuid is required")
        evidence = await self.client.memory_evidence(
            relationship_uuid=relationship_uuid,
            scope=self._request_scope(query),
        )
        return evidence.model_dump(mode="json")

    async def _timeline_payload(self, query: dict[str, list[str]]) -> dict:
        subject = first(query, "subject")
        predicate = first(query, "predicate")
        if not subject:
            raise ValueError("subject is required")
        if not predicate:
            raise ValueError("predicate is required")
        entries = await self.client.truth_timeline(
            scope=self._request_scope(query),
            subject=subject,
            predicate=predicate,
            relationship_type=first(query, "relationship_type"),
            include_statuses=parse_statuses(first(query, "statuses")),
        )
        return {"entries": [entry.model_dump(mode="json") for entry in entries]}

    async def _evolution_payload(self, query: dict[str, list[str]]) -> dict:
        proof = await self.client.memory_evolution(
            scope=self._request_scope(query),
            as_of=parse_datetime(first(query, "as_of")),
        )
        return {"proof": proof.model_dump(mode="json")}

    async def _neighborhood_payload(self, query: dict[str, list[str]]) -> dict:
        entity = first(query, "entity")
        if not entity:
            raise ValueError("entity is required")
        edges = await self.client.entity_neighborhood(
            scope=self._request_scope(query),
            entity=entity,
            relationship_types=parse_csv(first(query, "types")),
            as_of=parse_datetime(first(query, "as_of")),
            include_statuses=parse_statuses(first(query, "statuses")),
        )
        return {"edges": [edge.model_dump(mode="json") for edge in edges]}

    async def _dream_runs_payload(self, query: dict[str, list[str]]) -> dict:
        limit_raw = first(query, "limit")
        limit = int(limit_raw) if limit_raw else 30
        history = await self.client.dream_history(limit=limit)
        decisions = await self.client.dream_decisions(limit=limit)
        statuses = await self.client.dream_status(now=parse_datetime(first(query, "as_of")))
        return {
            "runs": [await self._dream_run_payload(record) for record in history],
            "decisions": [record.model_dump(mode="json") for record in decisions],
            "statuses": [status.model_dump(mode="json") for status in statuses],
        }

    async def _dream_run_payload(self, record: Any) -> dict[str, Any]:
        """One dream-run history row, plus the Motive that governed it.

        ``DreamJobRunRecord`` (models.py) has no ``motive_name`` field -- it
        predates per-run Motive attribution. The receipt ledger (WS-11) does:
        every formation receipt for a run carries ``motive_name``. Joining it
        in here, at the read edge, leaves that persisted row's own schema
        untouched and never backfills a run that predates the receipt ledger
        or ran a non-formation job kind with a guess -- those render
        ``None`` (the UI shows "unrecorded"), never the CURRENT policy's
        Motive, which would misattribute history to a policy that did not
        govern it.
        """
        payload = record.model_dump(mode="json")
        motive_name: str | None = None
        run_uuid = getattr(record, "run_uuid", None)
        if run_uuid:
            receipts = await self.client.memory_receipts(run_uuid=run_uuid)
            motive_name = next(
                (receipt.motive_name for receipt in receipts if receipt.motive_name),
                None,
            )
        payload["motive_name"] = motive_name
        return payload

    async def _archive_payload(self, query: dict[str, list[str]]) -> dict:
        view = await self.client.knowledge_graph(
            scope=self._request_scope(query),
            limit=1000,
            as_of=parse_datetime(first(query, "as_of")),
            include_statuses={
                RelationshipStatus.ACTIVE,
                RelationshipStatus.SUPERSEDED,
                RelationshipStatus.PRUNED,
            },
            include_demoted=True,
            query=first(query, "query"),
        )
        facts = [
            {
                **node.model_dump(mode="json"),
                "fact": node.label,
            }
            for node in view.nodes
            if node.node_type == "memory"
            and (node.status != RelationshipStatus.ACTIVE or node.active_in_context is False)
        ]
        facts.sort(
            key=lambda fact: (
                fact.get("status") == RelationshipStatus.ACTIVE.value,
                fact.get("valid_from") or "",
                fact.get("relationship_uuid") or "",
            )
        )
        return {"facts": facts}

    # ------------------------------------------------------------------
    # WS-16 T14: human adjudication surfaces
    # ------------------------------------------------------------------

    async def _pending_reviews_payload(self, query: dict[str, list[str]]) -> dict:
        limit_raw = first(query, "limit")
        limit = int(limit_raw) if limit_raw else 50
        items = await self.client.pending_supersession_reviews(
            scope=self._request_scope(query),
            limit=limit,
        )
        return {"reviews": [item.model_dump(mode="json") for item in items]}

    def _principal_bound_agent_id(self, payload: dict[str, Any]) -> str:
        """WS-23 M4: bind a body ``agent_id`` to the AUTHENTICATED principal.

        ``min_endorsements`` is a quorum, and a quorum over self-asserted
        identity is not a quorum: on these endpoints the endorsing agent id came
        straight from the POST body, so one caller could promote a fact and then
        endorse it again as ``agent_id="other-agent"`` until the threshold was
        met, or set another agent's memory visibility in that agent's name.
        ``interop.py`` already refuses a request whose ``_agent_id`` disagrees
        with the principal; the hosted surface now applies the same rule.

        A deployment that wants a genuine multi-agent quorum must therefore
        authenticate each agent as its own principal — that is the point.
        """
        agent_id = self._required_string(payload, "agent_id")
        principal_agent_id = self.principal.agent_id
        if not principal_agent_id:
            raise HttpApiError(
                403,
                "this endpoint acts as one agent, but the authenticated principal "
                "carries no agent identity to bind the request to",
            )
        if agent_id != principal_agent_id:
            raise HttpApiError(
                403,
                f"agent_id {agent_id!r} does not match the authenticated principal agent {principal_agent_id!r}",
            )
        return agent_id

    def _request_body_scope(self, payload: dict[str, Any]) -> MemoryScope:
        """Resolve a POST body's scope through the principal, like GET scopes."""
        requested = parse_scope_key(self._required_string(payload, "scope"))
        return self._request_policy(requested).scope

    async def _resolve_review_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={"scope", "relationship_uuid", "decision", "reason", "resolved_by"},
        )
        decision = self._required_string(payload, "decision")
        if decision not in {"approve", "reject"}:
            raise HttpApiError(400, "decision must be 'approve' or 'reject'")
        result = await self.client.resolve_supersession_review(
            relationship_uuid=self._required_string(payload, "relationship_uuid"),
            scope=self._request_body_scope(payload),
            decision=decision,  # type: ignore[arg-type]
            reason=self._required_string(payload, "reason"),
            resolved_by=self._required_string(payload, "resolved_by"),
        )
        return result.model_dump(mode="json")

    async def _pin_memory_payload(self, *, pin: bool) -> dict[str, Any]:
        """WS-20 T23: operator pin/unpin over the principal-resolved scope."""
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={"scope", "relationship_uuid", "reason", "pinned_by"},
        )
        method = self.client.pin_memory if pin else self.client.unpin_memory
        result = await method(
            relationship_uuid=self._required_string(payload, "relationship_uuid"),
            scope=self._request_body_scope(payload),
            reason=self._required_string(payload, "reason"),
            pinned_by=self._required_string(payload, "pinned_by"),
        )
        return result.model_dump(mode="json")

    async def _memory_visibility_payload(self) -> dict[str, Any]:
        """WS-19 T22: operator visibility allowlist over the principal-resolved
        scope — usable on any principal-authorized scope, including project."""
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={"scope", "relationship_uuid", "agents", "reason", "set_by"},
        )
        agents = payload.get("agents")
        if agents is not None:
            agents = self._optional_string_list(payload, "agents")
        result = await self.client.set_memory_visibility(
            relationship_uuid=self._required_string(payload, "relationship_uuid"),
            scope=self._request_body_scope(payload),
            agents=agents,
            reason=self._required_string(payload, "reason"),
            set_by=self._required_string(payload, "set_by"),
        )
        return result.model_dump(mode="json")

    async def _pending_entity_aliases_payload(self, query: dict[str, list[str]]) -> dict:
        limit_raw = first(query, "limit")
        limit = int(limit_raw) if limit_raw else 50
        items = await self.client.pending_entity_alias_proposals(
            scope=self._request_scope(query),
            limit=limit,
        )
        return {"proposals": [item.model_dump(mode="json") for item in items]}

    async def _epochs_payload(self, query: dict[str, list[str]]) -> dict:
        """WS-26 T2/T5: every epoch ever created for a scope, plus its HEAD."""
        scope = self._request_scope(query)
        epochs = await self.client.redream_epochs(scope=scope)
        active_epoch_id = await self.client.redream_active_epoch(scope=scope)
        return {"epochs": epochs, "active_epoch_id": active_epoch_id}

    async def _epoch_diff_payload(self, query: dict[str, list[str]]) -> dict:
        """WS-26 T5: ``{added, removed, changed, unchanged}`` + registry
        deltas between a scope's current HEAD and a recomputed branch epoch.
        ``?scope=...&epoch_id=epoch-...``.
        """
        epoch_id = first(query, "epoch_id")
        if not epoch_id:
            raise HttpApiError(400, "epoch_id is required")
        diff = await self.client.redream_diff(scope=self._request_scope(query), epoch_id=epoch_id)
        return {"epoch_id": epoch_id, "diff": diff.model_dump(mode="json")}

    async def _resolve_entity_alias_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={"scope", "name", "decision", "reason", "resolved_by"},
        )
        decision = self._required_string(payload, "decision")
        if decision not in {"approve", "reject"}:
            raise HttpApiError(400, "decision must be 'approve' or 'reject'")
        result = await self.client.resolve_entity_alias_proposal(
            scope=self._request_body_scope(payload),
            name=self._required_string(payload, "name"),
            decision=decision,  # type: ignore[arg-type]
            reason=self._required_string(payload, "reason"),
            resolved_by=self._required_string(payload, "resolved_by"),
        )
        return result.model_dump(mode="json")

    async def _resolve_incident_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={
                "scope",
                "incident_id",
                "status",
                "statement",
                "resolved_by",
                "allow_held",
            },
        )
        status = self._required_string(payload, "status")
        if status not in {"acknowledged", "resolved"}:
            raise HttpApiError(400, "status must be 'acknowledged' or 'resolved'")
        result = await self.client.resolve_coherence_incident(
            incident_id=self._required_string(payload, "incident_id"),
            scope=self._request_body_scope(payload),
            status=status,  # type: ignore[arg-type]
            statement=self._required_string(payload, "statement"),
            resolved_by=self._required_string(payload, "resolved_by"),
            allow_held=self._optional_bool(payload, "allow_held", default=False),
        )
        return result.model_dump(mode="json")

    async def _resolve_disambiguation_payload(self) -> dict[str, Any]:
        payload = self._read_json_body()
        self._reject_unknown_fields(
            payload,
            allowed={
                "scope",
                "request_id",
                "runtime_trace",
                "artifact_version",
                "statement",
                "resolved_by",
            },
        )
        result = await self.client.resolve_disambiguation_request(
            request_id=self._required_string(payload, "request_id"),
            scope=self._request_body_scope(payload),
            runtime_trace=self._required_string(payload, "runtime_trace"),
            artifact_version=self._required_string(payload, "artifact_version"),
            statement=self._required_string(payload, "statement"),
            resolved_by=self._required_string(payload, "resolved_by"),
        )
        return result.model_dump(mode="json")

    def _send_static_asset(self, relative_path: str) -> None:
        static_dir = Path(getattr(self, "static_dir", DEFAULT_ADMIN_STATIC_DIR)).expanduser().resolve()
        if not static_dir.exists():
            raise HttpApiError(
                503,
                f"React admin build not found at {static_dir}. Run `npm --prefix ui/admin run build` first.",
            )
        requested = "index.html" if relative_path in {"", "/", "memory-graph"} else relative_path
        candidate = (static_dir / requested).resolve()
        try:
            candidate.relative_to(static_dir)
        except ValueError as exc:
            raise HttpApiError(404, "not found") from exc
        if not candidate.is_file():
            raise HttpApiError(404, "not found")
        encoded = candidate.read_bytes()
        self.send_response(200)
        content_type, _ = mimetypes.guess_type(str(candidate))
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_no_content(self) -> None:
        self.send_response(204)
        self.end_headers()

    def _send_internal_error(self) -> None:
        """Respond to an UNEXPECTED exception without telling the client what it was.

        #154. Both request handlers used to end with::

            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)

        which serialised any internal error -- psycopg messages, file paths, key names,
        whatever a driver puts in its exception -- straight into the response body. The
        ``HttpApiError`` branch above is deliberately kept: that type carries a message
        written to be read by a caller. This branch cannot make that claim about an
        arbitrary exception, so it says nothing and logs everything.

        Distinct from #126 and survives it: authentication controls *who* reaches this
        surface; this controls *what an error tells them*. An authenticated operator still
        should not receive a driver's internal string.

        The traceback goes to the server log at ERROR, so the detail is not lost -- it is
        moved to where an operator can see it and a caller cannot.
        """
        _log.exception("unhandled error serving %s %s", self.command, self.path)
        self._send_json({"error": "internal error"}, status=500)

    def _send_json(self, body: dict, *, status: int = 200) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def build_admin_client(graph_path_arg: str) -> tuple[Memotron, StorageBackend | None, Path | None]:
    """Decide which store this admin server reads, and open a client on it.

    THE OPERATIONAL STORE WINS over ``--graph-path``, mirroring ``mcp_server`` where
    ``MEMOTRON_OPERATIONAL_STORE_DSN`` beats ``MEMOTRON_GRAPH_PATH``.

    Extracted from ``main()`` so the decision is testable without binding a port. It was
    the untested half of the change that made this server able to read Postgres at all,
    and an untested store-selection is exactly how the deployed admin surface ended up
    serving a demo SQLite graph while the real data sat in Postgres.

    ``storage_settings_from_env()`` returning None IS the "no DSN configured" signal, not
    an error -- the same contract ``mcp_server`` reads.

    Returns the client, the backend it owns (None on the SQLite path, where the client
    opens its own), and the resolved path (None on the operational path).
    """
    settings = storage_settings_from_env()
    if settings is not None:
        store = create_storage_backend(settings, key_manager=key_manager_from_env())
        return Memotron(storage=store), store, None

    if not graph_path_arg:
        raise SystemExit(
            "no store configured: pass --graph-path, or set MEMOTRON_OPERATIONAL_STORE_DSN for the operational store"
        )
    graph_path = Path(graph_path_arg).expanduser().resolve()
    if not graph_path.exists():
        raise SystemExit(f"graph path does not exist: {graph_path}")
    return Memotron(graph_path=graph_path), None, graph_path


def build_hosted_platform(
    *,
    graph_path: Path | None,
    store: StorageBackend | None,
    tenant_id: str,
    agent_id: str,
) -> tuple[AgentMemoryPlatform | None, tuple[MemoryScope, ...]]:
    """The hosted `/api/platform/*` API, and the scopes it needs registered. C1 / #194.

    Every one of those 18 routes returned ``503 "Memotron platform API is not enabled
    for this server"`` because nothing in `main()` ever assigned `handler.platform` --
    `local_platform.py:216` was the only assignment in the tree, so the API the README
    documents worked in local dev and nowhere else. `_platform()` raises 503 when it is
    None, which is the entire prefix.

    **GATED ON IDENTITY, and that is the difference between enabling an API and opening
    one.** `--no-admin-writes` deliberately does NOT cover `/api/platform/` -- #192 found
    a blanket POST refusal took the documented agent-memory API down everywhere to close
    an exposure living in the other thirteen routes. And `requireGatewayIdentity` renders
    `true` on `latest` ONLY; the base chart says `false`, so stage, prod, preview and load
    inherit it. Multiply those and an unconditional creation serves 15 unauthenticated
    WRITE routes on prod -- the exact class of thing #50 just closed. Same condition and
    same shape as `_build_principal_resolver`: the API exists exactly where its callers
    can be attributed, and arming the flag turns both on together.

    **Returns the scopes as well as the platform, because two different gates need them**
    and the first version of this fix set only one:

    * `handler.configured_scopes` feeds `_scope_payload`, so the console can address them.
    * `build_demo_control_plane(..., extra_scopes=)` feeds the control plane, and
      `_request_policy` consults THAT. This is the one that produced the 400 measured on
      `memory/search` and `memory/remember` -- the two most-used routes -- as
      ``scope 'user:...' is not registered for tenant '...'`` (#197's spike). "No longer
      503" and "works" are different claims, and only the second one needs this.

    Exactly one of *graph_path* / *store* is ever set: `build_admin_client` returns
    ``(client, store, None)`` on the operational path and ``(client, None, path)`` on
    SQLite. `AgentMemoryPlatform.create` accepts either.

    `principal_provider` is deliberately not passed. It exists for transports whose guard
    can resolve a caller unaided, and this platform is a CLASS attribute that cannot see
    the in-flight request -- a provider bound at startup would answer with a stale
    principal, which is worse than none. `_platform()` passes the REQUEST's caller into
    the guard explicitly.

    A function rather than inline in `main()` so it is reachable by a test. `main()` binds
    a socket, so nothing executes it, and every line inside it is invisible to `diff-cov`
    and provable only by reading the source -- which is how this logic first shipped with
    only one of its two gates set.
    """
    if not identity_required():
        return None, ()

    platform = AgentMemoryPlatform.create(
        graph_path=graph_path,
        storage=store,
        project_id=tenant_id,
        agent_ids=(agent_id,),
    )
    scopes = (
        platform.project_scope,
        *((platform.user_scope,) if platform.user_scope is not None else ()),
    )
    return platform, scopes


def _build_principal_resolver(client: Memotron) -> GatewayPrincipalResolver | None:
    """The resolver `/api/platform/*` authenticates with, or ``None`` to leave it open.

    ``None`` when identity is not required, so the console keeps working unchanged in
    every environment that has not armed the flag and every test that does not set it.

    Mirrors `examples/mcp_server.py::_serve` rather than inventing a second wiring, down to
    resolving the lookup at REQUEST time: `client.graph` is the storage backend (the name
    predates there being more than one), and binding the bound method here would pin this
    to whichever backend existed at startup and quietly survive a client rebuild.

    `resolve_gateway_identity` derives the ADMIN plane URL itself
    (`gateway_identity.py:179`), which is why no base URL is passed here. It matters: the
    data plane answers `/key/info` with ``403 "Management routes are disabled for this
    instance."`` -- including the ``?key=<self>`` form -- so a caller that supplied
    `LITELLM_API_BASE` instead would fail 100% of the time, as something that reads like
    an authorization refusal rather than a misconfiguration.
    """
    if not identity_required():
        return None
    return GatewayPrincipalResolver(
        resolver=resolve_gateway_identity,
        principal_lookup=lambda alias: client.graph.principal_for_key_alias(alias),
        cache=IdentityCache(),
    )


def _advertised_url(*, public_url: str, host: str, port: int) -> str:
    """The URL the integration contract should advertise -- where callers REACH us.

    #242: this used to be derived from the BIND address, so `--host 0.0.0.0` in a
    container rendered as `http://127.0.0.1:8765/` and was published as
    `platform_api_url` on every hosted deployment. A client that fetched the
    machine-readable contract and followed it dialled localhost. Every *other* field in
    that contract was correct, which is what made it dangerous -- the document invited
    being followed.

    `public_url` wins whenever it is set, and it is the only thing that can be right
    behind an ingress: the process cannot discover its own external hostname. The
    bind-derived fallback is kept for local development, where bind and public genuinely
    are the same address.

    Extracted from `main()` rather than left inline so it can be tested without
    replicating `main()`'s wiring -- replicating the wiring is how the original missing
    `handler.platform` assignment survived a whole test file (see
    tests/test_admin_platform_enabled.py).
    """
    explicit = public_url.strip()
    if explicit:
        return explicit if explicit.endswith("/") else f"{explicit}/"
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{display_host}:{port}/"


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Memotron local memory graph admin UI.")
    parser.add_argument(
        "--graph-path",
        default="",
        help=(
            "Existing SQLite graph path to inspect. Optional: ignored when "
            "MEMOTRON_OPERATIONAL_STORE_DSN is set, which selects the operational store."
        ),
    )
    parser.add_argument("--scope", required=True, help="Default scope key, for example customer:acme.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--tenant-id",
        default="",
        help=(
            "Tenant id to operate on. Omit to derive it from the graph when that is "
            "unambiguous; a mismatch is reported at startup and in the UI."
        ),
    )
    parser.add_argument("--agent-id", default=DEMO_AGENT_ID)
    parser.add_argument(
        "--public-url",
        default="",
        help=(
            "The URL callers actually reach this server on, e.g. "
            "https://latest.jedai-memotron-admin.wdprapps.disney.com/. #242: without it "
            "the integration contract advertises the BIND address, which behind an ingress "
            "is always wrong -- it published http://127.0.0.1:8765/ on every hosted "
            "deployment, so a client that fetched the contract and followed it dialled "
            "localhost. Omit for local development, where bind and public are the same."
        ),
    )
    parser.add_argument(
        "--mcp-url",
        default="",
        help=(
            "Agent-memory MCP endpoint this console should advertise. Omit when no "
            "MCP server is running; the UI then reports 'not configured' instead of "
            "a default that may be false."
        ),
    )
    parser.add_argument(
        "--static-dir", default=str(DEFAULT_ADMIN_STATIC_DIR), help="Built React admin asset directory."
    )
    parser.add_argument("--principal-id", default="demo-user")
    parser.add_argument(
        "--principal-role", choices=[role.value for role in PrincipalRole], default=PrincipalRole.USER.value
    )
    parser.add_argument(
        "--no-admin-writes",
        action="store_true",
        help=(
            "Refuse the tenant-administration POST routes with 405 before dispatch. This "
            "surface has no authentication, so until it does, this is what keeps an "
            "unauthenticated caller from purging a tenant's graph or repointing its "
            "extraction traffic. The agent-memory API under /api/platform/ keeps working."
        ),
    )
    args = parser.parse_args()

    # #129. Before anything else in main(): a process with no log handler is worse
    # than one with no traces, and setup_telemetry must precede any ASGI wrapping.
    configure_observability(service_name="memotron-admin-server")

    # The operational store WINS over --graph-path when a DSN is configured, mirroring
    # `mcp_server` (where MEMOTRON_OPERATIONAL_STORE_DSN wins over
    # MEMOTRON_GRAPH_PATH). Until this existed the admin server could only open a
    # SQLite FILE, so in every deployed environment it served a demo graph seeded into
    # the container -- `/api/overview` reported `graph_path=/tmp/memory_graph_demo.sqlite`
    # and tenant `wdpr-demo`, and `/api/dream-runs` returned runs months old, while the
    # real store sat in Postgres. An operator surface showing plausible fabricated data is
    # worse than one showing nothing, because nothing prompts a second look.
    client, store, graph_path = build_admin_client(args.graph_path)

    # Annotated, not suppressed (#138). Three-arg `type()` is inferred as bare `type`, which
    # discards the base and made every subsequent attribute assignment an `attr-defined`
    # error -- 30 of them, which is why this module suppressed the code for its ENTIRE
    # surface. MemoryGraphHandler already declares each of these attributes with a type, so
    # naming the base here is all mypy needed, and `attr-defined` now protects the admin
    # surface the way it protects mcp_server after #132.
    handler: type[MemoryGraphHandler] = type("MemotronAdminHandler", (MemoryGraphHandler,), {})
    handler.client = client
    handler.store = store
    handler.runtime_client = handler.client
    handler.default_scope = parse_scope_key(args.scope)
    tenant_id, tenant_warnings = resolve_launch_tenant_id(
        client=handler.client,
        requested_tenant_id=args.tenant_id,
        default_scope=handler.default_scope,
    )
    handler.graph_path = graph_path
    handler.static_dir = Path(args.static_dir).expanduser().resolve()
    handler.tenant_id = tenant_id
    handler.agent_ids = (args.agent_id,)
    # #194 / C1 -- the hosted platform API, which returned 503 on all 18 of its routes
    # because NOTHING here ever assigned `handler.platform`. `local_platform.py:216` was
    # the only assignment in the tree, so the surface the README documents worked only in
    # local dev. `_platform()` raises 503 when it is None, which is why the whole prefix
    # answered that and nothing else.
    #
    # Mirrors `local_platform.py:154-169` rather than inventing a second wiring, and takes
    # `storage=store` when a DSN is configured: `build_admin_client` returns
    # `(client, store, None)` on the operational path and `(client, None, path)` on SQLite,
    # so exactly one of the two is ever set and `AgentMemoryPlatform.create` accepts either.
    #
    # `principal_provider` is deliberately NOT set. It exists for transports the platform's
    # own guard can reach on its own, and this handler's platform is a CLASS attribute that
    # cannot see the in-flight request; a provider bound at startup would answer with a
    # stale principal, which is worse than none. `_platform()` passes the REQUEST's caller
    # into the guard explicitly (#50) and is the one accessor all 18 routes share.
    platform, platform_scopes = build_hosted_platform(
        graph_path=graph_path,
        store=store,
        tenant_id=tenant_id,
        agent_id=args.agent_id,
    )
    handler.platform = platform
    handler.configured_scopes = platform_scopes or (handler.default_scope,)
    handler.tenant_warnings = tenant_warnings
    handler.no_admin_writes = args.no_admin_writes
    handler.ui_url = _advertised_url(public_url=args.public_url, host=args.host, port=args.port)
    handler.mcp_url = args.mcp_url.strip()
    registered_agent_ids = tuple(
        dict.fromkeys(
            (
                args.agent_id,
                *(str(item["agent_id"]) for item in handler.client.graph.tenant_agents(tenant_id)),
            )
        )
    )
    handler.control_plane = build_demo_control_plane(
        client=handler.client,
        default_scope=handler.default_scope,
        tenant_id=tenant_id,
        agent_id=args.agent_id,
        extra_scopes=(
            *(agent_scope(item) for item in registered_agent_ids),
            # Without these the platform API answers 400 on its two most-used routes: the
            # scopes it writes to are not registered for the tenant it belongs to.
            *platform_scopes,
        ),
    )
    handler.client.control_plane = handler.control_plane
    apply_persisted_tenant_policy(client=handler.client, tenant_id=tenant_id)
    handler.control_plane = handler.client.control_plane
    handler.principal = build_demo_principal(
        principal_id=args.principal_id,
        tenant_id=tenant_id,
        agent_id=args.agent_id,
        default_scope=handler.default_scope,
        role=PrincipalRole(args.principal_role),
    )
    handler.principal_resolver = _build_principal_resolver(client)

    server = HTTPServer((args.host, args.port), handler)
    print(f"serving memory graph at http://{args.host}:{args.port}/", flush=True)
    print(f"graph_path={graph_path}", flush=True)
    print(f"static_dir={handler.static_dir}", flush=True)
    # Adjacent to the path it is about, so the two are read together (T2-10).
    warn_if_admin_build_missing(handler.static_dir)
    print(f"default_scope={handler.default_scope.key}", flush=True)
    print(
        f"tenant_id={tenant_id}" + ("" if args.tenant_id.strip() else " (derived from graph)"),
        flush=True,
    )
    print(f"mcp_url={handler.mcp_url or 'not configured'}", flush=True)
    print(
        f"principal={handler.principal.principal_id} role={handler.principal.role.value}",
        flush=True,
    )
    for warning in (*tenant_warnings, *operator_tenant_detail(client, tenant_warnings)):
        print(f"WARNING: {warning}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
