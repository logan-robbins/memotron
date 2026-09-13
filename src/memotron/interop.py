"""WS-8: Interop & adoption surface for Memotron.

Three components — all additive and opt-in; zero changes to existing behaviour:

1. MemoryToolBackend  (``AnthropicMemoryToolBackend``)
   A path-addressed, versioned memory store that implements the Anthropic
   ``memory_20250818`` command set (view / create / str_replace / insert /
   delete / rename).  A Claude agent's memory tool calls ``handle(command)``
   and gets back a dict shaped per the Anthropic contract.

   Storage model
   ~~~~~~~~~~~~~
   Each memory-tool "file" is stored as one entry in an in-memory dict keyed by
   ``(scope_key, path)``.  Each write produces an immutable version record (list
   of ``MemoryFileVersion`` objects) so prior versions are always retrievable.
   Optimistic concurrency: a conditional write (``expected_hash`` present) fails
   fast with ``MemoryToolConflictError`` when the supplied hash does not match the
   current file's ``content_sha256``.  Read-only mounts: the backend is constructed
   with a ``mount_mode`` (``"read_write"`` or ``"read_only"``); writes to a
   read-only backend fail fast with ``MemoryToolReadOnlyError``.

   Path safety
   ~~~~~~~~~~~
   Every path is validated on entry: blank paths, `..` components, and paths not
   rooted under ``/memories/`` are rejected with ``MemoryToolPathError`` (fail-fast,
   no silent coercion).

2. MotiveAsInstructionsAdapter  (light Dreams-contract bridge)
   A thin helper that surfaces the conceptual mapping between Anthropic "Dreams"
   ``instructions`` and Memotron ``Motive``.  A dream run's steerable
   ``instructions`` field *is* a Motive: the adapter wraps an async callable
   (the dream job runner) so callers can pass a ``Motive`` as ``instructions``
   and receive back a reviewable ``DreamRunSummary``.  This documents the
   mapping in code without rebuilding dreaming.

3. MemoryRouter  (scope/Motive-aware OpenAI-compatible router)
   A policy-enforcement point that sits in front of an OpenAI-compatible
   upstream transport (injected, pluggable, default hermetic/echo).  Given an
   OpenAI-style chat request payload and a tenant ``MemoryScope``, the router:
     (a) selects the active Motive(s) from the attached MemoryBank,
     (b) enforces tenant scope (cross-tenant requests fail fast),
     (c) injects retrieved memory context (via ``Memotron.profile()``) into
         the request as a system message prefix,
     (d) calls the upstream transport and returns its response.

   Upstream transport is a ``UpstreamTransport`` Protocol with a single async
   method ``chat_completion(payload: dict) -> dict``.  The default
   ``EchoUpstreamTransport`` returns a deterministic echo response — no network.
   Tests use this default; production callers inject a real HTTP transport.

All classes use ``from __future__ import annotations``, pydantic v2 where
modeling data, ``StrEnum``, and Protocol-based pluggable transports — mirroring
the ``ExtractionTransport`` / ``EmbeddingTransport`` pattern in the codebase.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from memotron.config import EffectiveMemoryPolicy, MemoryBank, MemoryControlPlane, MemoryPrincipal, Motive
from memotron.models import MemoryScope

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_MEMORY_ROOT = "/memories"


def _validate_path(path: str) -> str:
    """Validate and normalise a memory-tool path.

    Rules (fail-fast):
    - Must be a non-blank string.
    - Must not contain ``..`` components (path-traversal guard).
    - Must start with ``/memories/`` (the memory-tool root).
    - Returns the canonical path (stripped, no trailing slash unless root).

    Raises ``MemoryToolPathError`` on any violation.
    """
    if not path or not path.strip():
        raise MemoryToolPathError("path cannot be blank")
    stripped = path.strip()
    # Reject any .. component anywhere in the path.
    parts = stripped.split("/")
    if any(part == ".." for part in parts):
        raise MemoryToolPathError(f"path traversal rejected: {stripped!r}")
    # Must be under /memories/ — allow exactly "/memories" or "/memories/..."
    if stripped != _MEMORY_ROOT and not stripped.startswith(_MEMORY_ROOT + "/"):
        raise MemoryToolPathError(f"path must start with {_MEMORY_ROOT!r}, got {stripped!r}")
    return stripped


def _path_is_directory(path: str) -> bool:
    """True when the path refers to a directory listing (the root or a prefix)."""
    return path == _MEMORY_ROOT or path.endswith("/")


def _content_sha256(content: str) -> str:
    """SHA-256 hex digest of UTF-8-encoded content."""
    return hashlib.sha256(content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MemoryToolError(Exception):
    """Base class for all memory-tool errors."""


class MemoryToolPathError(MemoryToolError):
    """Invalid or unsafe path."""


class MemoryToolNotFoundError(MemoryToolError):
    """File not found."""


class MemoryToolConflictError(MemoryToolError):
    """Optimistic-concurrency conflict: stale content_sha256."""


class MemoryToolReadOnlyError(MemoryToolError):
    """Write attempted on a read-only mount."""


class MemoryToolCommandError(MemoryToolError):
    """Unknown or malformed command."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class MemoryFileVersion(BaseModel):
    """Immutable snapshot of a memory-tool file at one point in time.

    Versions are append-only: each write creates a new record.  Prior versions
    remain retrievable forever — they are never mutated or deleted.
    """

    version: int
    content: str
    content_sha256: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str = "memory-tool"


