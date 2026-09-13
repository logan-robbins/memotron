# `scripts/split/` — the tools that did the module split

**The split programme is complete.** All seven god-modules are packages. These are the
tools that moved the code, versioned now rather than left in a scratchpad, and they are
**not gates** — nothing in `scripts/check.sh` runs them and nothing depends on them.

They are here for two reasons, in order of importance:

1. **They encode traps that cost real time.** Every one of the notes below was learned by
   getting it wrong first, on a branch where a silent mistake could move 45,000 lines.
2. Two large files remain unsplit on purpose (`certification.py`, the admin request
   handler). If either is ever split, or a new module grows past readable, this is the
   machinery — and re-deriving it would cost days.

> Why not `scripts/verify/`? That directory is documented, in the `verifying-memotron`
> skill, as *the code gates* — things that answer a yes/no question about the tree and
> exit non-zero. These transform the tree instead. Filing them together would blur a
> category the skill leans on. (`STATE.md` said `scripts/verify/`; this is a deliberate
> departure, noted so it does not read as drift.)

## The tools

| tool | shape | what it does |
|---|---|---|
| `extract.py` | B | Moves a named set of top-level definitions out of a package `__init__` into a submodule. |
| `extract_mixin.py` | A | Moves methods out of a god-class into a mixin submodule. Handles nested classes and class-level attributes. |
| `wire.py` | A | Post-extraction wiring: resolves Shape-B references and installs the `_Base` Protocol. |
| `prune.py` | — | Three-way dead-import check, so an extraction does not leave the composer importing what it no longer uses. |
| `carry.py` | — | Resolves module-level names a freshly-extracted mixin still reads. Run it **after** `pure_move` names them, not before. |
| `move_members.py` | — | Moves methods between two already-split files, or onto the composer. This is the R-A2 fixer. |
| `surface.py` | — | Every name a module defined must still be importable from the package. |

## What they taught us, which is the part worth keeping

- **A `_*.py` glob matches `__init__.py`.** It emits `from pkg.__init__ import X`, a hard
  circular `ImportError` that ruff and `pure_move` are both clean on. Hit in the models
  split, then **again in a different tool two sessions later**. Exclude it explicitly in
  anything that globs private modules.
- **Run `carry.py` after `pure_move`, never from a guess.** You do not know which
  module-level constants a moved body reads until the bytecode check tells you. Guessing
  cost three reverts on the `agent_memory` split.
- **A class-qualified self-reference is a body edit, and body edits cost the proof.**
  `carry.py` reports `TheComposedClass.method` and refuses to rewrite it, because that is
  a decision a human should make and it forfeits `pure_move` coverage on that function.
- **`prune.py` is three-way for a reason.** A name can be unused in the composer, unused
  in the new submodule, and still load-bearing because a *test* patches it by string.
  That exact case (`memotron.models.uuid4`) survived a grep-based check and was only
  caught when the Postgres lane ran for the first time.
- **`move_members.py` reports whether the members form a contiguous block.** A
  non-contiguous move is legal but is no longer one `git mv`-shaped change, and reviewing
  it as though it were is how a member goes missing.

## Using them

Each is a single file with its usage in its module docstring; read that first. The
workflow the seven splits actually used:

```bash
# 1. cut
python scripts/split/extract_mixin.py <pkg_dir> <ClassName> <method> [<method> ...]
python scripts/split/wire.py <pkg_dir>

# 2. find what the cut broke -- pure_move is the oracle, not intuition
uv run python scripts/verify/pure_move.py HEAD .
python scripts/split/carry.py <pkg_dir>          # resolve the names it named
python scripts/split/prune.py <pkg_dir>          # drop what is now dead

# 3. prove it
uv run python scripts/verify/pure_move.py HEAD .   # bytecode identity
bash scripts/verify/api_removals.sh                # the FULL removal set
bash scripts/check.sh
```

**Do not trust a green `pure_move` alone.** It reads function *bodies*: a name in a
default argument is invisible to it, class-level statements are invisible to it, and it
cannot verify a reformat at all (see `scripts/verify/ast_identity.py`). Know which gate
covers which position — that table is in the `verifying-memotron` skill.
