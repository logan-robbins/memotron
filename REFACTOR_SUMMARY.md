# Refactor summary

One section per completed unit. Ordering, method and status live in `REFACTOR_OVERVIEW.md`.

---

## Unit 1 — Structural zero (2026-09-02)

**Commits:** `c2df472` (the change) · `7ec1fb0` (the re-baseline)
**Audit:** `refactor/plans/structural-zero.md`
**Outcome:** SZ-1 done · SZ-2 deferred here, then **DONE** in the addendum below (`fa02fb6`) · 0 defects found

### What changed and why

Three import retargets, one line each. `storage/base.py:157` was infra importing its own compat
shim (`memotron.receipts`) instead of the module that shim forwards to. `erasure.py:43` and
`replay.py:32` each imported the *package facade* as `from memotron import receipts`, and
those two lines were the only thing making the 8-node cycle a cycle.

The point was not the three lines. It was making `coupling_report.py` usable as a guard for
units 2–10: a gate passing at a non-zero baseline cannot distinguish *the cycle we accepted*
from *the cycle unit 4 introduced*.

### Before / after

| Metric | Before | After |
|---|---:|---:|
| import cycles | 3 | **1** |
| modules in a cycle | 12 | **2** |
| largest cycle | 8 | **2** |
| upward tier edges | 1 | **0** *(1 at the time of this unit; SZ-2 took it to 0 — see addendum)* |
| tests | 1,391 | 1,391 (1,389 passed · 1 skipped · 1 xfailed) |
| `check.sh` | 16/16 PASS | 16/16 PASS |
| api_surface golden | 9,628 lines | 9,628 lines, **unchanged** |
| coverage floors / mypy suppressions / receipt & storage goldens | — | **all unchanged** |

### The result worth keeping

**The ratchet is real, and it was proved rather than asserted.** Reintroducing the
`storage/base.py` shim import now reports `import cycles: 1 -> 2 WORSE` and **exits 1**. Against
the old baseline of 3, that identical regression reported `3 -> 3` and **passed**. Every later
unit is now guarded against silently reintroducing what this one removed.

### Approved behaviour changes

None. No public API, data format, CLI interface or error semantic changed. `api_surface.py`
reports the importable surface byte-identical.

### Cross-subsystem changes

None. All three files are inside unit 1's boundary.

### Deferred

* ~~**SZ-2 — the upward tier edge**~~ **— NO LONGER DEFERRED. Done in `fa02fb6`; see the
  *Unit 1 addendum — SZ-2* below.** Left in place because the reasoning still explains the
  cost that was weighed, but do not read this bullet as open work.

  It was deferred because relocating `storage/settings.py` (83 lines, imports only `typing`
  and `pydantic`) into core **costs 28 api_surface golden rows** — `DreamConfig`'s structural
  hash changes everywhere it is re-exported, since it carries a `storage: StorageSettings`
  field whose module path feeds the fingerprint. That cost was accepted and paid. Upward tier
  edges are **0** — re-measured today with `scripts/verify/coupling_report.py`, which reports
  `upward tier edges 0` and `COUPLING: PASS`, so the ratchet has held through a week of
  unrelated work.

  The alternative that was rejected — reclassifying the module in `TIERS` — would have been
  free and would have been metric management rather than a fix. That distinction is the
  reusable part of this unit.
* **The `config↔prompts` cycle.** Survives deliberately: `config/_prompts.py:3-4` is an in-code
  decision record calling its lazy import *"load-bearing and stays function-local"*. Removing it
  requires relocating `DreamPromptProfile`, which is a design change against that record's
  scope. Carried to **unit 7**, to be argued there.

### Lessons for later units

1. **A probe that skips a lane the commit must pass is an incomplete probe.** Mine ran `pytest`
   and `api_surface` but not `ruff`; `lint` failed on first execution (`I001`). Cheap to fix,
   but it should have been caught in the probe.
2. **Design the control against what the metric actually counts.** My first control added a
   second `config → storage` import and the reading did not move — which looks like a broken
   gate. It counts distinct *subsystem pairs*, not import sites. Had I trusted it, I would have
   reported a working instrument as broken.
3. **Probe the combination, not just the parts.** SZ-2 applied alone *breaks the package
   import* (`cannot import name 'ReceiptLedger' from partially initialized module`) because it
   changes init order and surfaces the cycle SZ-1 removes. Ordering constraints between items
   are only visible when both are probed.

---

## Unit 2 — `storage` twin unification (2026-09-02)

**Commits:** none · **Audit:** `refactor/plans/storage-twins.md` · **Outcome:** NO ACTION

The unit's premise did not survive measurement. Independently re-measured with a staged
normaliser: the query seam collapses **9** twin pairs to byte-equality (54 sqlite-side lines),
not the **21** the ordering rationale claimed. Extraction found 159 shared pairs, independently
matching `parity_coverage`'s 159.

Three falsifiers, any one sufficient:

1. **Yield 0.4%** — 54 lines against 14,253 across both engines.
2. **The methods that unify are not the ones that diverge.** All 9 are simple row-fetchers. The
   `anchor` half-life divergence lived in `_operational.py`; **zero** of the 9 are there.
3. **Unifying shrinks `parity_coverage`'s population** 159 → 150, spending the gate that catches
   this defect class to buy 54 lines.

The seam does not extend: 18 near-misses, but **63 uncategorised** residual lines against 11 in
known dialect buckets.