class MemoryFile(BaseModel):
    """Current state of a path-addressed memory-tool file.

    ``versions`` is the full immutable history (oldest → newest).
    ``current`` is the live version (last element of versions).
    ``path`` is the canonical path under ``/memories/``.
    ``scope_key`` is the owning scope — cross-scope access is rejected at the
    backend level.
    """

    path: str
    scope_key: str
    versions: list[MemoryFileVersion] = Field(default_factory=list)

    @property
    def current(self) -> MemoryFileVersion | None:
        return self.versions[-1] if self.versions else None

    @property
    def content(self) -> str:
        return self.current.content if self.current else ""

    @property
    def content_sha256(self) -> str:
        return self.current.content_sha256 if self.current else _content_sha256("")


# ---------------------------------------------------------------------------
# MemoryToolBackend  (AnthropicMemoryToolBackend)
# ---------------------------------------------------------------------------


class AnthropicMemoryToolBackend:
    """Implements the Anthropic ``memory_20250818`` command set.

    This is the TOOL-HANDLER side: the agent runtime calls ``handle(command)``
    and gets back a dict shaped per the Anthropic contract.  No network calls
    are made — storage is purely in-process (suitable for tests and POC).

    Parameters
    ----------
    scope:
        The owning ``MemoryScope``.  All files are scoped under ``scope.key``;
        cross-scope paths are rejected.
    mount_mode:
        ``"read_write"`` (default) — full access.
        ``"read_only"`` — all write commands fail fast with
        ``MemoryToolReadOnlyError``.  Integrates with WS-7 ``read_only_scopes``
        at a higher level: callers should create a ``read_only`` backend for any
        scope in ``config.read_only_scopes``.

    Command surface (``memory_20250818``)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    ``view``        — list directory or read file content + sha256.
    ``create``      — write a new file (fails if it already exists unless
                      ``overwrite=True``); conditional write via ``expected_hash``.
    ``str_replace``  — replace first occurrence of ``old_str`` with ``new_str``
                      in the file; fails fast if ``old_str`` not found or is
                      ambiguous (appears more than once).
    ``insert``      — insert ``new_str`` after line ``insert_line`` (0-based).
    ``delete``      — remove a file; fails if not found.
    ``rename``      — atomically rename a file; fails if source not found or
                      destination already exists.

    Optimistic concurrency
    ~~~~~~~~~~~~~~~~~~~~~~
    Any write command may carry ``expected_hash`` (``content_sha256`` of the
    version the caller last read).  When supplied and stale, the command fails
    fast with ``MemoryToolConflictError``; the current hash and version are
    returned so the caller can retry.  A fresh ``expected_hash`` always succeeds.

    Version history
    ~~~~~~~~~~~~~~~
    Every write appends an immutable ``MemoryFileVersion`` record.  Prior
    versions are retrievable via ``get_versions(path)``.

    Path isolation
    ~~~~~~~~~~~~~~
    Files are addressed as ``/memories/<anything>``.  Each ``(scope_key, path)``
    pair is independent — two scopes can have the same path without conflict.
    """

    def __init__(
        self,
        scope: MemoryScope,
        *,
        mount_mode: str = "read_write",
    ) -> None:
        if mount_mode not in {"read_write", "read_only"}:
            raise MemoryToolError(f"mount_mode must be 'read_write' or 'read_only', got {mount_mode!r}")
        self._scope = scope
        self._mount_mode = mount_mode
        # Storage: (scope_key, path) → MemoryFile
        self._store: dict[tuple[str, str], MemoryFile] = {}

    @property
    def scope(self) -> MemoryScope:
        return self._scope

    @property
    def mount_mode(self) -> str:
        return self._mount_mode

    @property
    def is_read_only(self) -> bool:
        return self._mount_mode == "read_only"

    # ------------------------------------------------------------------
    # Public dispatch
    # ------------------------------------------------------------------

    def handle(self, command: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a ``memory_20250818`` command dict.

        Parameters
        ----------
        command:
            A dict with at least a ``"command"`` key (string) and the
            command-specific fields documented in the Anthropic contract.

        Returns
        -------
        A dict with at minimum ``"ok": True/False`` and command-specific
        response fields.  On error, ``"ok"`` is ``False`` and ``"error"``
        contains a human-readable description.

        Raises
        ------
        All ``MemoryToolError`` subclasses bubble up rather than being silently
        swallowed so the caller can choose error-handling strategy.
        """
        if not isinstance(command, dict):
            raise MemoryToolCommandError(f"command must be a dict, got {type(command).__name__}")
        cmd_name = command.get("command")
        if not cmd_name or not isinstance(cmd_name, str):
            raise MemoryToolCommandError("command dict must have a non-blank 'command' key")
        dispatch = {
            "view": self._handle_view,
            "create": self._handle_create,
            "str_replace": self._handle_str_replace,
            "insert": self._handle_insert,
            "delete": self._handle_delete,
            "rename": self._handle_rename,
        }
        handler = dispatch.get(cmd_name)
        if handler is None:
            known = sorted(dispatch)
            raise MemoryToolCommandError(f"unknown command {cmd_name!r}. Known commands: {known}")
        return handler(command)

    # ------------------------------------------------------------------
    # Version history access
    # ------------------------------------------------------------------

    def get_versions(self, path: str) -> list[MemoryFileVersion]:
        """Return the full immutable version history for a path.

        Returns an empty list when the path has never existed.
        The list is oldest-first (index 0 = first version ever written).
        """
        validated = _validate_path(path)
        key = (self._scope.key, validated)
        file = self._store.get(key)
        if file is None:
            return []
        # Return a copy so callers cannot mutate internal history.
        return list(file.versions)

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _handle_view(self, command: dict[str, Any]) -> dict[str, Any]:
        """view: list directory or read file.

        ``{"command": "view", "path": "/memories/"}``
        → ``{"ok": True, "type": "directory", "entries": ["/memories/foo.md", ...]}``

        ``{"command": "view", "path": "/memories/foo.md"}``
        → ``{"ok": True, "type": "file", "path": "...", "content": "...",
             "content_sha256": "...", "version": N}``
        """
        path = self._require_path(command)
        validated = _validate_path(path)

        if _path_is_directory(validated):
            prefix = validated.rstrip("/") + "/"
            entries = [
                p
                for (sk, p) in self._store
                if sk == self._scope.key and (p.startswith(prefix) or validated == _MEMORY_ROOT)
            ]
            # Filter to direct children only if listing a non-root dir.
            if validated != _MEMORY_ROOT:
                # Strip the prefix; keep only paths with no further "/" after prefix.
                entries = [p for p in entries if "/" not in p[len(prefix) :]]
            else:
                entries = sorted(set(entries))
            return {
                "ok": True,
                "type": "directory",
                "path": validated,
                "entries": sorted(entries),
            }

        # File read.
        file = self._get_file(validated)
        current = file.current
        if current is None:
            raise MemoryToolNotFoundError(f"file not found: {validated!r}")
        return {
            "ok": True,
            "type": "file",
            "path": validated,
            "content": current.content,
            "content_sha256": current.content_sha256,
            "version": current.version,
        }

    def _handle_create(self, command: dict[str, Any]) -> dict[str, Any]:
        """create: write a new file or overwrite.

        Required fields: ``path``, ``content``.
        Optional:
          ``overwrite`` (bool, default False) — allow overwriting existing files.
          ``expected_hash`` (str) — conditional write; fails if stale.

        Returns: ``{"ok": True, "path": ..., "content_sha256": ..., "version": N}``
        """
        self._assert_writable()
        path = self._require_path(command)
        validated = _validate_path(path)
        content = command.get("content")
        if content is None or not isinstance(content, str):
            raise MemoryToolCommandError("create command requires a 'content' string field")

        overwrite = bool(command.get("overwrite", False))
        expected_hash = command.get("expected_hash")

        key = (self._scope.key, validated)
        existing = self._store.get(key)

        if existing is not None and not overwrite and expected_hash is None:
            raise MemoryToolCommandError(
                f"file already exists at {validated!r}. Pass overwrite=True or expected_hash to overwrite."
            )

        # Optimistic concurrency check.
        if expected_hash is not None:
            current_hash = existing.content_sha256 if existing else _content_sha256("")
            if expected_hash != current_hash:
                raise MemoryToolConflictError(
                    f"optimistic concurrency conflict at {validated!r}: "
                    f"supplied hash {expected_hash!r} does not match current hash {current_hash!r}"
                )

        return self._write_file(validated, content)

    def _handle_str_replace(self, command: dict[str, Any]) -> dict[str, Any]:
        """str_replace: replace first (and only) occurrence of old_str with new_str.

        Required fields: ``path``, ``old_str``, ``new_str``.
        Optional: ``expected_hash``.

        Fails fast when:
        - ``old_str`` is not found in the file.
        - ``old_str`` appears more than once (ambiguous replacement).
        """
        self._assert_writable()
        path = self._require_path(command)
        validated = _validate_path(path)
        old_str = command.get("old_str")
        new_str = command.get("new_str")
        if old_str is None or not isinstance(old_str, str):
            raise MemoryToolCommandError("str_replace command requires an 'old_str' string field")
        if new_str is None or not isinstance(new_str, str):
            raise MemoryToolCommandError("str_replace command requires a 'new_str' string field")

        file = self._get_file(validated)
        content = file.content
        expected_hash = command.get("expected_hash")
        if expected_hash is not None and expected_hash != file.content_sha256:
            raise MemoryToolConflictError(
                f"optimistic concurrency conflict at {validated!r}: "
                f"supplied hash {expected_hash!r} does not match current hash {file.content_sha256!r}"
            )

        count = content.count(old_str)
        if count == 0:
            raise MemoryToolCommandError(f"str_replace: old_str not found in {validated!r}: {old_str!r}")
        if count > 1:
            raise MemoryToolCommandError(
                f"str_replace: old_str appears {count} times in {validated!r} — "
                "provide more context to make the replacement unambiguous"
            )

        new_content = content.replace(old_str, new_str, 1)
        return self._write_file(validated, new_content)

    def _handle_insert(self, command: dict[str, Any]) -> dict[str, Any]:
        """insert: insert new_str after line insert_line (0-based line index).

        Required fields: ``path``, ``insert_line`` (int ≥ 0), ``new_str``.
        Optional: ``expected_hash``.

        Line 0 = insert before the first line (prepend).
        Line N ≥ len(lines) = append after the last line.
        """
        self._assert_writable()
        path = self._require_path(command)
        validated = _validate_path(path)
        insert_line = command.get("insert_line")
        new_str = command.get("new_str")
        if insert_line is None or not isinstance(insert_line, int) or insert_line < 0:
            raise MemoryToolCommandError("insert command requires a non-negative integer 'insert_line' field")
        if new_str is None or not isinstance(new_str, str):
            raise MemoryToolCommandError("insert command requires a 'new_str' string field")

        file = self._get_file(validated)
        content = file.content
        expected_hash = command.get("expected_hash")
        if expected_hash is not None and expected_hash != file.content_sha256:
            raise MemoryToolConflictError(
                f"optimistic concurrency conflict at {validated!r}: "
                f"supplied hash {expected_hash!r} does not match current hash {file.content_sha256!r}"
            )

        lines = content.split("\n")
        # Insert after insert_line; clamp to valid range.
        insert_at = min(insert_line + 1, len(lines))
        lines.insert(insert_at, new_str)
        new_content = "\n".join(lines)
        return self._write_file(validated, new_content)

    def _handle_delete(self, command: dict[str, Any]) -> dict[str, Any]:
        """delete: remove a file from the store.

        Required fields: ``path``.
        Fails fast when the file does not exist.

        Note: version history is also removed on delete (the file is gone).
        In a production system with immutable audit logs, history would be
        retained separately — this POC mirrors the simplest contract.
        """
        self._assert_writable()
        path = self._require_path(command)
        validated = _validate_path(path)
        key = (self._scope.key, validated)
        if key not in self._store:
            raise MemoryToolNotFoundError(f"file not found: {validated!r}")
        del self._store[key]
        return {"ok": True, "deleted": validated}

    def _handle_rename(self, command: dict[str, Any]) -> dict[str, Any]:
        """rename: atomically rename a file.

        Required fields: ``path`` (source), ``new_path`` (destination).
        Fails fast when:
        - source does not exist.
        - destination already exists.
        """
        self._assert_writable()
        source_path = self._require_path(command)
        new_path = command.get("new_path")
        if not new_path or not isinstance(new_path, str):
            raise MemoryToolCommandError("rename command requires a 'new_path' string field")

        validated_source = _validate_path(source_path)
        validated_dest = _validate_path(new_path)

        source_key = (self._scope.key, validated_source)
        dest_key = (self._scope.key, validated_dest)

        if source_key not in self._store:
            raise MemoryToolNotFoundError(f"rename source not found: {validated_source!r}")
        if dest_key in self._store:
            raise MemoryToolCommandError(f"rename destination already exists: {validated_dest!r}")

        file = self._store.pop(source_key)
        renamed_file = MemoryFile(
            path=validated_dest,
            scope_key=self._scope.key,
            versions=file.versions,
        )
        self._store[dest_key] = renamed_file
        return {
            "ok": True,
            "renamed_from": validated_source,
            "renamed_to": validated_dest,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _assert_writable(self) -> None:
        if self.is_read_only:
            raise MemoryToolReadOnlyError(f"scope {self._scope.key!r} is mounted read-only; writes are not permitted")

    def _require_path(self, command: dict[str, Any]) -> str:
        path = command.get("path")
        if not path or not isinstance(path, str):
            raise MemoryToolCommandError(f"command {command.get('command')!r} requires a 'path' string field")
        return path

    def _get_file(self, validated_path: str) -> MemoryFile:
        key = (self._scope.key, validated_path)
        file = self._store.get(key)
        if file is None:
            raise MemoryToolNotFoundError(f"file not found: {validated_path!r}")
        return file

    def _write_file(self, validated_path: str, content: str) -> dict[str, Any]:
        """Write a new version of the file and return the response dict."""
        key = (self._scope.key, validated_path)
        existing = self._store.get(key)
        next_version = (existing.current.version + 1) if (existing and existing.current) else 1

        new_hash = _content_sha256(content)
        version_record = MemoryFileVersion(
            version=next_version,
            content=content,
            content_sha256=new_hash,
        )
        if existing is None:
            file = MemoryFile(
                path=validated_path,
                scope_key=self._scope.key,
                versions=[version_record],
            )
        else:
            file = MemoryFile(
                path=validated_path,
                scope_key=existing.scope_key,
                versions=[*existing.versions, version_record],
            )
        self._store[key] = file
        return {
            "ok": True,
            "path": validated_path,
            "content_sha256": new_hash,
            "version": next_version,
        }


# Alias matching the NEXT.md name.
MemoryToolBackend = AnthropicMemoryToolBackend


# ---------------------------------------------------------------------------
# MotiveAsInstructionsAdapter  (Dreams-contract bridge)
# ---------------------------------------------------------------------------


class DreamRunSummary(BaseModel):
    """Reviewable summary returned by a MotiveAsInstructionsAdapter dream run.

    Conceptual mapping to Anthropic "Dreams" contract:
    - ``motive``           = Anthropic ``instructions`` (steerable goal)
    - ``input_scope_key``  = Anthropic immutable input store key
    - ``episodes_seen``    = transcripts fed to the dream run
    - ``relationships_created``, ``reinforced``, ``superseded`` = new reviewable store deltas
    - ``reviewable``       = True until the caller adopts or discards the run

    This is a POC-level summary: a production system would point to a new
    immutable output store (the Anthropic "Dreams" shape); here the graph
    has already been updated by the time this object is returned.
    """

    motive_name: str
    motive_goal: str
    input_scope_key: str
    ran_at: datetime
    episodes_seen: int = 0
    relationships_created: int = 0
    reinforced: int = 0
    superseded: int = 0
    reviewable: bool = True
    """Always True after a run — the caller reviews the MemoryEvolutionProof to
    decide whether to keep or rollback (via forget_memory / correct_memory)."""
    notes: str = ""


class MotiveAsInstructionsAdapter:
    """Thin bridge between Anthropic "Dreams" ``instructions`` and Memotron ``Motive``.

    The Anthropic "Dreams" API (beta ``dreaming-2026-04-21``) accepts an
    ``instructions`` text field that steers offline consolidation.  Memotron's
    equivalent is a ``Motive`` — a named, auditable, type-filtered formation
    policy.  This adapter makes the equivalence explicit in code:

        Anthropic ``instructions`` = Memotron ``Motive``

    Usage (conceptual; does not make network calls)::

        adapter = MotiveAsInstructionsAdapter(
            motive=bank.motive("learn-compliance-requirements"),
            scope=customer_scope,
            dream_runner=client.run_dream_job,
        )
        summary = await adapter.run_dream(job_name="formation-default")

    The ``dream_runner`` is an async callable with the same signature as
    ``Memotron.run_dream_job`` — injected for testability.
    """

    def __init__(
        self,
        *,
        motive: Motive,
        scope: MemoryScope,
        dream_runner: Any,  # async (job_name: str) -> DreamRunResult
    ) -> None:
        self._motive = motive
        self._scope = scope
        self._dream_runner = dream_runner

    @property
    def motive(self) -> Motive:
        return self._motive

    async def run_dream(self, *, job_name: str) -> DreamRunSummary:
        """Run a dream job steered by this adapter's Motive.

        The Motive IS the ``instructions`` in Anthropic terminology:
        - ``motive.goal``                  → the formation objective
        - ``motive.allowed_memory_types``  → type filter (what the run may create)
        - ``motive.prompt_override``       → extraction guidance overlay
        - ``motive.salience_rubric``       → quality gate

        The dream runner is called with the job name; the Motive selection is
        honoured because the job must reference this motive in its configuration
        (or episode metadata stamps it).  This adapter documents the conceptual
        mapping and provides a reviewable summary shaped like the Anthropic
        "Dreams" output contract.

        Returns a ``DreamRunSummary`` for the caller to review before treating
        the run as adopted (analogous to Anthropic's "new reviewable store").
        """
        ran_at = datetime.now(UTC)
        result = await self._dream_runner(job_name=job_name)

        # Aggregate across job runs.
        episodes_seen = sum(r.processed_episodes for r in result.job_runs)
        created = sum(r.created_relationships for r in result.job_runs)
        reinforced = sum(r.reinforced_relationships for r in result.job_runs)
        superseded = sum(r.superseded_relationships for r in result.job_runs)

        return DreamRunSummary(
            motive_name=self._motive.name,
            motive_goal=self._motive.goal,
            input_scope_key=self._scope.key,
            ran_at=ran_at,
            episodes_seen=episodes_seen,
            relationships_created=created,
            reinforced=reinforced,
            superseded=superseded,
            reviewable=True,
            notes=(
                f"Motive '{self._motive.name}' steered formation "
                f"(goal: {self._motive.goal}). "
                "Review via Memotron.memory_evolution() before adopting."
            ),
        )


# ---------------------------------------------------------------------------
# MemoryRouter — scope/Motive-aware OpenAI-compatible router
# ---------------------------------------------------------------------------


@runtime_checkable
class UpstreamTransport(Protocol):
    """Pluggable upstream transport for the MemoryRouter.

    Implementors must provide a single async method.  The default
    ``EchoUpstreamTransport`` returns a deterministic echo — no network.
    Production callers inject an HTTP transport (e.g. httpx-based).
    """

    async def chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a chat-completion payload upstream and return the response."""
        ...


class EchoUpstreamTransport:
    """Default hermetic transport for tests.

    Returns a deterministic echo response containing the injected system message
    so tests can assert that memory context was injected.  Makes zero network calls.
    """

    async def chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Extract injected system message for echo.
        messages = payload.get("messages", [])
        system_content = next(
            (m.get("content", "") for m in messages if m.get("role") == "system"),
            "",
        )
        return {
            "id": "echo-response",
            "object": "chat.completion",
            "model": payload.get("model", "echo"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"[ECHO] system={system_content!r}",
                    },
                    "finish_reason": "stop",
                }
            ],
            "_memotron_echo": True,
        }


