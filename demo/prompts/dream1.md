# Dream 1 — first dreaming session (terminal 2)

Everything Stage 1 produced is still **raw evidence**. Nothing is truth yet.
That gap is the product: Memotron does not write to the memory graph at
conversation speed.

Run these three commands in the repository root, in order.

---

## 1 — Snapshot the "before"

```
uv run demo/inspect.py --save before-dream-1
```

Point at:

- `pending_episode_count: 3` — three published candidates are queued
- `context_visible_relationship_count: 0` — the graph is still empty
- the "Active facts in context" table says `(none)`

---

## 2 — Dream

```
uv run demo/dream.py --stage 1
```

Point at:

- the **FORMATION** row: `epis 3`, `new 3` — three episodes in, three typed
  facts out
- the **CONSOLIDATION** row: no theme yet — three unrelated facts do not
  cluster, and Memotron does not invent one
- the dream-agent decisions table: deterministic `LocalDreamAgentTransport`
  approvals, one auditable row per maintenance action

---

## 3 — Snapshot the "after" and show the delta

```
uv run demo/inspect.py --diff before-dream-1 --save after-dream-1
```

Point at:

- three `+ NEW` rows in the delta table — one `decision`, one `requirement`,
  one `directive`, each with `conf 0.90` and `obs 1`
- `context_visible_relationship_count  0 -> 3`
- `compression_ratio  1.000` — one episode in, one fact out. **No compression
  yet**, and `tokens_saved_vs_raw` is still `0`: at three episodes the rendered
  profile (~199 tokens) costs slightly *more* than replaying the raw episodes
  (113 tokens), because the profile carries headers and typed grouping.

Do not skip that last point — say it out loud. *"Memory does not pay for
itself at three facts. Watch this number after the second dream."* The
crossover is the demo; claiming a win here would be the fake version.

---

Now go back to terminal 1 (same session, still open) and paste
`demo/prompts/stage2.md`.
