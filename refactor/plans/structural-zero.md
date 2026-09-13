# Audit — Unit 1: Structural zero

> **STATUS 2026-09-02: SZ-1 AND SZ-2 BOTH DONE** (`c2df472`, `7ec1fb0`, `fa02fb6`).
> SZ-2 was approved after its open question was answered: the `DreamConfig` structural hash
> appears **only** in the api_surface golden — no cache key, artifact or contract version.
> The `config↔prompts` cycle carries to unit 7.
>
> Achieved: cycles **3→1** · modules-in-cycle **12→2** · largest **8→2** · upward tier edges
> **1→0**. Both metrics re-baselined; **both ratchets proved** by control — reintroducing
> either fault now exits 1 where it previously passed.
>
> **The 28-row estimate was wrong.** Real cost: **66 golden rows replaced + 3 new**, plus a
> coverage-floor entry the gate itself asked for. My probe measured api_surface but never ran
> `cov-floor`, so the radius was under-counted — the same incomplete-probe lesson as SZ-1's
> lint failure, in a second form.
>
> One thing my probe missed: it ran `pytest` and `api_surface` but **not ruff**, so `lint`
> failed on first execution (`I001`, import block unsorted — `memotron.storage` sorts after
> `crypto`/`receipts`/`replay`). Fixed with `ruff check --fix`. **A probe that skips a lane the
> commit must pass is an incomplete probe.**

**Verdict: 2 items admitted · 1 decision required · 0 defects found**
Commit audited: `667e748` · Audited: 2026-09-02 · Tier-C spend authorised: none (no item needed a second pass)

Goal: drive `coupling_report.py`'s gated structural metrics to zero so the gate becomes a real
ratchet for units 2–10. A gate that passes at a non-zero baseline cannot distinguish *the cycle
we accepted* from *the cycle unit 4 introduced*.

---

## Instrument readings — reproduce these before reading anything below

| Instrument | Command | Reading (`N_now`) | Denominator (proof it looked) | Control result |
|---|---|---|---|---|
| coupling | `uv run --no-sync python scripts/verify/coupling_report.py` | cycles **3** · modules-in-cycle **12** · largest **8** · upward tier edges **1** | 3 cycles listed with all 20 edges and their file:line | ✅ `models`→`storage` probe: **1 → 2 WORSE**, reverted to 1 |
| api surface | `scripts/verify/api_surface.py --check` | PASS | **9,628 lines**, no MRO shadowing, 5 guards owned by the composer | ✅ failed loudly on the settings move (28 rows) |
| tests | `pytest -q` with the DSN | 1,391 | 1,389 passed · 1 skipped · 1 xfailed | ✅ ran with Postgres; `postgres` lane PASS in the 16-lane baseline |

> **The first control I wrote was wrong, and the failure is instructive.** I added a second
> `config → storage` import and the reading did not move, which looks like a broken gate. It is
> not: line 336 counts distinct **(subsystem, subsystem)** pairs, not import sites, and the
> baseline comment says so — *"upward tier edges is 1: config -> storage, at
> `config/_dream_config.py:61` **and** `config/_storage_env.py:14`"*. One edge, two sites. The
> corrected control uses a genuinely new pair. **Had I accepted the first result, I would have
> reported a working instrument as broken.**

---

## Items admitted

| id | Claim (falsifiable) | `N_now` | `N_after` (**PROBED**) | Δ | Falsifier + source | Commit size | Risk |
|---|---|---|---|---|---|---|---|
| **SZ-1** | Retargeting 3 imports off the `memotron.receipts` compat shim kills 2 of the 3 cycles | cycles 3 · modules 12 · largest 8 | cycles **1** · modules **2** · largest **2** | −2 / −10 / −6 | Sign: any gated row moves. Source: `coupling_report.BASELINE` | 3 lines, 3 files | **Low** |
| **SZ-2** | Relocating `EngineSettings`/`StorageSettings` out of `storage/` removes the one upward tier edge | upward **1** | upward **0** | −1 | Sign, as above | 83-line module move + shim + 2 call sites | **Medium** — see decision |

**SZ-1 verification (measured, not forecast):** `1389 passed, 1 skipped, 1 xfailed` with the
Postgres DSN set, and `API SURFACE: PASS`. Imports clean. This one is unambiguous.

**SZ-1 must land before SZ-2.** Discovered by probing, not reasoning: applying SZ-2 alone
**breaks the package import** —

```
ImportError: cannot import name 'ReceiptLedger' from partially initialized module
'memotron.receipts' (most likely due to a circular import)
```

Relocating the settings module changes initialisation order and surfaces the previously benign
`receipts↔storage` cycle. SZ-1 removes that cycle, after which the combined state imports
cleanly. **A forecast would have produced a plan that breaks the build.**

