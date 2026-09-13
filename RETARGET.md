# Retargeting this branch at `main` after #131 merges

**Delete this file in the commit that does the retarget.** It exists because the mistake it
prevents is silent, and because this exact trap already cost time on this repo once, when #103
squash-merged underneath #131.

## The trap

`jedai/memotron` **squash-merges**. Verified 2026-09-01: `e5fb5a4` (#103), `f8eb862` (#147)
and `e84c240` (#152) each have exactly **one parent**.

So when #131 merges, `production-hardening`'s **254 commits become a single commit on `main`**
under a brand-new SHA. Git has no record that those 254 are contained in it. This branch still
carries all 254 in its history, so pointing a PR at `main` renders:

    254 already-merged commits  +  whatever Phase 3 work is on top

which is unreviewable — and worse, it reads as though this branch is re-proposing work that
already landed.

## The fix, once #131 has merged

```bash
git fetch origin main
git merge -s ours origin/main -m "merge: record that #131's squash landed in main (tree unchanged)"
git diff origin/main HEAD --stat      # MUST show only Phase 3 work
```

`-s ours` records `origin/main` as a parent **without taking any of its content**. This branch's
tree is already correct — it descends from the same commits #131 squashed — so that one merge
collapses the PR diff down to just the new work.

## Verify immediately after, because `-s ours` fails silently

The same command that fixes the ancestry will **discard someone else's work** if `main` moved in
a way this branch has not absorbed. It never conflicts and never warns.

```bash
git diff origin/main HEAD -- .helm/ src/ tests/ | head -40
```

If that shows anything nobody wrote on this branch, **stop** — `main` gained changes that
`-s ours` has just thrown away. Recovery is to drop that merge commit (a hard reset back one
commit) and then do a *real* `git merge origin/main` before re-checking. Note that the
destructive-git PreToolUse hook blocks the agent from running that reset, so Ryan performs it.

A cheap pre-check that avoids the whole situation: before merging, confirm
`git log HEAD..origin/main --oneline` contains nothing except the squash of #131 itself.

## Base at the time of branching

* branched from `production-hardening` at `9e09422`, which is a *real* merge of `origin/main` —
  so #147 and Adam's #152 prod cutover are shared ancestry rather than part of this branch's diff
* `check.sh` on that commit: **16 PASS + corpus SKIPPED**, with the Postgres DSN set
* `main` at branch time: `e84c240`

## What is queued for this branch

Per `STATE.md`'s *Next session* block:

* **Phase 3 cluster 2** — sqlite↔postgres duplication, ~282 lines claimed and **UNMEASURED**.
  Both clusters that *were* measured came in far under claim (300 → 138, 347 → 2), so measure
  before building. It also interacts with `parity-cov`: a shared helper could genuinely close
  some of the 27 one-sided methods, or could hide divergence behind it — the `anchor` failure
  mode wearing a nicer hat. Do it serially, reading the gate before and after.
* Decisions rather than code: **#148** (the admin PVC has never stored a byte), **#149** (27
  one-sided storage methods), **#150** (pilot object composition off the mixin idiom), **#151**
  (two categories of API surface no gate records), and **#130**'s lower-environment question —
  whether `latest`/`stage`/`load` get `MEMOTRON_ALLOW_EPHEMERAL_KEK` or wait for a real key
  manager.
