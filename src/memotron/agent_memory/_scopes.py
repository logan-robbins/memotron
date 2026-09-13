"""How a scope key is built for a project, an agent, or a user.

Sixteen lines, and worth a named file rather than folding into a grab-bag: these four
are the vocabulary the rest of the platform is written in -- `project_scope` and
`agent_scope` are imported by cli, adoption, migration and four probes. A scope is
the unit of isolation here, and getting the key wrong lands a write where nobody
reads it, with no error."""

from __future__ import annotations

from getpass import getuser
from hashlib import sha256

from memotron.agent_memory._common import (
    _normalize_non_blank,
)
from memotron.identity import normalize_agent_id
from memotron.models import (
    MemoryScope,
    ScopeKind,
)


def project_scope(project_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.TENANT, scope_id=_normalize_non_blank(project_id, "project_id"))


def agent_scope(agent_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.AGENT, scope_id=normalize_agent_id(agent_id))


def user_scope(user_id: str) -> MemoryScope:
    return MemoryScope(
        kind=ScopeKind.USER,
        scope_id=_normalize_non_blank(user_id, "user_id"),
    )


def default_user_id() -> str:
    """Return a stable, non-identifying ID for the current local OS account."""

    account = getuser().strip().casefold()
    if not account:
        raise ValueError("cannot derive user_id from the current OS account")
    return f"local-{sha256(account.encode('utf-8')).hexdigest()[:16]}"
