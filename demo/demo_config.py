#!/usr/bin/env python
"""Apply "demo mode" to the isolated Memotron demo tenant.

Run with:  uv run demo/demo_config.py            # apply + print effective policy
           uv run demo/demo_config.py --show     # print only, change nothing

Demo mode means: **few, predictable memories**.  Four levers do that, and this
file is the only place any of them is set.

  1. agent_guidance in .memotron.yaml      -> WHEN the agent writes at all
  2. project-memory allowed_memory_types      -> WHICH types can ever exist
  3. project-memory min_salience + candidate  -> HOW MANY survive per publish
     cap
  4. project-memory dedup_threshold           -> when a restatement REINFORCES
                                                 instead of creating a row

It also seals the tenant LLM credential (extraction needs a real model) and
rewires the workspace's .mcp.json + Claude Code hooks onto demo/bin/dw-demo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import _demopath  # noqa: F401  MUST BE FIRST — unshadows stdlib `inspect`
import yaml
from _demo import (
    DEMO_AGENT_ID,
    DEMO_GRAPH_PATH,
    DEMO_PROJECT_ID,
    DEMO_WORKSPACE,
    REPO_ROOT,
    assert_isolated,
    banner,
    build_demo_platform,
    bullet,
    env_var_present,
    fail,
    kv,
    load_repo_env,
    section,
    table,
    workspace_or_fail,
)

# ===========================================================================
# THE ONE EXTRACTION KNOB IN THE ENTIRE DEMO KIT
# ===========================================================================
# Extraction turns a published candidate into typed graph facts.  Two paths
# exist and the demo works on either:
#
#   "llm"            DEFAULT.  The sealed tenant credential in DEMO_LLM below,
#                    through the JedAI Gateway — extraction, dream-agent
#                    decisions, and theme synthesis.  Reads free prose; needs a
#                    working key.  This is what a real deployment does.
#   "deterministic"  RuleBasedExtractionTransport + LocalDreamAgentTransport +
#                    deterministic theme labels.  Zero network, zero cost,
#                    identical output every run.  Free-prose publishes are NOT
#                    extracted — only the structured line is.
#
# KEY STATUS (verified live 2026-08-12, not assumed):
#   The personal Anthropic key is retired: it was out of credit (HTTP 400
#   "Your credit balance is too low"), and Memotron no longer has an
#   Anthropic-native transport at all.
#   LITELLM_API_KEY    -> WORKS, but only against the *preview* (Integration)
#                         gateway.  It 401s on latest/stage/prod, so the
#                         environment in DEMO_LLM below matters:
#                           preview  https://preview.jedai-gateway...  200, 71 models
#                           latest   https://latest.jedai-gateway...   401
#
# The gateway path is proven working end to end (extraction + dream-agent
# decisions + theme synthesis all return 200 and parse).
#
# TWO gaps used to block "llm", and BOTH are now closed:
#
#   1. Strictness.  Per-candidate schema rejection is non-fatal and receipted
#      (CANDIDATE_SCHEMA_REJECTED), so a model that invents a label or a
#      property key costs you that one candidate, never the episode.
#
#   2. Predicate folding — the real one.  Extraction used to produce, 3/3 at
#      temperature 0, from the demo's canonical publish line:
#
#        Memory: subject=checkout-api; predicate=decided; object=use DynamoDB ...
#
#        rule-based   -> predicate="decided"
#                        object="use DynamoDB for the reservation ledger"
#        claude-haiku -> predicate="decided to use DynamoDB for the reservation ledger"
#                        object="DynamoDB"
#
#      Truth keys are scope:subject:predicate, so a predicate that swallowed the
#      object put the Stage 2 correction in a DIFFERENT slot from the Stage 1
#      decision: supersession never fired (superseded=0), the restated
#      requirement created a row instead of reinforcing (reinforced=0), and the
#      directive cluster never reached a theme (theme=0).
#
#      This was NOT inherent LLM variance.  It was a Memotron prompt defect:
#      the rendered extraction prompt's only substantive mention of `predicate`
#      was the field list, so the model invented its own granularity, and the
#      relationship-type query text pulled it toward a verbose paraphrase.  A
#      minimal hand-written prompt got it right 3/3 on the same model, same
#      episode, same temperature.
#
#      Fixed in the engine, not worked around here: DreamInstructionSet.
#      render_prompt now states an explicit predicate contract (short verb
#      phrase naming the relation only, never containing the object, with the
#      correct/incorrect example pair), and
#      InstructionalExtractor._validate_memory enforces it, rejecting a folded
#      candidate as PREDICATE_NOT_A_VERB_PHRASE with a receipt rather than
#      rewriting it.  Re-probed live on the same model/temperature afterwards:
#      predicate="decided", object="use DynamoDB for the reservation ledger",
#      3/3.  See tests/test_predicate_contract.py.
#
# So "llm" is the default.  `uv run demo/selftest.py --reset` PASSES in this
# mode: Dream 1 forms 3 facts, Dream 2 supersedes 1, reinforces 1, synthesizes 1
# theme, demotes its 3 members, and leaves context_visible flat at 3 — with
# extraction, dream-agent decisions, and theme synthesis all going through the
# gateway on this tenant's virtual key.
#
# Flip to "deterministic" for an offline, zero-cost, byte-identical rehearsal
# (airplane, no key, or a run that must not spend).  The demo's `publish_when`
# guidance tells the agent to publish ONE structured line per confirmed fact:
#
#     Memory: subject=...; predicate=...; object=...; relationship_type=...; confidence=0.9
#
# which is the documented input format of the hermetic
# RuleBasedExtractionTransport (extraction.py::_parse_memory_lines) and equally
# readable by the LLM extractor — so the same publishes produce the same numbers
# either way.  What "deterministic" gives up is free-prose extraction: a publish
# that is NOT in that line format yields nothing.
DEMO_EXTRACTION_MODE = "llm"

# The credential that "llm" mode uses.  It is sealed into the demo graph by
# setup in either mode, so flipping the switch above is genuinely a one-line
# change.  Only the variable NAME ever appears anywhere in this kit; no script
# prints, echoes, logs, or copies a key value.
#
# provider="litellm" routes extraction, theme synthesis, AND dream-agent
# decisions through the gateway (see runtime.build_transports_from_*), so the
# demo runs entirely on JedAI Platform infrastructure with spend attributed to
# this tenant's virtual key.  Model names on the gateway are UNDATED aliases —
# "claude-haiku-4-5-20251001" does not resolve there.
DEMO_LLM = {
    "provider": "litellm",
    "model": "claude-haiku-4-5",
    "base_url": "https://preview.jedai-gateway.wdprapps.disney.com/v1",
    "api_key_env": "LITELLM_API_KEY",
}

# ===========================================================================
# LEVER 1 — agent_guidance: when Claude Code writes at all
# ===========================================================================
# `memotron init` renders these lists verbatim into
# .claude/rules/memotron.md, which is always loaded in the session.  This
# is the operator's main control over write volume.  Demo mode makes writes
# rare, deliberate, one-fact-at-a-time, and structurally predictable.
DEMO_AGENT_GUIDANCE = {
    "cadence": "event-driven",
    "search_when": [
        "At the start of every task, before answering anything about this project.",
        "Whenever the user asks what you know, or refers to an earlier decision, requirement, or rule.",
        "Before you accept a statement that may contradict something already remembered.",
    ],
    "remember_when": [
        "Only when the user states a durable personal working preference about themselves.",
        "Project facts never go here — they go through memory_publish.",
    ],
    "publish_when": [
        "Exactly once, immediately after the user confirms one durable project DECISION, REQUIREMENT, or DIRECTIVE.",
        "Publish ONE candidate per confirmed fact. Never batch facts, never publish a summary of the conversation.",
        "Format every candidate as exactly one line: "
        "Memory: subject=<entity>; predicate=<verb>; object=<value>; "
        "relationship_type=<DECIDES|REQUIRES|SHOULD>; confidence=0.9",
        "Use DECIDES for a chosen decision, REQUIRES for a hard requirement or "
        "constraint, SHOULD for a procedural rule the team must follow.",
        "Write zero memories for questions, exploration, command output, or anything "
        "the user has not explicitly confirmed. Zero writes is the correct answer "
        "most of the time.",
    ],
    "review_when": [
        "After the user confirms or corrects a fact.",
        "Before answering any 'what do you know about X' question.",
    ],
    "refresh_when": [
        "Never call memory_refresh in this demo. The operator triggers dreaming "
        "explicitly (demo/dream.py) so the graph change is visible on screen.",
    ],
}

# ===========================================================================
# LEVERS 2-4 — the demo Motive (project-memory policy)
# ===========================================================================
# configure_project_memory() versions this policy AND compiles it into a
# Motive named `project-memory-policy`, which is what actually governs
# formation in tenant:memotron-demo:
#
#   allowed_memory_types  -> Motive.allowed_memory_types   (hard type gate)
#   min_salience          -> Motive.salience_rubric.min_salience
#   max_memories_per_...  -> Motive.salience_rubric.max_memories_per_episode
#   dedup_threshold       -> Motive.dedup_threshold
#   protected_memory_...  -> Motive.retention.protected_memory_types
DEMO_PROJECT_MEMORY = {
    "project_goal": (
        "Keep the checkout-api team's durable engineering decisions, requirements, "
        "and operating rules correct and current."
    ),
    "memory_goal": (
        "Preserve only the small set of confirmed facts a future session must know "
        "to make correct decisions about checkout-api."
    ),
    "keep": [
        "Confirmed architectural and technology decisions, with their rationale.",
        "Hard requirements and service-level constraints the team has committed to.",
        "Procedural rules the team must follow before merging or shipping.",
    ],
    "exclude": [
        "Anything the user has not explicitly confirmed.",
        "Personal preferences, scratch work, command output, and file contents.",
        "Secrets, credentials, and tokens.",
    ],
    "rules": [
        "Prefer the newest adequately authoritative fact while preserving supersession history.",
        "One fact per candidate. Do not split one confirmed statement into several facts.",
    ],
    # LEVER 2 — hard type gate.  No PREFERENCE, no STATE, no IDENTITY, no
    # INCIDENT: the demo can literally only produce decisions, requirements,
    # directives, and the THEME rows consolidation derives from them.
    "allowed_memory_types": ["decision", "requirement", "directive", "theme"],
    "protected_memory_types": ["decision", "requirement"],
    # LEVER 3 — salience floor + per-candidate cap.
    # salience = recency x importance x relevance, with importance scaled by
    # confidence.  At 0.9 confidence: requirement ~0.81, decision ~0.77,
    # directive ~0.45 before the relevance factor.  0.25 keeps all three and
    # drops incidental chatter; the cap of 2 means one publish can never turn
    # into a wall of facts.
    "min_salience": 0.25,
    "max_memories_per_candidate": 2,
    # LEVER 4 — conservative dedup.  0.92 (vs the 0.87 default) means a
    # restatement must be very close to the stored fact before it reinforces
    # instead of creating a second row.  That keeps the four Stage-2 facts
    # distinct while an exact restatement still reinforces on the exact-object
    # path.
    "dedup_threshold": 0.92,
    "min_endorsements": 1,
}

# ===========================================================================
# Supersession: why the Stage-2 correction lands in ONE dream cycle
# ===========================================================================
# SupersessionPolicy lives on DreamConfig (the packaged base config), not on
# the tenant policy, so `memotron mcp` always runs the shipped defaults:
#
#     corroboration_margin   = 3
#     corroboration_required = 2
#
# Meaning: an equal-authority challenger only parks for review once the
# incumbent's observed_count has reached 3.  The demo deliberately states the
# fact that gets corrected exactly ONCE (observed_count = 1), so the
# correction is below the margin and flips current truth immediately —
# visible supersession in a single cycle, with no policy override needed.
#
# For a knowledge-base tenant where the document IS ground truth and facts are
# repeated across many pages, the documented override is the KB-style
# SupersessionPolicy(corroboration_required=1) — see INGEST.md section 2.3.
# It cannot be set per-tenant today (DreamConfig-level field), so the demo
# stays under the margin instead of pretending otherwise.
SUPERSESSION_NOTE = (
    "corroboration_margin=3 / corroboration_required=2 (packaged default); "
    "demo keeps the corrected fact at observed_count=1 so it flips in one cycle"
)


# ---------------------------------------------------------------------------
# .memotron.yaml patching
# ---------------------------------------------------------------------------


def patch_project_yaml(workspace: Path) -> dict:
    """Point the workspace at the isolated demo graph and install demo guidance."""

    path = workspace / ".memotron.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    payload["storage"] = {"graph_path": str(DEMO_GRAPH_PATH)}
    payload["agent_guidance"] = DEMO_AGENT_GUIDANCE
    payload["llm"] = dict(DEMO_LLM)
    payload["project_memory"] = {
        **payload.get("project_memory", {}),
        **DEMO_PROJECT_MEMORY,
    }

    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return payload


# ---------------------------------------------------------------------------
# Rewire MCP + hooks onto the demo shim
# ---------------------------------------------------------------------------


def rewire_to_demo_shim(workspace: Path) -> tuple[Path, Path]:
    """Replace the `memotron` executable with demo/bin/dw-demo.

    `memotron init` writes `command: memotron` (a PATH install).  The
    demo instead runs out of this repository's virtualenv AND swaps the
    dream-agent transport, so both wirings point at demo/bin/dw-demo with an
    absolute --project-root.  Same argument shape, same behaviour, minus the
    nested-`claude` failure.
    """

    launcher = str((REPO_ROOT / "demo" / "bin" / "dw-demo").resolve())
    project_root = str(workspace.resolve())

    mcp_path = workspace / ".mcp.json"
    mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    mcp["mcpServers"]["memotron_agent_memory"] = {
        "type": "stdio",
        "command": launcher,
        "args": ["mcp", "--project-root", project_root],
        "alwaysLoad": True,
    }
    mcp_path.write_text(json.dumps(mcp, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Hooks must be REPLACED, not patched in place.  `memotron init`
    # de-duplicates only entries whose command is literally "memotron", so
    # once an entry has been rewired to dw-demo, init stops recognising it and
    # appends a second handler on every subsequent run — the boundary would
    # then fire twice, four times, ... Drop every Memotron hook handler
    # (either command spelling) and write exactly one back.
    settings_path = workspace / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks = settings.setdefault("hooks", {})
    for event, hook_event in (
        ("SessionStart", "session-start"),
        ("PreCompact", "pre-compact"),
        ("PostCompact", "post-compact"),
        ("SessionEnd", "session-end"),
    ):
        entries = [entry for entry in hooks.get(event, []) if not _is_memotron_hook_entry(entry)]
        entries.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": launcher,
                        "args": ["hook", hook_event, "--project-root", project_root],
                        "timeout": 30 if event == "SessionStart" else 60,
                        "statusMessage": "Syncing Memotron memory",
                    }
                ]
            }
        )
        hooks[event] = entries
    settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return mcp_path, settings_path


def _is_memotron_hook_entry(entry: dict) -> bool:
    """True for a Memotron lifecycle hook under either command spelling."""

    handlers = entry.get("hooks")
    if not isinstance(handlers, list):
        return False
    return any(isinstance(handler, dict) and list(handler.get("args") or [])[:1] == ["hook"] for handler in handlers)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply(workspace: Path) -> None:
    from memotron.adoption import (
        configure_project_llm,
        initialize_claude_code_project,
        project_llm_status,
    )

    section("1. Pointing the workspace at the isolated demo graph")
    patch_project_yaml(workspace)
    kv("graph_path", DEMO_GRAPH_PATH)
    assert_isolated(DEMO_GRAPH_PATH)
    kv("isolation", "OK - not the spymaster graph")
    DEMO_GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)

    section("2. Regenerating the always-loaded rule from demo agent_guidance")
    result = initialize_claude_code_project(project_root=workspace)
    for item in result["files"]:
        bullet(item)

    section("3. Rewiring MCP + hooks onto demo/bin/dw-demo")
    mcp_path, settings_path = rewire_to_demo_shim(workspace)
    bullet(f"{mcp_path}  -> deterministic dream agent, repo virtualenv")
    bullet(f"{settings_path}  -> same for SessionStart/PreCompact/PostCompact/SessionEnd")

    section("4. Sealing the tenant LLM credential")
    if not env_var_present(DEMO_LLM["api_key_env"]):
        fail(f"{DEMO_LLM['api_key_env']} is not set.\n  export it, or put it in the gitignored {REPO_ROOT / '.env'}")
    kv(f"${DEMO_LLM['api_key_env']}", "present (value never printed)")
    configure_project_llm(
        project_root=workspace,
        provider=DEMO_LLM["provider"],
        model=DEMO_LLM["model"],
        base_url=DEMO_LLM["base_url"],
        api_key_env=DEMO_LLM["api_key_env"],
    )
    status = project_llm_status(workspace)
    kv("provider / model", f"{status['provider']} / {status['model']}")
    kv("llm_ready", status["llm_ready"])

    section("5. Versioning the demo project-memory policy (the demo Motive)")
    # Building the platform already versions + activates whatever
    # .memotron.yaml declares (adoption.ensure_project_memory_config), and
    # step 1 wrote DEMO_PROJECT_MEMORY there.  Only version again if something
    # drifted, so re-running setup.sh does not churn policy versions.
    platform, _config = build_demo_platform()
    try:
        active = platform.project_memory_config()
        if active is not None and _matches_demo_policy(active):
            kv("policy version", f"{active.version} (already active, unchanged)")
        else:
            config = platform.configure_project_memory(
                **DEMO_PROJECT_MEMORY,
                configured_by="demo/demo_config.py",
            )
            kv("policy version", f"{config.version} (newly activated)")
        kv("publishing enabled", "yes — memory_publish / memory_promote available")
    finally:
        platform.client.graph.close()


def _matches_demo_policy(active) -> bool:
    """True when the ACTIVE project-memory policy already equals demo mode."""

    for key, want in DEMO_PROJECT_MEMORY.items():
        have = getattr(active, key)
        if key.endswith("memory_types"):
            have = [item.value for item in have]
        elif isinstance(have, tuple):
            have = list(have)
        if have != want:
            return False
    return True


# ---------------------------------------------------------------------------
# Show effective policy
# ---------------------------------------------------------------------------


def show(workspace: Path) -> None:
    platform, project_config = build_demo_platform()
    try:
        asyncio.run(_show(platform, project_config, workspace))
    finally:
        platform.client.graph.close()


def _embedding_identifier(client) -> str:
    from memotron.embedding import LocalEmbeddingTransport

    transport = client._embedding_transport or LocalEmbeddingTransport()
    return str(getattr(transport, "identifier", type(transport).__name__))


async def _show(platform, project_config, workspace: Path) -> None:
    from memotron.agent_memory import PROJECT_MEMORY_POLICY_MOTIVE

    banner(
        "MEMOTRON DEMO MODE — EFFECTIVE POLICY",
        f"tenant {DEMO_PROJECT_ID}   graph {DEMO_GRAPH_PATH}",
    )

    section("Identity and isolation")
    kv("project / tenant id", project_config.project.id)
    kv("mode", project_config.mode.value)
    kv("graph file", project_config.graph_path())
    kv("workspace", workspace)
    kv("project scope", platform.project_scope.key)
    kv("personal scope", platform.user_scope.key if platform.user_scope else "-")
    kv("continuity scope", platform.agent_scope(DEMO_AGENT_ID).key)

    section("LEVER 1 — agent_guidance (rendered into .claude/rules/memotron.md)")
    for title, key in (
        ("search_when", "search_when"),
        ("remember_when", "remember_when"),
        ("publish_when", "publish_when"),
        ("review_when", "review_when"),
        ("refresh_when", "refresh_when"),
    ):
        print(f"  {title}:")
        for item in getattr(project_config.agent_guidance, key):
            print(f"      - {item}")

    memory_config = platform.project_memory_config()
    if memory_config is None:
        fail("project memory is not configured; run: uv run demo/demo_config.py")

    section("LEVERS 2-4 — active project-memory policy")
    kv("policy version", memory_config.version)
    kv("configured_by", memory_config.configured_by)
    kv("allowed_memory_types", ", ".join(t.value for t in memory_config.allowed_memory_types))
    kv("protected_memory_types", ", ".join(t.value for t in memory_config.protected_memory_types))
    kv("min_salience", memory_config.min_salience)
    kv("max_memories_per_candidate", memory_config.max_memories_per_candidate)
    kv("dedup_threshold", memory_config.dedup_threshold)
    kv("min_endorsements", memory_config.min_endorsements)

    section("Resolved Motive actually governing project formation")
    policy = platform.client.resolve_policy(
        tenant_id=DEMO_PROJECT_ID,
        agent_id=DEMO_AGENT_ID,
        scope=platform.project_scope,
    )
    motive = policy.motive
    kv("motive name", motive.name if motive else "-")
    if motive is not None:
        kv("allowed types", ", ".join(t.value for t in motive.allowed_memory_types))
        kv("min_salience", motive.salience_rubric.min_salience)
        kv("max_memories_per_episode", motive.salience_rubric.max_memories_per_episode)
        kv("dedup_threshold", motive.dedup_threshold)
        assert motive.name == PROJECT_MEMORY_POLICY_MOTIVE
    kv("dream_mode", policy.dream_mode.name)
    kv("enabled jobs", ", ".join(k.value for k in policy.enabled_job_kinds))

    section("Supersession (packaged default — not tenant-overridable)")
    supersession = platform.client.config.supersession
    kv("corroboration_margin", supersession.corroboration_margin)
    kv("corroboration_required", supersession.corroboration_required)
    print(f"  note: {SUPERSESSION_NOTE}")

    section("Transports")
    kv("DEMO_EXTRACTION_MODE", DEMO_EXTRACTION_MODE)
    kv("extraction", type(platform.client._extractor._transport).__name__)
    kv(
        "dream agent",
        type(platform.client._dream_agent_transport).__name__
        if platform.client._dream_agent_transport
        else "LocalDreamAgentTransport (DreamEngine default; deterministic)",
    )
    kv("embedding", _embedding_identifier(platform.client))
    kv(
        "theme synthesis",
        type(platform.client.theme_synthesis_transport).__name__
        if platform.client.theme_synthesis_transport
        else "none (deterministic theme labels)",
    )

    section("What this buys the demo")
    table(
        ["gate", "effect on screen"],
        [
            ["type allowlist", "only decision / requirement / directive / theme can exist"],
            ["min_salience 0.25", "incidental extractions are dropped before materialization"],
            ["cap 2 per candidate", "one publish can never explode into many facts"],
            ["dedup 0.92", "restatement reinforces; distinct facts stay distinct"],
            ["publish_when", "the agent writes once, on confirmation, one fact at a time"],
            ["refresh_when", "the agent never dreams; the operator does, visibly"],
        ],
    )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--show",
        action="store_true",
        help="print the effective policy without changing anything",
    )
    args = parser.parse_args()

    load_repo_env()
    workspace = workspace_or_fail() if args.show else DEMO_WORKSPACE
    if not (workspace / ".memotron.yaml").is_file():
        fail(f"run demo/setup.sh first — no .memotron.yaml in {workspace}")

    if not args.show:
        banner("APPLYING MEMOTRON DEMO MODE", f"workspace {workspace}")
        apply(workspace)
    show(workspace)


if __name__ == "__main__":
    main()
