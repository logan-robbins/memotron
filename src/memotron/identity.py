"""Canonical tenant-agent identity validation."""

from __future__ import annotations

import os
import re

_AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")
MAX_AGENT_NAME_LENGTH = 128


def normalize_agent_id(value: str) -> str:
    """Validate and normalize a stable agent identifier."""

    normalized = value.strip()
    if not normalized:
        raise ValueError("agent_id cannot be blank")
    if not _AGENT_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            "agent_id must be 1-64 ASCII letters, digits, '.', '_', or '-', "
            "and must start and end with a letter or digit"
        )
    return normalized


def agent_id_key(value: str) -> str:
    """Return the tenant-registry uniqueness key for an agent identifier."""

    return normalize_agent_id(value).casefold()


def normalize_agent_name(value: str) -> str:
    """Validate and normalize an agent display name."""

    normalized = " ".join(value.strip().split())
    if not normalized:
        raise ValueError("agent_name cannot be blank")
    if len(normalized) > MAX_AGENT_NAME_LENGTH:
        raise ValueError(f"agent_name cannot exceed {MAX_AGENT_NAME_LENGTH} characters")
    return normalized


def agent_name_key(value: str) -> str:
    """Return the tenant-registry uniqueness key for an agent display name."""

    return normalize_agent_name(value).casefold()


# ---------------------------------------------------------------------------
# Per-request identity vocabulary (#126, relocated by #206 Phase 2)
# ---------------------------------------------------------------------------
# These four lived in `mcp_auth` until the agent-memory platform needed them, which made
# `application agent_memory -> delivery mcp_auth` an UPWARD TIER EDGE and failed
# `coupling_report.py`. They are relocated here rather than baselined, following the SZ-2
# precedent recorded in that file's baseline comment: the last time this metric went to 1
# it was fixed by moving `storage/settings.py` into `config`, not by raising the number.
#
# `identity` is the right home and not merely a convenient one. None of these four touches
# a transport: two are exception types, one is a constant, and `identity_required` reads an
# environment variable. What made `mcp_auth` delivery-tier is the ASGI middleware and the
# in-flight request lookup, and neither of those moves.
#
# `mcp_auth` re-exports all four, so its public surface is unchanged.


#: Rollout flag. Off means the per-request identity guard does nothing at all.
REQUIRE_IDENTITY_ENV = "MEMOTRON_REQUIRE_GATEWAY_IDENTITY"


class ScopeNotAuthorizedError(PermissionError):
    """The caller is authenticated, and the scope it named is not one of its own."""


class IdentityUnavailableError(PermissionError):
    """Identity is required and no principal reached the callee.

    Distinct from :class:`ScopeNotAuthorizedError` on purpose: that one is a caller error and
    this one is almost always **ours** -- the request never passed through the middleware.
    Merging them would file a deployment fault under "user tried something they shouldn't".
    """


def identity_required() -> bool:
    """Whether the per-request identity guard is armed.

    Reads the environment on every call rather than caching at import. Guard state is
    something an operator flips and then verifies; a value frozen at import time makes a
    live pod disagree with its own env and is unobservable until a restart.

    The truthy set matches ``observability/_logging.py:_env_flag`` -- ``1``/``true``/``yes``
    only. A flag that accepts any non-empty string turns a typo into a silent security
    change, and this is the flag where that matters most.
    """
    return os.environ.get(REQUIRE_IDENTITY_ENV, "").strip().lower() in {"1", "true", "yes"}