---

## Decision required — SZ-2 has two forms with very different blast radii

| | (a) Move the module | (b) Reclassify the tier |
|---|---|---|
| Change | `storage/settings.py` → `config/_storage_settings.py`, re-export shim left behind | one line in `coupling_report.TIERS`, with the reason |
| Tier edge | 1 → 0 | 1 → 0 |
| Production code changed | 3 files | **none** |
| **api_surface golden rows changed** | **28** | **0** |
| What the 28 rows are | `DreamConfig`'s structural hash `f88727189a6c → 6cf3e2fa9b83` **everywhere it is re-exported** — because `DreamConfig` carries a `storage: StorageSettings` field and the type's module path feeds the fingerprint | — |

Option (a) is the honest structural fix but changes the public fingerprint of a widely
re-exported class. Option (b) is free, and defensible on the merits — `storage/settings.py` is
**83 lines importing nothing but `typing` and `pydantic`**, so classifying it as `infra` is
arguably the error, and `TIERS` is a hand-written *design statement*, not a derived fact.

**I am not choosing this one.** (b) risks looking like gaming the metric; (a) costs a 28-row
re-bless of a golden whose purpose is to make exactly this kind of identity change visible.
It needs a reviewer.

---

## Candidates rejected

| Candidate | The number that killed it | Source | Escalated? |
|---|---|---|---|
| Make the `config↔prompts` lazy import eager | Not a number — an **in-code decision record**. `config/_prompts.py:3-4`: *"The lazy import of `memotron.prompts` inside `default_prompt_profiles` is **load-bearing and stays function-local**"* | the module's own docstring | See reconciliation |

---

## Prior reconciliation

| Record | Subject | My blind pass found | Verdict | Its own number, re-run today |
|---|---|---|---|---|
| `config/_prompts.py:3-4` | the `config↔prompts` lazy import | The cycle is real: `prompts/__init__.py:15` imports `DreamPromptProfile` from `config`; `config/_prompts.py:94` lazily imports back | **NEW ANGLE** | The record's claim still holds — the import *must* stay lazy **given the current type location**. It does not address relocating `DreamPromptProfile` |

The record forecloses *making this import eager*. It does not foreclose *moving the type so the
import is unnecessary*. Stepping outside its stated scope, so it is argued rather than
overridden. **Not admitted as an item in this unit** — it is a design change needing its own
probe, and unit 7 (`config` + `prompts`) is its natural home.

**Consequence to state plainly:** with SZ-1 and SZ-2 only, cycles go **3 → 1, not 3 → 0**, and
the `coupling_report.BASELINE` re-baseline is therefore **partial** — cycles 1, modules 2,
largest 2, upward 0. Full structural zero needs the unit-7 decision.

---

## Defects quarantined out of this plan

None found in this unit. (X7 — `admin_server` returning raw `str(exc)` — belongs to unit 9.)

---

## Execution — ordered commits

1. **SZ-1** — retarget 3 imports off the compat shim.
   `storage/base.py:157` · `erasure.py:43` · `replay.py:32`.
   Proved by: `pytest` (1,389 pass) + `api_surface.py --check` (PASS, unchanged).
   **Not** `pure_move.py` — no code moves; these are import retargets.
2. **SZ-2** — the tier edge, in whichever form is approved. If (a): re-bless the golden in the
   **same commit**, with the 28 rows and the `DreamConfig` hash change named in the message.
3. **Re-baseline** — edit `coupling_report.BASELINE` to the achieved values in its own commit,
   with the reason. The file has no `--bless` **by design**; this is a deliberate edit.

**Blast radius, counted:** api_surface golden **0 rows** (SZ-1) / **28 rows** (SZ-2 option a) ·
receipt_stream **0** · storage_profile **0** · coverage floors **0** · mypy suppressions **0**.

Bless procedure: one full run with `MEMOTRON_TEST_POSTGRES_DSN` set, `NO_COLOR=1 TERM=dumb`.

---

## Where this is wrong

The claim most worth attacking is that **SZ-2 is worth doing at all in form (a)**. It buys one
gated metric moving 1→0 and costs a 28-row change to the fingerprint of the most widely
re-exported class in the package. If a reviewer decides that trade is bad, the honest outcome is
form (b) or dropping SZ-2 entirely and re-baselining only the cycle metrics — SZ-1 stands alone
on its own evidence either way. What would refute my framing: if the `DreamConfig` hash is
consumed by anything outside the golden (a cache key, a serialised artifact, a stored contract
version), then option (a) is not surface-neutral in the way "it is only a re-bless" implies, and
**I have not checked that** — it should be checked before (a) is chosen.
