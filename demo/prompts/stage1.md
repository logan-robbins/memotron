# Stage 1 — establish the baseline (Claude Code session, terminal 1)

Five prompts. **Four** of them should produce exactly **three** project-memory
candidates; the fifth should produce **none**, on purpose.

Paste them one at a time and let each finish. Do **not** exit the session
between Stage 1 and Stage 2 — the `SessionEnd` hook runs due dreams, and this
demo wants the two dreaming moments under your control.

---

## 1.1 — Prove the graph is empty and the tools are live

The agent should call `memory_search` (or read the SessionStart injection) and
honestly report that it knows nothing yet. **Zero writes.**

```
You're joining the checkout-api project. Before we do anything else: what do you already know about this service? Answer only from memory, and tell me plainly if the answer is "nothing".
```

Look for: an `mcp__memotron_agent_memory__memory_search` tool call, an empty
result, and no publish. On screen, point at the SessionStart banner —
"Memotron simple memory loaded for Memotron Demo" — and the task-run id.

---

## 1.2 — A confirmed architectural DECISION

```
The ledger review just closed and the decision is final: checkout-api will use PostgreSQL for the reservation ledger. We need multi-row transactional guarantees on seat holds. Record that decision.
```

Expect the agent to call `memory_publish` **once**, with a single line shaped
like:

```
Memory: subject=checkout-api; predicate=decided; object=use PostgreSQL for the reservation ledger; relationship_type=DECIDES; confidence=0.9
```

Look for: exactly one publish, and the tool result saying
`queued_for_dreaming: true`. Nothing is in the graph yet — that is the point.

---

## 1.3 — A confirmed REQUIREMENT

```
Confirmed SLO from the platform review: checkout-api must keep reservation lookup p95 latency under 200 ms. That's a hard requirement, not a target.
```

Expect one `memory_publish` with `relationship_type=REQUIRES` and
`object=reservation lookup p95 latency under 200 ms`.

---

## 1.4 — A procedural DIRECTIVE

```
New team rule, effective immediately: every reservation ledger migration needs a data-platform review before it merges.
```

Expect one `memory_publish` with `relationship_type=SHOULD` and
`object=require a data-platform review on every reservation ledger migration`.

---

## 1.5 — The control: nothing was confirmed, so nothing is remembered

This is the most under-rated beat in the demo. The agent should answer
normally and write **nothing**, because the operator never confirmed anything.

```
I'm thinking out loud here, nothing is decided — what connection pool size would you start with for that Postgres instance, and why?
```

Look for: a useful answer, and **no** `memory_publish` call. Say the line out
loud: *"zero writes is the correct answer most of the time — that is what
`publish_when` in `.memotron.yaml` is enforcing."*

---

## End of Stage 1 — what should be true

| Signal | Expected |
|---|---|
| `memory_publish` calls | 3 |
| project episodes queued | 3 |
| project facts in the graph | **0** |

Leave the session open and switch to terminal 2 for `demo/prompts/dream1.md`.
