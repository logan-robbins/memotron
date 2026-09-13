# Audit — Unit 3: the #150 object-composition pilot

**Verdict: NO ACTION on the pilot as specified · 0 items admitted · 0 defects found**
Commit audited: `7ec1fb0` · Audited: 2026-09-02 · Tier-C spend authorised: none (and one is now requested — see below)

This unit is a **decision step**. Its output was to govern whether mixin work in units 4–6
(`dreaming`, `client`, `agent_memory`) is worth planning. **It cannot produce that verdict as
scoped**, and the reason is structural rather than a matter of degree.

**Falsifier, declared before measuring** (source: existing committed baselines —
`tests/mypy_suppressions.baseline.tsv` and the `_protocol.py` line counts, not a number I chose):
*the pilot retires zero suppression rows and deletes zero `_protocol.py` lines → cosmetic.*

**It fires. 0 of 1,219 protocol lines. 1 of 93 suppression rows.**

---

## Instrument readings

| Instrument | Reading | Denominator | Control |
|---|---|---|---|
| `mypy_suppressions.py` | PASS, **186 errors hidden**, 86 codes across 57 modules | 93 baseline rows | ✅ run under `NO_COLOR=1 TERM=dumb`, **error count non-zero** — the ANSI trap that once made 237 errors parse as 0 is not firing |
| `_protocol.py` sizes | 1,219 lines total | 6 files, enumerated below | — |
| `mixin_dag.py` | PASS, all packages | modules / edges / crossing members per package | (inherited, unit 1) |

### The cost, per package

| Package | `_protocol.py` | Suppression rows | Mixin modules | Crossing members |
|---|---:|---:|---:|---:|
| `dreaming` | **533** (44%) | 24 | 15 | **63** |
| `client` | **301** (25%) | 25 | 14 | 19 |
| `agent_memory` | 131 | 10 | 16 | 24 |
| `storage/sqlite` | 85 | 5 | 7 | **5** |
| `storage/postgres` | 66 | 10 | 10 | **4** |
| `storage/_shared` | 103 | — | 5 | 0 |
| **total** | **1,219** | **74 of 93** on mixin packages | | |

#150's headline of *"78 of 89 suppression pairs"* re-measures as **74 of 93** — 80%, not 88%.
Directionally intact; the exact figure moved.

---

## Why the pilot cannot measure its own metric

**`_protocol.py` is per-package, not per-plane.** It declares *what the composed backend
provides to its mixins* — the shared kernel — and its own docstring says why it exists:

> *"The cost was 200 `attr-defined` errors across the seven sqlite mixins, silenced by a blanket
> `disable_error_code` — which also silenced any REAL attribute error in 5,700 lines of storage
> code. This Protocol replaces that blanket with a declaration, so a typo in a helper name is
> caught again."*

Three measured consequences:

1. **Neither engine's `_protocol.py` mentions `policy` at all** (0 references in both). The file
   describes the kernel, not the planes.
2. **7 of 8 sqlite mixins import `_protocol`.** Converting `_policy` alone leaves six others
   depending on it, so the 85 lines survive **entirely**. The benefit is only realisable when the
   *last* plane in a package converts.
3. **`_policy` carries 1 of 93 suppression rows** (`postgres._policy  no-any-return  1`).

So a one-plane pilot deletes nothing and retires nothing, **by construction**. Running it would
produce a number that cannot be extrapolated to the metric the issue is about.

### And the plane chosen is the least informative one

`_policy` sits in the two packages holding **12%** of the protocol cost, **16%** of the
suppressions, and **8%** of the crossing members. `dreaming` alone has **63** crossing members
against `storage/sqlite`'s **5** — a 12× difference in the coupling composition would have to
replace. A conversion that goes smoothly at 5 says little about 63.

---

## Steelman, and what it leaves standing

The fair reading of #150 is that the pilot measures the **cost** side — delegation lines,
constructor wiring, call-site indirection — which you then multiply by the plane count. That is
legitimate, and it is not what I have refuted.

