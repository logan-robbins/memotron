# Dream 2 — second dreaming session (terminal 2)

This is the payoff. One dream turns four new candidates into a corrected,
reinforced, consolidated graph — and the fact count goes **down**, not up.

---

## 1 — Dream

```
uv run demo/dream.py --stage 2
```

Point at, in the job table:

- **FORMATION**: `new 3` (the DynamoDB decision + two new rules),
  `reinf 1` (the restated p95 requirement), `supers 1` (PostgreSQL retired)
- **CONSOLIDATION**: a theme is synthesized and its members are demoted
- **PRUNING**: no growth backstop needed at this size — it stays quiet, which
  is the correct behaviour

---

## 2 — Show the delta

```
uv run demo/inspect.py --diff after-dream-1 --save after-dream-2
```

Walk the four beats in the delta table, in this order:

**Supersession.** A row flips `active -> superseded` with a
`(superseded by <id>)` pointer, and a `+ NEW` decision appears. Current truth
is DynamoDB; the PostgreSQL fact is not deleted, not edited — it is closed out
with a successor pointer, and the "Superseded / historical truth" section
still shows it.

**Reinforcement.** One row shows `~ obs 1->2` with the note
*"reinforced — no duplicate row"*, and its confidence rises. The same
requirement, stated twice, is one fact with two observations. The graph did
not grow.

**Clustering.** Three `~ DEMOTED` rows, each *"absorbed by theme &lt;id&gt;"*, and
one `+ NEW` row of type `theme`. In the "Themes" section the theme prints with
its three members indented underneath, each marked
`[demoted from context]` — still searchable, still evidence, just out of the
default context render.

**The bill.** In the proof deltas:

- `context_visible_relationship_count  3 -> 3` — seven episodes have been
  learned and the number the next session pays for did **not move**
- `demoted_relationship_count  0 -> 3`
- `theme_relationship_count    0 -> 1`
- `semantic_dedup_rate  0.000 -> 0.125` — the first observation that
  reinforced instead of creating
- `compression_ratio  1.000 -> 2.333` — episodes grew, context did not
- `tokens_saved_by_demotion` and `tokens_saved_vs_raw` both go from `0` to
  roughly `65-70`. **This is the crossover.** At Dream 1 memory cost more than
  it saved; one dream later the same graph renders about 26% cheaper than
  replaying raw episodes, and the gap widens with every session.

---

## 3 — Optional closers

Back in the Claude Code session:

```
What do you know about checkout-api now? Answer only from memory, and tell me if anything changed since you last looked.
```

The agent now says **DynamoDB**, and can explain why.

```
/memotron-memory why checkout-api uses DynamoDB
```

Runs `memory_search` then `memory_explain` — current status, confidence, valid
time, source episodes, supersession lineage.

Then close the session (`/exit`) and point out that the `SessionEnd` hook runs
the same dreaming path automatically — the operator-triggered
`demo/dream.py` was only so both dreaming moments were visible on screen.

---

## Reset for the next run-through

```
demo/setup.sh --reset
```

Wipes the demo graph, workspace, and snapshots, then rebuilds a clean READY
state. Runs in a few seconds.
