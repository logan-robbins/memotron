# Stage 2 — evolve the graph (same Claude Code session, terminal 1)

Six prompts that exercise the four things a memory system has to get right:
**recall**, **correction**, **reinforcement**, and **clustering**.

---

## 2.1 — Recall: the graph changed underneath a live session

Dream 1 ran in another process while this session stayed open. The agent has
not been told anything new — it has to go and look.

```
What do you currently know about checkout-api? Search memory and answer only from what's stored — no guessing.
```

Look for: `memory_search`, then an answer naming the PostgreSQL decision, the
200 ms p95 requirement, and the data-platform review rule. Those were prose
five minutes ago; they are typed graph facts now.

---

## 2.2 — Correction: current truth changes, history survives

```
Correction from yesterday's architecture review: we are NOT using PostgreSQL. checkout-api will use DynamoDB for the reservation ledger — single-digit-millisecond reads at peak won the argument. Record the change.
```

Expect one `memory_publish`:

```
Memory: subject=checkout-api; predicate=decided; object=use DynamoDB for the reservation ledger; relationship_type=DECIDES; confidence=0.9
```

This is a **supersession**, not an edit. Same truth slot
(`checkout-api : decided`), different object.

---

## 2.3 — Reinforcement: the same fact again is evidence, not a duplicate

```
Confirming again from today's SLO review, unchanged: checkout-api must keep reservation lookup p95 latency under 200 ms.
```

Expect one `memory_publish` with the **same** subject / predicate / object as
Stage 1. After Dream 2 this must not create a second row.

---

## 2.4 — Related rule #2 (cluster seed)

```
Second team rule in the same class: every reservation ledger migration needs a load test before it merges.
```

---

## 2.5 — Related rule #3 (cluster seed)

```
Third one, same class: every reservation ledger migration needs a documented rollback plan before it merges.
```

There are now three sibling procedural rules about reservation ledger
migrations. Consolidation has something real to theme.

---

## 2.6 — Recall again, before dreaming

```
Summarise everything you know about checkout-api right now, from memory only.
```

Look for: the agent still reports **PostgreSQL**, because the correction is
queued evidence and has not been dreamed yet. Say it out loud: *"the graph has
not lied to you — it just hasn't dreamed."*

---

## End of Stage 2 — what should be true

| Signal | Expected |
|---|---|
| `memory_publish` calls this stage | 4 |
| project episodes total | 7 |
| project facts in the graph | still 3 |

Switch to terminal 2 for `demo/prompts/dream2.md`.