But it changes what the pilot is for, because **the benefit side needs no pilot at all — it is
already measured above.** Converting a whole package recovers exactly its `_protocol.py` lines
plus its suppression rows:

| If you converted… | Recovers | Planes to convert | Crossing members to replace |
|---|---:|---:|---:|
| `storage/sqlite` | 85 lines + 5 rows | 7 | 5 |
| `storage/postgres` | 66 lines + 10 rows | 10 | 4 |
| `client` | 301 lines + 25 rows | 14 | 19 |
| `dreaming` | **533 lines + 24 rows** | 15 | **63** |

The cheapest conversion has the smallest payoff; the largest payoff has 12× the coupling. That
tension is the actual decision in #150, and it is visible **without running any pilot**.

---

## Prior reconciliation

| Record | Subject | My blind pass found | Verdict | Its own number, re-run |
|---|---|---|---|---|
| **#150** | *"pilot object composition on `_policy`, the only plane with zero cross-mixin edges in BOTH engines"* | The zero-edge claim is true **as `mixin_dag` measures it** (it excludes composer calls), but `_policy` reaches **6** kernel members in sqlite and **1** in postgres. And the plane contributes 0 protocol lines / 1 suppression row | **CONTRADICTS** the pilot's *design*, not its premise | `_protocol.py` re-measured **1,116 → 1,219** (grew, `storage/_shared` added a sixth); suppressions **89 → 93**, mixin share **78/89 → 74/93** |

**#150's premise stands: the idiom does cost ~1,219 lines and 80% of the type debt.** What does
not stand is that `_policy` can demonstrate anything about it.

---

## What I am NOT concluding

**This is not "the idiom stays."** It is "this experiment cannot decide." The question #150 asks
is still open and still worth answering — the cost is real and *growing* (it rose 103 lines and
5 suppressions when `storage/_shared` landed, which was good work).

---

## Tier-C authorisation requested

The minimum experiment that could actually answer #150 is a **whole-package conversion**, which
exceeds this audit's Tier-B budget (≤1 hour, discarded probe) and therefore needs your approval
in advance, per the method.

**Recommended target: `storage/sqlite`** — 7 planes, only 5 crossing members, and a known
payoff of 85 lines + 5 suppression rows. It is the cheapest package to convert and would yield a
measured **cost-per-crossing-member**, which is the number that makes `dreaming`'s 63 projectable.

**Recommended against: piloting on `dreaming` directly.** Largest payoff, but 63 crossing members
with no cost model yet — that is the shape of work that produces a second #119.

---

## Consequence for units 4–6

They were gated on this verdict and no longer are. **Recommendation: units 4–6 audit everything
*except* the mixin idiom, and leave the composition question to the Tier-C experiment.** Their
plans should say so explicitly rather than silently omitting it.

---

## Revisit preconditions

1. The Tier-C `storage/sqlite` conversion is run and yields a cost-per-crossing-member figure.
2. `_protocol.py` total exceeds ~1,400 lines, or the mixin share of suppressions exceeds 85% —
   either would mean the cost is accelerating rather than merely growing.
3. A package's planes drop below ~3 crossing members, making a whole-package conversion cheap
   enough to do without a cost model.

---

## The partition test — run, because it was the one thing that could overturn this

I flagged one cheap measurement that would refute the verdict: *do the Protocol's members
partition by consumer, or are they a single shared kernel?* Measured on
`storage/sqlite/_protocol.py`, **22 declared members**, counting consumers among the 7 sibling
mixins:

| Consumers | Members |
|---|---:|
| 8 / 7 / 6 / 5 / 3 | 6 |
| 2 | 9 |
| **exactly 1** | **6** |
| 0 (dead) | 0 |

So it is **15/22 true kernel, 6/22 partitionable** — my "0 lines, by construction" was too
absolute as a general statement. A single-plane conversion *can* delete the members its plane
uniquely owns.

**But not for `_policy`.** The six single-consumer members are owned by `_epochs`
(`_ENTITY_ALIAS_STATUSES`, `reveal`), `_governance` (`_normalize_llm_provider`,
`_normalize_tenant_id`), `_graph` (`_optional_datetime_from_text`) and `__init__` (`receipts`).
**`_policy` owns none of them.** The verdict holds, and now for a measured reason rather than an
inferred one.

