# Memotron productionization — what this branch is

**Audience:** anyone on the team who needs to understand this branch without reading 192 commits.
> **This document describes PR #103 — the readiness phase — and is accurate for it.** Work has
> continued on the stacked branch `production-hardening` (PR #131) since it was written: the
> product now forms memory (T1-1, T0-11), the Postgres audit plane detects tampering (T0-7),
> nine broken MCP serializers are fixed, and there is a live lane with a recorded baseline. For
> current state read `STATE.md`, not this file.

**Status: this phase is complete and the branch is in review.** Everything below is on the branch, green, and not yet merged — deliberately, so it gets read before it lands. Follow-on work continues on a branch stacked on top of this one; see *What happens next*.

Every number here was measured against the merge-base (`8225c7f`) at the time of writing, not
estimated. Where something is a judgement rather than a measurement, it says so.

---

## Why this exists

Memotron was written as a **research proof-of-concept** and it works — the memory lifecycle it
promises actually runs. But it was never built to be operated by a team or deployed to a real
environment, and that shows up in two ways.

**It was seven enormous files.** `dreaming.py` was 10,533 lines. `client.py` was 8,826. Five more
were between 1,800 and 5,700. A file that size can only be worked on by one person at a time, can't
be reviewed meaningfully, and makes every change feel risky because nobody can hold it in their head.

**There was no engineering safety net at all.** At the base commit the project had *no* linter, *no*
type checking, *no* coverage configuration, *no* pre-commit hooks, and *no* CI check script. That is
normal and fine for a PoC. It is not a thing you can hand to a team.

So the goal of this branch: **turn the research PoC into a codebase a team can safely work on**,
without changing what the product does. Making it *run well in production* is deliberately the
next phase, not this one.

---

## The one rule that shaped everything

**Zero behaviour change, zero import change.**

Every name you could import before, you can still import from the exact same place. Every function
does exactly what it did. The test suite passes unmodified at every single commit.

That constraint is why this branch is 192 commits instead of 10. It meant we couldn't "clean things
up while we were in there" — and that was the point. A refactor this large is only reviewable if the
answer to "did this change behaviour?" is *provably* no. Mixing in improvements would have destroyed
that property, and the improvements are worth more later when they can be reviewed on their own.

---

## What actually changed

### The seven big files are now packages

| module | was | now | largest file now |
|---|---|---|---|
| `dreaming.py` | 10,533 | 17 files | 1,329 |
| `client.py` | 8,826 | 16 files | 1,254 |
| `storage/sqlite.py` | 5,709 | 9 files | 1,130 |
| `config.py` | 4,361 | 15 files | 960 |
| `agent_memory.py` | 3,821 | 18 files | 519 |
| `admin_server.py` | 3,025 | 9 files | 2,133 |
| `models.py` | 1,826 | 11 files | 321 |

Each one became a small "composer" plus a set of single-concern files. Nothing moved between
modules; the code was cut along the seams it already had.

### A safety net that didn't exist before

Everything in this list is new on this branch:

- **ruff** (linting) and **mypy** (type checking), with **119 modules held to strict typing**
- **134 coverage floors** — per-module minimums that fail the build if coverage drops
- **`scripts/check.sh`** — one command, 12 gates. **Not the CI gate**: CI runs
  `ci-build-check.sh`, which is pytest only. Pointing the pipeline at `check.sh` is a
  one-variable change that cannot land until this script is on the default branch.
  (Corrected 2026-08-31 after an independent review; the original text said "used by CI".)
- **pre-commit hooks**, including secret detection
- **46 verification scripts** in `scripts/verify/`
- 13 new test files; the suite is now **1,247 tests**

### Real defects found and fixed along the way

The refactor wasn't the only output. Because the work required reading every line, it surfaced
things nobody knew about:

- **An MCP tool that was advertised but never registered.** `memory_restore` was implemented,
  documented, and listed as required — and missing one decorator, so it didn't exist over the
  protocol. The existing test called it as a plain function, so the suite was green.
- **A test that claimed to pin random IDs and didn't.** Found only when the Postgres test lane ran
  for the first time in the effort — which also took that backend from **0% to 86.8% coverage**,
  roughly 10% of the codebase that had never once executed in a test.
- **A cross-tenant write reachable by omitting an argument** — see the security section below.

### Consistent formatting, and a lesson about trusting a gate

The whole codebase now goes through one formatter (243 files, and it *removed* 2,225 lines by
collapsing things that fitted on one line), and limits on function size and complexity are switched
on — pinned to the worst function that exists today, so nothing fails now and nothing may get worse.
Those numbers are deliberately unflattering: the worst function is 191 statements long.

The formatting pass is worth one paragraph because of what it exposed. Our strictest safety check
reported **28 failures** — which reads like a formatter changing behaviour. It hadn't. Python
compiles a loop slightly differently depending on how the source is spread across lines, so the
instructions differ while the meaning doesn't. We established that rather than assuming it: every
function behind those 28 reports parsed to an identical tree, reduced to a six-line demonstration.

The conclusion was about the check, not the code — it's the right tool for *moving* code and the
wrong tool for *reformatting* it. So we wrote the right one. Worth recording because the tempting
move was to shrug and re-run it, which would never have given an answer.

### Documentation

23 markdown files, +8,603 lines: a defect register with evidence for every entry, a running
findings log, and a `STATE.md` that lets any session — human or agent — pick up where the last
one stopped.

---

## How we did it, and why it took 192 commits

The interesting part of this branch isn't the refactor. It's the **method**, because a 45,000-line
restructuring is exactly the kind of work where you can be confidently wrong.

**Every claim is checked by a machine, not by reading.** The main gate compares the *compiled
bytecode* of every function before and after a move. If a body changed at all, it fails. Another
computes the exact set of public names that left the API. Another records what every test does to
the database and diffs it. Another builds the actual installable package, installs it into a clean
environment, runs it, and compares the output against a build of the original code.

**That last one matters more than it sounds.** Every other check reads the source tree; none of
them starts the product. About 100 commits went by before anyone noticed that gap — and then a
second one: for its whole life that check had been comparing *"the product did nothing"* against
*"the product did nothing"*, identically, and calling it a pass. It now forms real memory on both
sides and fails loudly if it forms none.

**The gates repeatedly caught things that all the "normal" checks missed.** A representative
example, from the final module: 115 public names silently disappeared from the API — classes that
external code imports. Linting was clean. Type checking was clean. All 1,052 tests passed. Only the
set-difference check saw it, and it takes two seconds to run.

**We test the gates themselves.** A guard nobody has watched fail is a guard nobody has tested, so
each one gets deliberately broken to confirm it goes red. This caught a check that was silently
passing on empty input, and an allowance that was seven times wider than the evidence justified.

**Two independent reviewers checked the largest change.** Both passed it, and both corrected errors
in our own reporting — including one gate that had been reporting success while never actually
examining the package under review.

---

## The security pass, and two things we tried and abandoned

Four security items closed, and the work found one live problem rather than only
confirming old ones. A permission-restricted client could **un-archive another tenant's
memory by leaving an argument out** — passing the same value explicitly was correctly
refused. The check was there; it just did nothing when the argument was absent. Found by
testing the "what if you omit it" case, fixed, and covered by a test proved to fail
before the fix.

The tests that found it are **generated, not hand-written**, which is the more durable
change. The previous coverage was a hand-maintained list, and a whole file had simply
never been added to it — that was the actual root cause of the gap. There are now 103
cases derived from the code itself, so a new entry point is covered the moment someone
writes it.

Two pieces of planned work were **abandoned after measuring them**, and that is worth
recording as an outcome rather than a gap:

- **Breaking up the largest function** (1,075 lines). The documented reason it had been
  left alone turned out to be stale, so we cleared it — then found the real reason
  underneath. It isn't a long function, it's a mutable state machine: its parts don't
  hand each other results, they keep rewriting shared values. Extracting a piece would
  make the code harder to follow, not easier. It needs a rewrite, not a refactor.
- **A typing change to enable that**, which measured well on one half of the problem and
  did nothing for the other half. Recorded before building it.

Both are written down with the numbers, so the next person spends the measurement once
rather than the build.

## What we deliberately did *not* do

This is the part most worth knowing, because these look like omissions and aren't:

- **We didn't decouple further.** We measured three candidate improvements and **all three failed on
  their own evidence** — including one where our own metric turned out to be wrong. The honest
  conclusion: the code is already about as decoupled as this design allows, and further refactoring
  would be motion, not progress. *(This is a judgement based on those measurements, and it's the
  claim most worth challenging.)*
- **We didn't split `certification.py` (2,196 lines) or the admin request handler (2,133).** Both
  were examined; both are already made of small pieces that happen to share a file. Splitting them
  buys nothing.
- **We didn't fix the security items that need deployment decisions.** The ones that were ours to
  fix in code are fixed (see above). What remains is environmental — no request authentication on
  either surface, a role model that doesn't distinguish operators from users, and a network policy
  that exists but ships switched off on clusters that don't enforce it. Those need someone to decide
  how the service is deployed, not a patch.
- **We didn't reorganise the test files** into `unit/feature/integration`. It was planned, then
  measured and dropped: 4,234 golden records are keyed to test paths and would all need rewriting
  in the same commit that moved the files, while 48 of the 70 files fall into a single category.
  The one distinction that matters — which tests need a real database — is already marked.

---

## Where this stands

**The branch is green.** Every gate passes; the built package runs and behaves identically to a
build of the original code.

**It hasn't merged because merging is the cutover.** `main` is the frozen research baseline; this
branch is intended to replace it wholesale as the new engineering baseline, and merging releases to
lower environments. The agreed bar for that was *"more decoupled and manageable, with at least
parity."* Two of those three are demonstrable and one isn't — see above — so **the gate needs
rewording to "manageable + parity" before cutover.** That's a conversation, not a blocker.

**`main` is merged in and current.** Its one outstanding commit — a NetworkPolicy chart — is on the
branch. Worth reading carefully rather than ticking off: it closes the *literal* gap we filed ("the
chart has no NetworkPolicy"), but it ships switched **off**, and the chart's own note says the
production clusters don't enforce NetworkPolicy anyway. So the file exists and the protection does
not. It's recorded as partially closed for that reason.

**Immediately next:** reword the gate, then cut over. The housekeeping work — consistent formatting
and the complexity limits — is done.

**After cutover** — this is where the real production work starts, and it's the larger half:
modern Python structure (a proper exception hierarchy instead of ~1,000 generic errors, real types
instead of untyped dictionaries, breaking up the remaining oversized *functions*), then logging and
observability aligned to how our other applications do it, then everything needed to actually run
this in the upper environments.

---

## What happens next

**This is a stopping point, not a pause.** The goal it was opened for — make a research
prototype into something a team can safely work on — is met, and every item planned for
that phase is either done or has been measured out and written up with numbers. Nothing
is half-finished.

**It is not merged yet, on purpose.** It should be read first. 192 commits is a lot to
review, so the intended path is: this document, then `STATE.md`, then `bash
scripts/check.sh`, then the commits of whichever part you care about.

**Follow-on work continues on a branch stacked on this one.** That branch carries all
192 commits, so its pull request targets *this* branch rather than `main`, and it stays
that way until this one merges. Two reasons for stacking rather than waiting:

- the next items are large and cross-cutting (a proper exception hierarchy in place of
  ~1,000 generic errors is the obvious first one), and piling them onto a 192-commit
  branch would make an already-hard review harder rather than easier;
- the work after that is the actual production blockers, and those are independent of
  whether this has merged.

**What is explicitly NOT claimed by this branch.** It does not make Memotron
production-quality code throughout — 1,023 generic errors, ~500 untyped payload
dictionaries and 30 functions over 150 lines remain. It does not make it deployable: the
defect register still lists 85 open items, and five of them say the Postgres backend —
the production one — is broken in ways that make its audit and replay guarantees hollow.
None of that is regression from this work; it is what the work has made it possible to
approach safely.

---

## How to read this branch if you're reviewing it

- **Don't read it commit by commit.** Read `STATE.md` first — it's the resume pointer and explains
  the current state in order.
- **`TAKEOVER-BACKLOG.md`** is the defect register. Every entry cites evidence, and entries are
  tagged with whether a human verified them or an agent reported them.
- **`bash scripts/check.sh`** runs everything. If it's green, the branch is green.
- The commit messages are long on purpose. Each one records what was measured, what was assumed,
  and what the change *cost* — including where a change made a number worse.

**The condition under which this write-up is wrong:** it claims behaviour is unchanged, and that
rests on bytecode-identity checks plus a build-and-run comparison against the original. Both cover
executed and compiled code well. Neither covers behaviour that only appears under a real
Postgres-backed, LLM-enabled, multi-tenant deployment — which is precisely what the post-cutover
phase exists to exercise.