class MemoryRouter:
    """Scope/Motive-aware OpenAI-compatible memory router.

    This is the supermemory-style transparent proxy reimagined as a
    **policy-enforcement point**: it enforces tenant scope isolation and
    injects retrieved memory context before forwarding to an upstream
    OpenAI-compatible transport.

    Policy enforcement
    ~~~~~~~~~~~~~~~~~~
    1. **Tenant scope check**: the request must carry ``_scope`` (a
       ``MemoryScope``) matching the router's configured scope.  Cross-tenant
       requests (mismatched scope key) fail fast with ``ValueError``.
    2. **Motive selection**: if the router has a ``MemoryBank``, it resolves
       the active Motive(s) from the bank.  The first (primary) Motive is used
       for retrieval budget and goal annotation.  Passing ``motive_name`` in the
       request overrides the bank default.
    3. **Memory injection**: the router calls ``memory_context_provider(scope)``
       (async) to retrieve the rendered memory context and prepends it as a
       system message in the outbound request.
    4. **Pass-through**: the enriched payload is forwarded to ``upstream``
       (injected transport, default ``EchoUpstreamTransport``).

    Parameters
    ----------
    scope:
        Authoritative tenant scope for a fixed-scope router instance.  Omit
        only when ``control_plane`` is supplied and every routed request carries
        enough tenant metadata for policy resolution.
    memory_context_provider:
        Async callable ``(scope: MemoryScope) -> str`` that returns the
        rendered memory context for the scope.  Typically bound to
        ``Memotron.profile(scope=scope).rendered_context``.
    upstream:
        Pluggable upstream transport.  Default: ``EchoUpstreamTransport``
        (hermetic, no network calls — for tests and offline demos).
    memory_bank:
        Optional ``MemoryBank``.  When set, Motives are resolved and their
        goals are annotated in the injected system message.
    default_motive_name:
        When set and a bank is attached, this Motive is selected for every
        request unless the request overrides it via ``"_motive_name"``.
    control_plane:
        Optional multi-tenant resolver.  When request metadata includes
        ``"_tenant_id"`` (or the router has no fixed scope), the router resolves
        an ``EffectiveMemoryPolicy`` and uses that policy's scope, memory bank,
        Motive, dream mode, prompt pack, and audit metadata.
    """

    def __init__(
        self,
        *,
        scope: MemoryScope | None = None,
        memory_context_provider: Any,  # async (scope: MemoryScope) -> str
        upstream: UpstreamTransport | None = None,
        memory_bank: MemoryBank | None = None,
        default_motive_name: str | None = None,
        control_plane: MemoryControlPlane | None = None,
    ) -> None:
        if scope is None and control_plane is None:
            raise ValueError("MemoryRouter requires either scope or control_plane")
        self._scope = scope
        self._memory_context_provider = memory_context_provider
        self._upstream: UpstreamTransport = upstream or EchoUpstreamTransport()
        self._memory_bank = memory_bank
        self._default_motive_name = default_motive_name
        self._control_plane = control_plane

    @property
    def scope(self) -> MemoryScope:
        if self._scope is None:
            raise ValueError("router has no fixed scope; resolve a request policy instead")
        return self._scope

    async def route(self, request: dict[str, Any], *, principal: MemoryPrincipal | None = None) -> dict[str, Any]:
        """Route an OpenAI-style chat request through the policy enforcement point.

        Parameters
        ----------
        request:
            A dict shaped like an OpenAI ``/v1/chat/completions`` body plus
            optional Memotron-specific fields:
            - ``"_tenant_id"`` (str): tenant policy key when using a control plane.
            - ``"_agent_id"`` (str): agent policy key when using a control plane.
            - ``"_scope"`` (``MemoryScope``): the caller's tenant scope.
              Must match ``self.scope`` — cross-tenant requests fail fast.
            - ``"_motive_name"`` (str): override the default Motive selection.
            - ``"_dream_mode"`` (str): request-level dream mode override.
            - ``"_prompt_pack"`` (str): request-level prompt pack override.
        principal:
            Authenticated server-side principal.  When supplied, it is the
            authority for tenant/agent/scope authorization; request metadata may
            only select an authorized scope and optional mode/prompt/motive.

        Returns
        -------
        The upstream response dict, unchanged except for the added system
        message in the outbound payload.  The response also carries
        ``"_memotron_injected"`` metadata for introspection.

        Raises
        ------
        ``ValueError`` on cross-tenant scope mismatch (fail-fast).
        """
        policy = self._resolve_request_policy(request, principal=principal)
        request_scope: MemoryScope | None = request.get("_scope")

        if policy is not None:
            effective_scope = policy.scope
            if self._scope is not None and effective_scope.key != self._scope.key:
                raise ValueError(
                    f"cross-tenant request rejected: resolved scope {effective_scope.key!r} "
                    f"does not match router scope {self._scope.key!r}"
                )
            active_motive = policy.motive
            motive_name = policy.motive_name
        else:
            if self._scope is None:
                raise ValueError("tenant_id is required when routing without a fixed scope")
            effective_scope = self._scope
            if request_scope is not None and request_scope.key != effective_scope.key:
                raise ValueError(
                    f"cross-tenant request rejected: request scope {request_scope.key!r} "
                    f"does not match router scope {effective_scope.key!r}"
                )
            active_motive = None
            motive_name = request.get("_motive_name") or self._default_motive_name
            if motive_name and self._memory_bank is not None:
                active_motive = self._memory_bank.motive(motive_name)

        # (c) Memory context injection.
        memory_context: str = await self._memory_context_provider(effective_scope)

        # Build the enriched system message.
        system_parts: list[str] = []
        if memory_context.strip():
            system_parts.append(memory_context)
        if active_motive is not None:
            system_parts.append(f"[Memory formation goal: {active_motive.goal}]")

        # Prepend system message to the request messages list.
        messages: list[dict[str, Any]] = list(request.get("messages", []))
        if system_parts:
            injected_system = "\n".join(system_parts)
            # Prepend before existing system messages (or add new one).
            existing_system_idx = next(
                (i for i, m in enumerate(messages) if m.get("role") == "system"),
                None,
            )
            if existing_system_idx is not None:
                existing = messages[existing_system_idx]["content"]
                messages[existing_system_idx] = {
                    "role": "system",
                    "content": injected_system + "\n\n" + existing,
                }
            else:
                messages.insert(0, {"role": "system", "content": injected_system})

        outbound = {**request, "messages": messages}
        # Strip Memotron-private fields from upstream payload.
        outbound.pop("_tenant_id", None)
        outbound.pop("_agent_id", None)
        outbound.pop("_scope", None)
        outbound.pop("_motive_name", None)
        outbound.pop("_dream_mode", None)
        outbound.pop("_prompt_pack", None)

        # (d) Pass-through to upstream transport.
        response = await self._upstream.chat_completion(outbound)

        # Annotate response with injection metadata.
        response = dict(response)
        response["_memotron_injected"] = {
            "scope_key": effective_scope.key,
            "motive_name": active_motive.name if active_motive else motive_name,
            "memory_context_chars": len(memory_context),
        }
        if policy is not None:
            response["_memotron_injected"].update(
                {
                    "principal_id": principal.principal_id if principal is not None else None,
                    "principal_role": principal.role.value if principal is not None else None,
                    "tenant_id": policy.tenant_id,
                    "agent_id": policy.agent_id,
                    "dream_mode": policy.dream_mode.name,
                    "prompt_pack": policy.prompt_pack.name if policy.prompt_pack is not None else None,
                    "prompt_profile": policy.prompt_profile,
                    "prompt_profile_version": policy.prompt_profile_version,
                    "read_only": policy.read_only,
                    "source_trace": policy.source_trace,
                }
            )
        return response

    def _resolve_request_policy(
        self,
        request: dict[str, Any],
        *,
        principal: MemoryPrincipal | None = None,
    ) -> EffectiveMemoryPolicy | None:
        if self._control_plane is None:
            return None
        if principal is not None:
            tenant_id = request.get("_tenant_id")
            if tenant_id is not None and tenant_id != principal.tenant_id:
                raise ValueError(
                    f"request tenant_id {tenant_id!r} does not match authenticated principal tenant "
                    f"{principal.tenant_id!r}"
                )
            agent_id = request.get("_agent_id")
            if agent_id is not None and principal.agent_id is not None and agent_id != principal.agent_id:
                raise ValueError(
                    f"request agent_id {agent_id!r} does not match authenticated principal agent {principal.agent_id!r}"
                )
            scope = request.get("_scope") or principal.default_scope
            if scope is not None and not isinstance(scope, MemoryScope):
                raise ValueError("_scope must be a MemoryScope")
            dream_mode = request.get("_dream_mode")
            if dream_mode is not None and not isinstance(dream_mode, str):
                raise ValueError("_dream_mode must be a string")
            motive = request.get("_motive_name")
            if motive is not None and not isinstance(motive, str):
                raise ValueError("_motive_name must be a string")
            prompt_pack = request.get("_prompt_pack")
            if prompt_pack is not None and not isinstance(prompt_pack, str):
                raise ValueError("_prompt_pack must be a string")
            return self._control_plane.resolve_for_principal(
                principal=principal,
                scope=scope,
                dream_mode=dream_mode,
                motive=motive,
                prompt_pack=prompt_pack,
            )
        tenant_id = request.get("_tenant_id")
        if tenant_id is None:
            if self._scope is None:
                raise ValueError("tenant_id is required when routing without a fixed scope")
            return None
        if not isinstance(tenant_id, str):
            raise ValueError("_tenant_id must be a string")
        agent_id = request.get("_agent_id")
        if agent_id is not None and not isinstance(agent_id, str):
            raise ValueError("_agent_id must be a string")
        dream_mode = request.get("_dream_mode")
        if dream_mode is not None and not isinstance(dream_mode, str):
            raise ValueError("_dream_mode must be a string")
        motive = request.get("_motive_name")
        if motive is not None and not isinstance(motive, str):
            raise ValueError("_motive_name must be a string")
        prompt_pack = request.get("_prompt_pack")
        if prompt_pack is not None and not isinstance(prompt_pack, str):
            raise ValueError("_prompt_pack must be a string")
        scope = request.get("_scope") or self._scope
        if scope is not None and not isinstance(scope, MemoryScope):
            raise ValueError("_scope must be a MemoryScope")
        return self._control_plane.resolve(
            tenant_id=tenant_id,
            agent_id=agent_id,
            scope=scope,
            dream_mode=dream_mode,
            motive=motive,
            prompt_pack=prompt_pack,
        )