Two corrections this produces for the Tier-C experiment: a whole-package conversion recovers all
85 lines, but a *partial* one recovers only the converting planes' uniquely-owned members — so
`_epochs` (2 members) or `_governance` (2) would be strictly more informative pilots than
`_policy` (0), if a single-plane pilot is wanted after all.

## Where this is wrong

With the partition test run, the remaining soft spot is the **cost** side, which I have not
measured at all — no probe was taken, because the falsifier rejected the item before Stage C.
If conversion turns out to cost *less* per plane than the delegation-plus-wiring overhead I am
implicitly assuming, then even an 85-line payoff could be worth taking, and "cheapest package
has smallest payoff" stops being an argument against. That number only comes from the Tier-C
run, which is why it is requested rather than declined.

---

# Tier-C result (authorised and run, 2026-09-02)

**A whole-plane conversion was performed for real, verified, measured, and reverted.**
`_policy` was converted from mixin to object composition on `storage/sqlite`: the plane became a
class taking an explicit kernel reference, and the backend gained typed delegations preserving
every signature (`*args/**kwargs` is not available — this is a strict-tier surface and untyped
delegation would forfeit the checking the Protocol exists to provide).

**It works.** `mypy` clean on 9 source files, backend instantiates, delegated methods callable,
**1,388 tests pass** — the only failure is the api_surface golden, expected because the class
structure changed.

## The measured rate

| | |
|---|---:|
| public methods delegated | 10 |
| delegation lines generated from real signatures | 76 |
| plane change (kernel indirection + constructor) | +6 |
| **net cost, one plane** | **+81 lines** |
| **rate** | **8.1 lines per public method** |
| protocol lines recovered | **0** (per-package; recovers only when the last plane converts) |

## Projection — and it inverts the recommendation

Cost driver is **public surface**, not crossing members. Crossing members size the kernel
interface (a small constant); delegations dominate. Public-method counts are exact:

| package | public methods | cost (+lines) | benefit (protocol) | suppressions | ratio |
|---|---:|---:|---:|---:|---:|
| `storage/postgres` | 174 | 1,409 | 66 | 10 | **21.3 : 1 against** |
| `storage/sqlite` | 144 | 1,166 | 85 | 5 | **13.7 : 1 against** |
| `agent_memory` | 47 | 381 | 131 | 10 | 2.9 : 1 against |
| `client` | 97 | 786 | 301 | 25 | 2.6 : 1 against |
| **`dreaming`** | **20** | **162** | **533** | 24 | **0.3 : 1 — FOR** |

**`dreaming` is the only package where conversion pays** — 162 lines added to delete 533, a 3.3×
net win. Every other package loses, and the two I recommended piloting on lose worst.

