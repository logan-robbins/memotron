---
name: memotron-memory
description: Inspect and manage Memotron memory. Use when the user asks what is remembered, why it is remembered, to forget something, or to review project memory.
---

# Memotron memory

This repository uses Memotron `simple` mode.
Personal facts go to `memory_remember`; repository facts go through `memory_publish`.

Always use the `memotron_agent_memory` MCP server.
If lifecycle hooks have not run, call `memory_bootstrap` as `claude-code` / `Claude Code`.
Keep returned use events and report outcomes only after a real evaluator observes a result.
Use event-driven writes, not periodic or per-turn writes; zero new memories is valid.
At task milestones, review confirmed durable facts and publish one coherent sourced project event at a time.
Publish only sourced facts useful in a later repository session.
Never publish secrets, transient command output, or model speculation.

For `/memotron-memory status`, call `memory_contract`, `project_memory_config`, and `memory_evolution`.
For `/memotron-memory why <fact>`, use `memory_search`, then `memory_explain` on the selected relationship.
For `/memotron-memory forget <fact>`, search first, ask for confirmation, then call `memory_forget` with the exact relationship and scope.
`memory_forget` cannot retire project memory; use the governed operator workflow.
For `/memotron-memory review`, call `project_memory_candidates` and `project_memory_config`; do not invent approval.