Deferred as **ST-1** (deliberately unprobed) with four checkable revisit preconditions.

---

## Unit 3 — the #150 composition pilot (2026-09-02)

**Commits:** none · **Audit:** `refactor/plans/150-composition-pilot.md` · **Outcome:** NO ACTION on the pilot as specified

Falsifier declared before measuring, sourced from committed baselines: *retires zero suppression
rows and deletes zero `_protocol.py` lines → cosmetic.* **It fired — 0 of 1,219 protocol lines,
1 of 93 suppression rows.**

`_protocol.py` is **per-package, not per-plane** — it declares the shared kernel that replaced a
blanket `disable_error_code` silencing 200 `attr-defined` errors. 7 of 8 sqlite mixins import it,
so converting one plane leaves it intact.

The partition test (the one measurement that could have overturned this) was run: **15 of 22
members are true kernel, 6 are single-consumer** — owned by `_epochs`, `_governance`, `_graph`
and `__init__`. **None by `_policy`.** So a single-plane pilot *can* delete lines, just not this
plane's.

**#150's premise stands and is growing** — `_protocol.py` 1,116 → **1,219**, suppressions
89 → **93**, mixin share 78/89 → **74/93** (80%). What does not stand is that `_policy` can
demonstrate anything about it.

**Tier-C authorisation requested:** a whole-package conversion of `storage/sqlite` (7 planes,
5 crossing members, known payoff 85 lines + 5 rows) to yield a cost-per-crossing-member figure —
the number that makes `dreaming`'s 63 projectable. Recommended *against* piloting on `dreaming`
directly: largest payoff, but 63 crossing members with no cost model is the shape that produced
#119.

**Consequence:** units 4–6 were gated on this verdict and no longer are. They should audit
everything *except* the mixin idiom, and say so explicitly.

---

## Unit 1 addendum — SZ-2 (2026-09-02)

**Commit:** `fa02fb6` · **Outcome:** the last upward tier edge removed

`storage/settings.py` (83 lines, importing only `typing` and `pydantic`) was configuration
vocabulary filed under infra, so core `config` reached upward for it. Definitions moved to
`config/_storage_settings.py`; `storage/settings.py` remains a re-export shim because
`memotron.storage.settings` is a public path.

| Metric | Before | After |
|---|---:|---:|
| upward tier edges | 1 | **0** |
| import cycles | 1 | 1 *(unit 7)* |
| `check.sh` | 16/16 | 16/16 |

**Both coupling ratchets now hold**, each proved by control: a reintroduced core→infra import
reports `0 → 1 WORSE` and exits 1.

### The estimate was wrong, and the reason generalises

I quoted **28 golden rows** throughout. The real cost was **66 replaced + 3 new**, plus a
coverage-floor entry. Two distinct errors: the 28 came from counting diff lines with a different
method, and my probe **never ran `cov-floor`** — so it could not have seen that a new module
needs a floor. This is SZ-1's incomplete-probe lesson in a second form: there, the probe skipped
`lint`; here it skipped `cov-floor`. **The rule stands and is now twice-earned: a probe runs
every lane the commit will face.**

The open question that had deferred SZ-2 was answered before taking it: `DreamConfig`'s
structural hash appears **only** in `tests/api_surface.golden.txt` — no cache key, no artifact,
no stored contract version. "Only a re-bless" was checked, not assumed.

### Noted, not fixed

`detect-secrets --all-files` drops a baseline entry for `tests/test_embedding_transport.py` — a
file **no commit in this branch touches**. Pre-existing baseline drift; left alone under the
defect-quarantine rule rather than ridden inside a refactor. The commit-time hook path is clean.

---

## Unit 3 addendum — two Tier-C conversions, and #150 closed (2026-09-02)

**Commits:** none (both conversions reverted) · **Outcome:** #150 closed as a decision record

**Conversion 1 — `storage/sqlite/_policy`: clean.** mypy clean, 1,388 tests pass. **Net +81 lines
for 10 public methods = 8.1 lines per delegated member.** Delegations must repeat every signature;
untyped forwarding forfeits the checking the Protocol exists to provide.

**Conversion 2 — `dreaming/_decisions`: failed.** Chosen as the package median, not for ease.
mypy clean, line count net **−63** (a nominal win — `dreaming`'s Protocol declares per-member
signatures, unlike storage's shared kernel), **and 259 tests failed**:

```
AttributeError: 'DreamEngine' object has no attribute '_secret_reference_metadata'
```

`client/` reaches `dreaming`'s plane **privates** through the composed engine from 10 files. So
"0 public methods" was not "no delegation surface" — the surface is *members reached from outside
the plane*, and here they are private. True surface **36, not 20**.

| package | true surface | cost | benefit | verdict |
|---|---:|---:|---:|---|
| `storage/postgres` | 189 | 1,531 | 66 | 23 : 1 against |
| `storage/sqlite` | 164 | 1,328 | 85 | 16 : 1 against |
| `client` | 109 | 883 | 301 | 2.9 : 1 against |
| `agent_memory` | 50 | 405 | 131 | 3.1 : 1 against |
| `dreaming` | 36 | 292 | 533 | 0.55 : 1 *nominally for* |

### Two findings beyond the verdict

1. **mypy passed while the package could not import** — a Protocol↔plane circular import. The
   type checker cleared a change that does not run.
2. **X11: `client/` depends on `DreamEngine`'s private surface from 10 files.** A layering defect
   independent of #150, and the direct cause of conversion 2's failure. Unfiled.
