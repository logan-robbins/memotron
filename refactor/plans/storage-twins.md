# Audit — Unit 2: `storage` twin unification

**Verdict: NO ACTION on the twin thesis · 1 optional item (54 lines) · 0 defects found**
Commit audited: `7ec1fb0` · Audited: 2026-09-02 · Tier-C spend authorised: none

The unit's premise — that a query-seam normalisation unlocks a substantial mechanical
de-duplication between the SQLite and Postgres engines — **does not survive measurement at the
size claimed**, and the residual that does unify is the part least likely to harbour the defect
class the work was meant to prevent.

---

## Instrument readings

| Instrument | Command | Reading | Denominator | Control |
|---|---|---|---|---|
| parity coverage | `scripts/verify/parity_coverage.py` | PASS at baseline; sqlite-only **18**, postgres-only **7**, shared-plane unexercised **0** | **159** methods defined on both engines | ✅ synthesised a hermetic `coverage.json` (12 postgres files zeroed) → **exit 2, declined**, did not print ~170 false gaps. Restored |
| mixin dag | `scripts/verify/mixin_dag.py` | PASS; sqlite 7 modules / 1 accepted domain cycle, postgres 10 / 1 | edges and crossing members listed per package | (inherited from unit 1's run) |
| twin equality | own AST measure, staged | see below | **159** shared pairs — *independently matches `parity_coverage`'s 159* | staged normalisation shows each assumption's contribution separately |

> The first control I attempted was **invalid**: I passed `coverage.json` as a positional
> argument, argparse rejected it, and the resulting `exit 2` looked like the correct
> "declined to judge" answer. It was the right number for the wrong reason. The real control
> had to synthesise a hermetic report.

---

## The measurement

Staged normalisation, so it is visible which assumption does the work:

| Normalisation | Byte-equal pairs |
|---|---:|
| raw (whitespace only) | **0** |
| + placeholder dialect `?` ↔ `%s` | 0 |
| + query seam `self._connection` ↔ `self._engine` | 0 |
| + cursor idiom `.execute(…)` / `.fetchone(…)` | 0 |
| **+ sqlite's trailing `.fetchone()` dropped** | **9** |

The whole seam is one shape:

```python
sqlite:   self._connection.execute("SELECT … WHERE x = ?",  args).fetchone()
postgres: self._engine.fetchone(   "SELECT … WHERE x = %s", args)
```

**The 9, and 54 sqlite-side lines:** `_epochs.py` (4: `entity_alias_row`, `quarantine_counts`,
`quarantined_candidate`, `quarantined_candidate_payload`) · `_graph.py` (4: `get_node`,
`get_relationship`, `nodes`, `relationships`) · `_artifacts.py` (1:
`coherence_repair_monitor`).

**The seam does not extend.** 18 near-misses at 0.93–0.999. Bucketing their residual differing
lines: **63 uncategorised** against 6 datetime-handling, 4 `ON CONFLICT`, 1 json/jsonb. The
audit method's own rule — *"under six constructs the seam extends; if thirty, stop"* — says stop.

---

## Prior reconciliation

| Record | Subject | My blind pass found | Verdict | Re-run today |
|---|---|---|---|---|
| Unit-2 ordering rationale (`REFACTOR_OVERVIEW.md` §4) | *"normalising the query seam collapses **21** twin pairs to byte-equality, 12 of them in `_epochs`"* | **9** pairs, 4 in `_epochs`, under a normalisation matching every step the record describes | **CONTRADICTS** | Could not re-run the record's own command — its normaliser was not preserved. Mine is reproducible and reports its stages; the record's was described as *"an upper bound… deliberately conflated `execute`/`fetchone`"* |
| `storage/_shared/` | is the twin harvest finished? | **Yes at its declared bar** — 0 pairs are byte-equal without dialect normalisation | **CONFIRMS** | `parity_coverage` still reports shared-plane unexercised **0 of 13** |

The record labelled its own figure an upper bound. It was one. **The honest number is 9, and
the difference matters** — 21 pairs would have been a plausible unit of work; 9 methods and 54
lines is not.

---

## Why NO ACTION — the falsifiers

**1. Yield is 0.4%.** 54 sqlite-side lines against **14,253** lines across the two engines.

**2. The methods that unify are not the methods that diverge.** All 9 are simple row-fetchers.
The defect class that motivated this work — the `anchor` half-life divergence, where Postgres
decayed anchor memories 4× faster than SQLite under a 3,420-line parity suite that passed —
lived in **`_operational.py`**. **Zero** of the 9 are in `_operational.py`. Mechanically
removable duplication and bug-bearing duplication are disjoint sets here.

**3. Unifying would weaken the gate that catches this defect class.** `parity_coverage`'s
population is *methods defined in both engines*. Every unified twin **leaves** that population:
159 → 150. So the change spends the gate's coverage to buy 54 lines, in exchange for removing
the nine cases least likely to need it. This is the audit method's planted-defect falsifier in
substance: the unified form would catch **less** than the duplicated form.

---

## Optional item, if you want it anyway

| id | Claim | `N_now` | `N_after` | Falsifier + source | Risk |
|---|---|---|---|---|---|
| **ST-1** | A `self._q(sql, args)` helper on each engine makes 9 twins byte-identical, extractable to `_shared/` | 159 twin pairs · 54 duplicated lines | 150 twin pairs · 0 | Sign: `parity_coverage` population drop must be recorded per commit or the gate silently weakens | **Medium** — touches read paths on both engines |

**Not probed.** Under this audit's own rules an admitted item needs a measured `N_after`, and I
did not probe ST-1 because the falsifiers above say it should not be admitted. If you want it,
it needs a probe first. `pure_move.py` **cannot** verify it — bodies genuinely change;
`parity_postgres.py` plus the storage and receipt goldens are the proof.

---

## Defects quarantined

None found.

---

## Revisit preconditions — checkable, not felt

1. **A cross-engine divergence is found in one of the 9** (`_epochs`/`_graph`/`_artifacts`
   fetchers). That would refute falsifier 2 directly.
2. **`parity_coverage` sqlite-only or postgres-only rises above its baseline of 18 / 7** —
   evidence the engines are drifting where tests do not look.
3. **The near-miss residual becomes categorisable** — if a future change collapses the 63
   uncategorised lines into under six constructs, the seam extends and the yield changes.
4. **A `_q` seam is introduced for an unrelated reason** (e.g. connection pooling), making the
   9 free.

If nobody can produce one of these, the prior stands.

---

## Where this is wrong

The claim most worth attacking is falsifier 3 — that shrinking `parity_coverage`'s population
is a real cost. It could be argued the opposite way: a unified method *cannot* diverge, so
removing it from a divergence-detector's population loses nothing, and the gate's population
shrinking is a sign of success rather than erosion. I find that argument reasonable and did not
take it, because `_shared/`'s own docstring already records the same tension and the repo chose
to close the hole rather than accept it. **If you accept that counter-argument, falsifier 3
falls and the verdict rests on falsifiers 1 and 2 alone — which still say 0.4% and the wrong
nine methods.** What would change the verdict outright: a probe showing the `_q` seam also
collapses a meaningful share of the 18 near-misses, which my construct bucketing says it will
not.
