"""Sweep every public method on Memotron + AgentMemoryPlatform.

Builds arguments from type hints and parameter names, calls each method against a
DISPOSABLE graph, and classifies OK / EMPTY / ERROR / SKIP.

Only REQUIRED parameters (no default) are supplied — this tests the documented
minimum call, which is what a new user would write.
"""

from __future__ import annotations

import asyncio
import enum
import inspect
import json
import os
import typing
from datetime import UTC, datetime

from memotron import Memotron, MemoryScope, ScopeKind

STATE = {}


def unwrap(ann):
    """Strip Optional/Union/Annotated down to a usable concrete type."""
    origin = typing.get_origin(ann)
    if origin is typing.Union:
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        return unwrap(args[0]) if args else str
    return ann


def make_arg(name, ann, dw):
    a = unwrap(ann)
    scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="memotron-dogfood")
    byname = {
        "scope": scope,
        "source_scope": scope,
        "dest_scope": scope,
        "target_scope": scope,
        "query": "Postgres",
        "subject": "sweep-subject",
        "predicate": "requires",
        "object": "sweep-object",
        "relationship_type": "REQUIRES",
        "name": "sweep-item",
        "episode_body": "Sweep episode body.",
        "content": "sweep content",
        "body": "sweep body",
        "text": "sweep text",
        "summary": "sweep summary",
        "agent_id": "claude-code",
        "agent_name": "Claude Code",
        "tenant_id": "memotron-dogfood",
        "task_run_id": "sweep-1",
        "idempotency_key": "sweep-idem",
        "reason": "sweep",
        "rationale": "sweep",
        "relationship_uuid": STATE.get("rel"),
        "run_uuid": STATE.get("run"),
        "episode_uuid": STATE.get("ep"),
        "use_id": STATE.get("use"),
        "job_name": "formation-default",
        "entity": "Postgres cutover",
        "provider": "litellm",
        "api_key": "sk-sweep",
        "base_url": "https://example.invalid/v1",
        "model": "claude-haiku-4-5",
        "motive": "agent-memory",
        "motive_name": "agent-memory",
        "corrected_object": "sweep-corrected",
        "artifact_id": "sweep-artifact",
        "location": "/tmp/x",
        "author": "sweep",
        "resolved_by": "sweep",
        "decision": "approve",
        "verdict": "helpful",
        "kind": "injected",
        "judge_identity": "sweep",
        "judge_input_digest": "0" * 64,
        "now": datetime.now(UTC),
        "as_of": None,
    }
    if name in byname and byname[name] is not None:
        return byname[name]
    if isinstance(a, type) and issubclass(a, enum.Enum):
        return next(iter(a))
    if a is bool:
        return False
    if a is int:
        return 1
    if a is float:
        return 0.5
    if a is datetime:
        return datetime.now(UTC)
    if a is str:
        return "sweep-value"
    if a is MemoryScope:
        return scope
    return None


def classify(val):
    if val is None:
        return "EMPTY", "returned None"
    if isinstance(val, (list, tuple, set, dict)) and len(val) == 0:
        return "EMPTY", f"empty {type(val).__name__}"
    r = repr(val)
    return "OK", r[:80].replace("\n", " ")


async def sweep(obj, label, dw):
    names = [n for n in dir(obj) if not n.startswith("_") and callable(getattr(obj, n, None))]
    rows = []
    for n in sorted(names):
        fn = getattr(obj, n)
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        try:
            hints = typing.get_type_hints(fn)
        except Exception:
            hints = {}
        kwargs, missing = {}, []
        for pname, p in sig.parameters.items():
            if pname in ("self", "args", "kwargs"):
                continue
            if p.default is not inspect.Parameter.empty:
                continue  # required only
            v = make_arg(pname, hints.get(pname, p.annotation), dw)
            if v is None:
                missing.append(pname)
            else:
                kwargs[pname] = v
        if missing:
            rows.append((n, "SKIP", "no value for " + ",".join(missing[:3])))
            continue
        try:
            out = fn(**kwargs)
            if inspect.isawaitable(out):
                out = await asyncio.wait_for(out, timeout=60)
            v, d = classify(out)
        except Exception as e:
            v, d = "ERROR", f"{type(e).__name__}: {str(e)[:95]}"
        rows.append((n, v, d))
        print(f"  {n:36s} {v:6s} {d[:88]}")
    return rows


async def main():
    graph = os.environ["MEMOTRON_GRAPH_PATH"]
    dw = Memotron(graph_path=graph)
    # harvest a real relationship uuid so uuid-taking methods have a target
    for sc in dw.graph.scopes():
        rels = dw.graph.relationships_for_scope(sc.key)
        if rels:
            STATE["rel"] = rels[0].uuid
            break
    print(f"  [seeded relationship_uuid={str(STATE.get('rel'))[:8]}]\n")

    print("===== Memotron (SDK) =====")
    a = await sweep(dw, "Memotron", dw)
    print("\n===== AgentMemoryPlatform =====")
    from memotron.agent_memory_mcp import build_platform_from_env

    p = build_platform_from_env()
    b = await sweep(p, "AgentMemoryPlatform", dw)

    print("\n===== TALLY =====")
    for lbl, rows in (("Memotron", a), ("AgentMemoryPlatform", b)):
        c = {}
        for _, v, _ in rows:
            c[v] = c.get(v, 0) + 1
        print(f"  {lbl:22s} total={len(rows):3d}  " + "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
    json.dump({"Memotron": a, "AgentMemoryPlatform": b}, open("/tmp/sdk_sweep.json", "w"), indent=1)


asyncio.run(main())