**My recommendation was backwards.** I proposed `storage/sqlite` (cheapest, 5 crossing members)
and advised *against* `dreaming` (63 crossing members). Crossing members were the wrong driver.
`dreaming` is an orchestrator — 20 public methods against 133 private, verified two ways
(AST count, and `DreamEngine`'s 15 runtime public attributes). Storage backends are *contracts*,
so their surface is inverted: 144 and 174 public methods.

## What this does NOT settle — read before acting

The rate was measured on `_policy`, which reaches **6** kernel members. `dreaming`'s planes cross
**63**. Cost has two components and I measured only one:

* **delegation cost** — scales with public surface, measured, projects cleanly.
* **kernel-plumbing cost** — scales with crossing members, measured only at 6, and **63 is 10×
  outside the measured range.**

So `dreaming` is now the *only* candidate worth considering, but the experiment did not measure
the component that would decide it. Under composition its 63 cross-mixin private calls must go
somewhere: either a kernel that grows to absorb them, or planes holding references to each other
— which is mixins with extra steps.

**Recommendation:** do not act on the 0.3:1 figure yet. If #150 is pursued, the next experiment
is converting **one `dreaming` plane** to measure kernel-plumbing cost at realistic crossing-member
density. That is a second Tier-C run, and it should be authorised on its own terms rather than
folded into this result.

**Settled by this run:** `storage/sqlite`, `storage/postgres`, `agent_memory` and `client` are
all net-negative and should not be converted. That is four of five packages closed with numbers.

---

# Tier-C run 2 — the `dreaming` plane (2026-09-02)

**Result: the conversion FAILED, and the failure is the finding.**

`_decisions` was chosen deliberately, not for ease: outward reach **8** (the package median;
`_policy` was 6), **16 inbound calls from 6 sibling planes**, 745 lines, and **0 public methods**
— so delegation cost would be zero and everything measured would be pure plumbing.

## What happened, in order

1. **Mechanical conversion applied**: plane takes an explicit kernel, 8 outward reaches rewritten,
   **23 sibling call sites** rewritten across 6 files, wired into the composer.
2. **mypy: clean.** 17 source files, no issues.
3. **Runtime: circular import.** `_protocol` imports the plane, the plane imports the Protocol.
   Fixed under `TYPE_CHECKING` (+3 lines). **Note mypy passed while the package could not
   import** — the type checker cleared a change that does not run.
4. **Line count looked like a win: net −63**, because `dreaming`'s `_protocol.py` declares
   **per-member signatures** (71 lines for `_decisions`' 11 members) rather than a shared kernel
   as `storage`'s does. This contradicted my earlier "0 recoverable until the last plane" claim —
   which was true for storage and false here.
5. **Tests: 259 failed, 98 errors.**

## Root cause, and why it kills the projection

```
AttributeError: 'DreamEngine' object has no attribute '_secret_reference_metadata'
```

`_decisions`' *private* members are an informal **cross-package API**. `client/` reaches them
directly through the composed engine — `self._engine._secret_reference_metadata(...)`,
`_relationship_status`, `_relationship_scope` — across **10 client files**.

So **"0 public methods" was not "no delegation surface"**. The delegation surface is *members
reached from outside the plane*, and for `dreaming` those are overwhelmingly private:

| package | public | privates reached externally | **true surface** |
|---|---:|---:|---:|
| `dreaming` | 20 | **16** | **36** |
| `client` | 97 | 12 | 109 |
| `agent_memory` | 47 | 3 | 50 |
| `storage/sqlite` | 144 | 20 | 164 |
| `storage/postgres` | 170 | 19 | 189 |

**Revised projection for `dreaming`: 36 × 8.1 ≈ 292 lines cost against 533 benefit — 0.55 : 1,
not 0.3 : 1.** Still nominally favourable, but the margin falls from 3.3× to 1.8×, and it rests
on a rate measured in `_policy`, whose members happen *not* to be reached externally. The one
realistic attempt did not convert cleanly.

## Verdict

**#150 is not achievable by mechanical conversion**, and the cost model built on public-method
counts was measuring the wrong surface. Four of five packages remain clearly net-negative. For
`dreaming` the margin is real but thin, unproven in practice, and gated behind a coupling problem
that has nothing to do with the mixin idiom.

**Recommend closing #150 as a decision record**, in the #119 shape, with the true-surface table
as the reason. It is not that the idiom is good — it costs 1,219 lines and 80% of the type debt —
it is that composition does not recover that cost at a price worth paying, and the one package
where the arithmetic works is blocked by something else.

## Separable finding worth its own issue

**`client/` reaches into `DreamEngine`'s private members from 10 files.** That is a genuine
layering defect, independent of #150 and visible without it: a consumer package depending on a
provider's `_private` surface. It is what made this conversion fail, and it would make *any*
restructuring of `dreaming` fail the same way. Worth filing — I have not filed it.

## Where this is wrong

The 8.1 lines/method rate came from `_policy`, a plane with no external private consumers. If
delegation for externally-reached privates is cheaper than for public methods — plausible, since
several are one-line property-style accessors — then 292 is an overestimate and `dreaming`'s
margin is better than 0.55:1. I did not measure that separately, and it is the number that would
most change this verdict.
