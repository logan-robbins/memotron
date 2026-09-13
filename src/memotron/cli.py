"""Memotron command-line entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from memotron import key_registry_cli
from memotron.adoption import (
    DEFAULT_GRAPH_PATH,
    LLM_API_KEY_ENV_DEFAULTS,
    LLM_BASE_URL_DEFAULTS,
    PROJECT_CONFIG_NAME,
    MemotronProjectConfig,
    build_platform_from_project,
    clear_project_llm,
    configure_project_llm,
    discover_project_root,
    hook_checkpoint_summary,
    hook_transcript_turns,
    infer_project_identity,
    initialize_claude_code_project,
    load_project_config,
    project_llm_status,
    session_task_run_id,
)
from memotron.agent_memory import AgentMemoryMode
from memotron.agent_memory_mcp import install_platform, mcp
from memotron.gateway import DEFAULT_GATEWAY_MODEL
from memotron.observability import configure_observability


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memotron",
        description="Configure and run Memotron memory.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser(
        "init",
        help="Configure the current Git repository for Claude Code.",
    )
    init.add_argument("--project-root", default="")
    init.add_argument(
        "--mode",
        choices=[mode.value for mode in AgentMemoryMode],
        default=None,
        help="Memory ownership model; new projects default to simple.",
    )
    init.add_argument("--project-id", default="")
    init.add_argument("--project-name", default="")
    init.add_argument("--project-goal", default="")

    mcp_command = commands.add_parser(
        "mcp",
        help="Run the repository-bound MCP server over stdio.",
    )
    mcp_command.add_argument("--project-root", default="")

    hook = commands.add_parser(
        "hook",
        help="Handle a Claude Code lifecycle event from JSON stdin.",
    )
    hook.add_argument(
        "event",
        choices=("session-start", "pre-compact", "post-compact", "session-end"),
    )
    hook.add_argument("--project-root", default="")

    status = commands.add_parser(
        "status",
        help="Show the active repository memory identity and scopes.",
    )
    status.add_argument("--project-root", default="")

    llm = commands.add_parser(
        "llm",
        help="Configure the repository's local Memotron LLM transport.",
    )
    llm_commands = llm.add_subparsers(dest="llm_command", required=True)
    llm_configure = llm_commands.add_parser(
        "configure",
        help="Seal an API key from an environment variable and save non-secret settings.",
    )
    llm_configure.add_argument(
        "--provider",
        choices=("litellm", "openai"),
        required=True,
    )
    llm_configure.add_argument("--model", required=True)
    llm_configure.add_argument(
        "--base-url",
        default="",
        help=("OpenAI-compatible endpoint; defaults to the JedAI Gateway for --provider litellm."),
    )
    llm_configure.add_argument(
        "--api-key-env",
        default="",
        help="Environment variable containing the key; provider-specific by default.",
    )
    llm_configure.add_argument("--project-root", default="")
    llm_status = llm_commands.add_parser(
        "status",
        help="Show non-secret LLM readiness and configuration.",
    )
    llm_status.add_argument("--project-root", default="")
    llm_clear = llm_commands.add_parser(
        "clear",
        help="Remove sealed project credentials and return to environment/fallback mode.",
    )
    llm_clear.add_argument("--project-root", default="")

    # The operator write path for the per-key registry (#168). Kept in its own
    # module: it is the only command here that talks to a DEPLOYED store rather
    # than a project-local one, and its authorization rule is possession of
    # database credentials rather than anything this file establishes.
    key_registry_cli.add_parser(commands)

    migrate = commands.add_parser(
        "migrate",
        help="Move (or copy) one tenant's memory to a new tenant id, in-place or across graph files.",
    )
    migrate.add_argument("--from-tenant-id", required=True)
    migrate.add_argument("--to-tenant-id", required=True)
    migrate.add_argument(
        "--graph-path",
        default=DEFAULT_GRAPH_PATH,
        help="Destination graph file (and source, unless --source-graph-path is given).",
    )
    migrate.add_argument(
        "--source-graph-path",
        default="",
        help="Source graph file, when migrating across two distinct graph files.",
    )
    migrate.add_argument(
        "--keep-source",
        action="store_true",
        help="Copy instead of move: leave the source tenant's derived state in place.",
    )
    migrate.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "init":
            _run_init(args)
        elif args.command == "mcp":
            _run_mcp(args)
        elif args.command == "hook":
            asyncio.run(_run_hook(args))
        elif args.command == "status":
            _run_status(args)
        elif args.command == "llm":
            _run_llm(args)
        elif args.command == "migrate":
            _run_migrate(args)
        elif args.command == "key":
            # Returns an exit code rather than raising: `show` on an unbound alias
            # is a legitimate answer (exit 1), not an error, and must stay
            # distinguishable from a storage fault by a script that checks $?.
            raise SystemExit(key_registry_cli.run(args))
        else:  # pragma: no cover - argparse enforces the command set
            raise ValueError(f"unsupported command: {args.command}")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"memotron: {exc}\n")


def _project_root(raw: str) -> Path:
    return discover_project_root(raw or None)


def _run_init(args: argparse.Namespace) -> None:
    # A non-TTY caller (CI, Claude Code hooks, any scripted invocation) must see
    # byte-identical behaviour to a purely flag-driven init: no prompt, no hang.
    interactive = sys.stdin.isatty()
    project_root = _project_root(args.project_root)
    mode = args.mode
    project_id = args.project_id
    project_name = args.project_name
    project_goal = args.project_goal

    existing_config: MemotronProjectConfig | None = None
    if (project_root / PROJECT_CONFIG_NAME).is_file():
        existing_config = load_project_config(project_root)

    if interactive:
        mode, project_id, project_name, project_goal = _prompt_init_fields(
            project_root=project_root,
            mode=mode,
            project_id=project_id,
            project_name=project_name,
            project_goal=project_goal,
        )

    result = initialize_claude_code_project(
        project_root=project_root,
        mode=mode,
        project_id=project_id,
        project_name=project_name,
        project_goal=project_goal,
    )
    print(json.dumps(result, indent=2, sort_keys=True))

    if interactive:
        _prompt_llm_provider(project_root)
        _offer_memory_migration(
            project_root=project_root,
            existing_config=existing_config,
            new_project_id=str(result["project_id"]),
            graph_path=Path(str(result["graph_path"])),
        )


def _prompt_init_fields(
    *,
    project_root: Path,
    mode: str | None,
    project_id: str,
    project_name: str,
    project_goal: str,
) -> tuple[str | None, str, str, str]:
    """Prompt only for fields the caller did not already pass as a flag.

    Reuses the SAME sentinels ``initialize_claude_code_project`` already
    treats as "not provided" (``mode is None``; ``project_id``/``project_name``/
    ``project_goal`` == ``""``) so a caller who passed some flags and not
    others is only prompted for the gap.
    """
    resolved_mode = mode
    if resolved_mode is None:
        choices = [item.value for item in AgentMemoryMode]
        answer = _prompt(f"Memory mode {choices} (default: {AgentMemoryMode.SIMPLE.value}): ").strip().lower()
        resolved_mode = answer or AgentMemoryMode.SIMPLE.value

    resolved_id, resolved_name = project_id, project_name
    if not resolved_id.strip() or not resolved_name.strip():
        try:
            inferred = infer_project_identity(project_root)
        except ValueError:
            inferred = None
        if inferred is not None:
            id_answer = _prompt(f"Project id [{inferred.id}] (Enter to accept): ").strip()
            resolved_id = id_answer or inferred.id
            name_answer = _prompt(f"Project name [{inferred.name}] (Enter to accept): ").strip()
            resolved_name = name_answer or inferred.name
        else:
            print("No Git remote found; project id/name cannot be inferred.")
            resolved_id = _prompt("Project id: ").strip()
            resolved_name = _prompt("Project name: ").strip()

    resolved_goal = project_goal
    if not resolved_goal.strip():
        resolved_goal = _prompt("Project goal (optional, Enter to skip): ").strip()

    return resolved_mode, resolved_id, resolved_name, resolved_goal


def _prompt_llm_provider(project_root: Path) -> None:
    choices = ("environment", "litellm", "openai")
    answer = _prompt(f"LLM provider {choices} (default: environment): ").strip().lower()
    provider = answer or "environment"
    if provider not in choices:
        print(f"Unrecognized provider {provider!r}; keeping environment/fallback mode.")
        return
    if provider == "environment":
        return
    default_model = DEFAULT_GATEWAY_MODEL if provider == "litellm" else ""
    model = (
        _prompt(f"{provider} model name" + (f" (default: {default_model})" if default_model else "") + ": ").strip()
        or default_model
    )
    default_base_url = LLM_BASE_URL_DEFAULTS[provider]
    base_url = _prompt(f"{provider} base URL (default: {default_base_url}): ").strip() or default_base_url
    default_env = LLM_API_KEY_ENV_DEFAULTS[provider]
    api_key_env = (
        _prompt(f"Environment variable holding the {provider} API key (default: {default_env}): ").strip()
        or default_env
    )
    try:
        configure_project_llm(
            project_root=project_root,
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
        )
        print(f"Configured {provider} ({model}); reading the key from {api_key_env}.")
    except ValueError as exc:
        print(f"Could not configure {provider} yet: {exc}")
        print("Run `memotron llm configure` later once the key is available.")


def _offer_memory_migration(
    *,
    project_root: Path,
    existing_config: MemotronProjectConfig | None,
    new_project_id: str,
    graph_path: Path,
) -> None:
    """Detect pre-existing local memory under a different tenant id and offer to move it.

    Two cases, mutually exclusive (matching the approved plan):
    (a) re-running init with a NEW project id while an old ``.memotron.yaml``
        already claimed a different one — offer to move that project's own
        prior memory; or otherwise
    (b) list OTHER tenant scopes already present in the same (commonly shared,
        ``simple``-mode) graph file and offer to import from one.
    A CRYPTO_SHRED-blocked tenant is always shown, never silently skipped.
    """
    from memotron.graph import PropertyGraphStore
    from memotron.migration import migrate_tenant_memory, preview_tenant_migration
    from memotron.models import ScopeKind

    store = PropertyGraphStore(graph_path)
    try:
        candidates: list[tuple[str, str]] = []
        if existing_config is not None and existing_config.project.id != new_project_id:
            candidates.append((existing_config.project.id, "the previous identity of this project"))
        else:
            for scope in store.scopes():
                if scope.kind != ScopeKind.TENANT or scope.scope_id == new_project_id:
                    continue
                candidates.append((scope.scope_id, "another local project"))

        for source_tenant_id, label in candidates:
            preview = preview_tenant_migration(store, source_tenant_id=source_tenant_id, dest_tenant_id=new_project_id)
            if preview.blocked_reason is not None:
                print(
                    f"Found memory for {label} ({source_tenant_id!r}) but it cannot be "
                    f"imported automatically: {preview.blocked_reason}"
                )
                continue
            total_facts = preview.active_relationship_count + preview.agent_scoped_relationship_count
            total_episodes = preview.episode_count + preview.agent_scoped_episode_count
            if total_facts == 0 and total_episodes == 0:
                continue
            # Report the agent-scope share explicitly: in multi-agent mode it is
            # routinely almost all of it, and a tenant-only count would badly
            # understate what is about to move.
            detail = ""
            if preview.agent_scoped_relationship_count:
                detail = (
                    f" ({preview.agent_scoped_relationship_count} of them held by "
                    f"{preview.agent_registration_count} registered agents)"
                )
            print(
                f"Found existing memory for {label} ({source_tenant_id!r}): "
                f"{total_facts} facts across {total_episodes} episodes{detail}."
            )
            answer = _prompt(f"Move this memory into {new_project_id!r} now? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                continue
            result = migrate_tenant_memory(
                store,
                source_tenant_id=source_tenant_id,
                dest_tenant_id=new_project_id,
                purge_source=True,
            )
            print(
                f"Migrated {result.relationships_migrated} facts and "
                f"{result.episodes_migrated} episodes from {source_tenant_id!r} into {new_project_id!r}."
            )
    finally:
        store.close()


def _prompt(text: str) -> str:
    try:
        return input(text)
    except EOFError:
        return ""


def _run_migrate(args: argparse.Namespace) -> None:
    """Preview, confirm, and run a tenant memory migration.

    Non-interactive callers are gated exactly like ``_run_init``: the
    confirmation prompt requires ``sys.stdin.isatty()``. A non-TTY caller (CI, a
    pipe, a Claude Code hook) that did not pass ``--yes`` fails fast with a clear
    error instead of prompting. Relying on ``input()`` raising ``EOFError`` is
    not equivalent — an open-but-idle pipe blocks forever, and this command's
    default is a destructive purge of the source tenant.
    """
    from memotron.graph import PropertyGraphStore
    from memotron.migration import migrate_tenant_memory, preview_tenant_migration

    if not args.yes and not sys.stdin.isatty():
        raise ValueError(
            "migrate needs an interactive terminal to confirm a destructive move; "
            "re-run with --yes to confirm non-interactively "
            "(add --keep-source to copy instead of move)"
        )

    dest_path = Path(args.graph_path).expanduser().resolve()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    source_path = Path(args.source_graph_path).expanduser().resolve() if args.source_graph_path.strip() else dest_path
    source_store = PropertyGraphStore(source_path)
    dest_store = None if source_path == dest_path else PropertyGraphStore(dest_path)
    try:
        preview = preview_tenant_migration(
            source_store,
            source_tenant_id=args.from_tenant_id,
            dest_tenant_id=args.to_tenant_id,
            dest_store=dest_store,
        )
        print(json.dumps(preview.model_dump(mode="json"), indent=2, sort_keys=True))
        if preview.blocked_reason is not None:
            raise ValueError(preview.blocked_reason)
        if not args.yes:
            action = "Copy" if args.keep_source else "Move"
            confirmation = _prompt(f"{action} tenant {args.from_tenant_id!r} into {args.to_tenant_id!r}? [y/N] ")
            if confirmation.strip().lower() not in ("y", "yes"):
                print("Aborted; nothing was changed.")
                return
        result = migrate_tenant_memory(
            source_store,
            source_tenant_id=args.from_tenant_id,
            dest_tenant_id=args.to_tenant_id,
            dest_store=dest_store,
            purge_source=not args.keep_source,
        )
        print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    finally:
        source_store.close()
        if dest_store is not None:
            dest_store.close()


def _run_mcp(args: argparse.Namespace) -> None:
    # #129. Safe here ONLY because `configure_logging` writes to stderr: this transport is
    # stdio, so stdout is the MCP wire and anything written to it corrupts the session.
    configure_observability(service_name="memotron-agent-memory-mcp")

    platform, _ = build_platform_from_project(_project_root(args.project_root))
    install_platform(platform)
    try:
        mcp.run(transport="stdio")
    finally:
        platform.client.graph.close()


async def _run_hook(args: argparse.Namespace) -> None:
    hook_input = _read_hook_input()
    platform, config = build_platform_from_project(_project_root(args.project_root))
    try:
        if args.event == "session-start":
            # WS-28 T1: checkpoint-seeded rehydration.  Only a compaction
            # re-fire (source=compact) looks up a checkpoint at all -- a
            # normal session start passes checkpoint_seed=None and
            # memory_start behaves byte-identically to before this hooked
            # in.  No checkpoint yet (or a non-compact source) is the same
            # None, so this is a strict superset of prior behavior.
            checkpoint_seed = None
            if str(hook_input.get("source") or "").strip() == "compact":
                session_id = str(hook_input.get("session_id") or "").strip()
                if session_id:
                    checkpoint_seed = platform.latest_compaction_checkpoint(
                        agent_id=config.caller.id, session_id=session_id
                    )
            started = await platform.memory_start(
                agent_id=config.caller.id,
                task_run_id=session_task_run_id(hook_input),
                checkpoint_seed=checkpoint_seed,
            )
            print(
                "\n".join(
                    (
                        (f"Memotron {config.mode.value} memory loaded for {config.project.name}."),
                        f"Memotron task_run_id: {started.task_run_id}",
                        started.rendered_context,
                        (
                            "Use memory_search before relying on prior work. "
                            "Use memory_remember for personal facts and memory_publish "
                            "for repository facts."
                            if config.mode == AgentMemoryMode.SIMPLE
                            else (
                                "Use memory_search before relying on prior work. "
                                "Use memory_remember for agent facts and memory_publish "
                                "for shared repository facts."
                            )
                        ),
                    )
                )
            )
            return

        if args.event == "pre-compact":
            # Capture the dying context BEFORE compaction: the transcript is
            # still complete here, so the checkpoint carries real task focus,
            # recent assistant state, tools, and files — not a placeholder.
            # The transcript is parsed ONCE and shared with the citation scan.
            turns = hook_transcript_turns(hook_input)
            task_run_id = session_task_run_id(hook_input)
            await platform.memory_log(
                agent_id=config.caller.id,
                summary=hook_checkpoint_summary(hook_input, event="pre-compact", turns=turns),
                checkpoint_reason="context_compaction",
                task_run_id=task_run_id,
            )
            # WS-15 T8/T9: auto-record CITED_OR_USED for injected/retrieved
            # facts the assistant actually used — after the checkpoint, before
            # the refresh.  Zero candidates / no transcript is a no-op.
            await platform.record_transcript_citations(
                agent_id=config.caller.id,
                turns=turns,
                session_id=str(hook_input.get("session_id") or "").strip(),
                task_run_id=task_run_id,
            )
            # WS-28 T3: deterministic runbook capture — reuses the SAME
            # already-parsed turns.  Zero qualifying commands is a no-op.
            await platform.record_runbook_commands(
                agent_id=config.caller.id,
                turns=turns,
                task_run_id=task_run_id,
            )
        elif args.event == "post-compact":
            # Persist Claude Code's own generated summary (explicit field, or
            # the isCompactSummary transcript entry); a missing summary still
            # records the boundary and never aborts the post-boundary refresh.
            # Context re-injection after compaction is SessionStart's job —
            # the matcher-less SessionStart hook re-fires with source=compact.
            await platform.memory_log(
                agent_id=config.caller.id,
                summary=hook_checkpoint_summary(hook_input, event="post-compact"),
                checkpoint_reason="context_compaction",
                task_run_id=session_task_run_id(hook_input),
            )
        elif args.event == "session-end":
            # Flush a final checkpoint so the ending session's state survives
            # into the next memory_start, then run due dreams below.
            turns = hook_transcript_turns(hook_input)
            task_run_id = session_task_run_id(hook_input)
            session_id = str(hook_input.get("session_id") or "").strip()
            await platform.memory_log(
                agent_id=config.caller.id,
                summary=hook_checkpoint_summary(hook_input, event="session-end", turns=turns),
                checkpoint_reason="session_end",
                task_run_id=task_run_id,
            )
            await platform.record_transcript_citations(
                agent_id=config.caller.id,
                turns=turns,
                session_id=session_id,
                task_run_id=task_run_id,
            )
            # WS-28 T3: deterministic runbook capture — reuses the SAME
            # already-parsed turns.  Zero qualifying commands is a no-op.
            await platform.record_runbook_commands(
                agent_id=config.caller.id,
                turns=turns,
                task_run_id=task_run_id,
            )
            # WS-15 T10: session end is the one boundary where outcomes are
            # judged (mid-session outcomes would be premature).  No judge
            # configured → explicit skipped result; an out-of-contract judge
            # response is receipted inside judge_session_outcomes and must
            # never fail the hook — the checkpoint and refresh still land.
            # A rejection is already receipted as SESSION_OUTCOME_JUDGE_REJECTED,
            # so swallowing it here loses nothing and keeps the hook alive.
            with contextlib.suppress(ValueError):
                await platform.judge_session_outcomes(
                    agent_id=config.caller.id,
                    turns=turns,
                    session_id=session_id,
                    task_run_id=task_run_id,
                )
        await platform.memory_refresh(
            agent_id=config.caller.id,
            include_project=True,
        )
    finally:
        platform.client.graph.close()


def _run_status(args: argparse.Namespace) -> None:
    platform, config = build_platform_from_project(_project_root(args.project_root))
    try:
        print(
            json.dumps(
                {
                    "mode": config.mode.value,
                    "project": config.project.model_dump(mode="json"),
                    "caller": config.caller.model_dump(mode="json"),
                    "graph_path": str(config.graph_path()),
                    "project_scope": platform.project_scope.model_dump(mode="json"),
                    "personal_scope": (
                        platform.user_scope.model_dump(mode="json") if platform.user_scope is not None else None
                    ),
                    "continuity_scope": platform.agent_scope(config.caller.id).model_dump(mode="json"),
                    "project_memory": platform.project_memory_status(),
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        platform.client.graph.close()


def _run_llm(args: argparse.Namespace) -> None:
    root = _project_root(args.project_root)
    if args.llm_command == "configure":
        result = configure_project_llm(
            project_root=root,
            provider=args.provider,
            model=args.model,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
        )
    elif args.llm_command == "status":
        result = project_llm_status(root)
    elif args.llm_command == "clear":
        result = clear_project_llm(root)
    else:  # pragma: no cover - argparse enforces the command set
        raise ValueError(f"unsupported llm command: {args.llm_command}")
    print(json.dumps(result, indent=2, sort_keys=True))


def _read_hook_input() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude Code hook stdin is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Claude Code hook stdin must be a JSON object")
    return payload


if __name__ == "__main__":
    main()
