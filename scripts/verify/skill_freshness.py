#!/usr/bin/env python3
"""Is the verification skill still true about this repo?

    uv run python scripts/verify/skill_freshness.py

A skill is a cache of hard-won knowledge, and like any cache it goes stale
silently. This one did: on 2026-08-28 the INSTALLED copy still asserted that
`runtime.py:177/232/320/385 call the bare load_env_file()` -- a defect closed the
day before. Anyone loading it would have hunted a bug that was not there, in code
that no longer looked like that. It was found because Ryan asked, not because
anything detected it.

Two failure modes, and this catches the mechanical halves of both:

  DIVERGENCE  the repo copy and the installed copy drift apart, and the one that
              gets LOADED is not necessarily the one that gets edited. Here the repo
              copy was correct and the installed one was a session behind.
  DEAD REFS   the skill points at a script, probe or doc that has moved or gone.

What it deliberately does NOT catch, so nobody over-trusts it: a claim that is
well-formed and false. "This bug is live" stays syntactically perfect after the fix.
That needs the human half of the loop -- see the evolution step in the `worklog`
skill, which runs at session end and asks what was learned that outlives the
session.

Exit 0 fresh, 1 stale, 2 the skill is missing.
"""

from __future__ import annotations

import pathlib
import re

REPO_COPY = pathlib.Path("docs/findings/verifying-memotron-SKILL.md")
INSTALLED = pathlib.Path.home() / ".claude/skills/verifying-memotron/SKILL.md"

#: The repo copy carries a short preamble explaining that it IS a copy; the
#: installed file starts at the frontmatter. Compare from there.
PREAMBLE_END = "---\n"


def main() -> int:
    if not REPO_COPY.exists():
        print(f"  {REPO_COPY} is missing")
        return 2

    stale = 0
    text = REPO_COPY.read_text()

    # -- divergence -----------------------------------------------------------
    if not INSTALLED.exists():
        print(f"  NOT INSTALLED  {INSTALLED} -- the skill cannot fire for anyone")
        stale += 1
    else:
        body = text.split(PREAMBLE_END, 1)[1] if PREAMBLE_END in text else text
        if (
            PREAMBLE_END + body.lstrip("\n") not in INSTALLED.read_text()
            and INSTALLED.read_text().strip() != (PREAMBLE_END + body).strip()
        ):
            print("  DIVERGED  the repo copy and the installed copy differ.")
            print("            The repo copy is the source of truth -- regenerate the")
            print("            installed one from it, not the other way round.")
            stale += 1
        else:
            print("  in sync    repo copy == installed copy")

        # The installed file must BEGIN with its frontmatter. Added 2026-08-31 after
        # `cp docs/findings/verifying-memotron-SKILL.md ~/.claude/skills/.../SKILL.md`
        # carried the repo copy's preamble across, and Claude Code then read the first
        # blockquote line as the skill's `description` -- so the skill still loaded, and
        # loaded describing itself as "Copy of a personal Claude Code skill", which fires
        # for nothing. This check reported "in sync" throughout, because the comparison
        # above strips the preamble from BOTH sides and therefore cannot see one leaking
        # into the installed copy. A gate blind to the exact failure it exists to prevent
        # is the shape this whole branch keeps finding; this is that shape in the freshness
        # gate itself.
        installed_text = INSTALLED.read_text()
        if not installed_text.startswith(PREAMBLE_END + "name:"):
            first = installed_text.split("\n", 1)[0][:60]
            print("  BAD HEADER  the installed copy does not start with its frontmatter.")
            print(f"              first line is {first!r}")
            print("              Claude Code parses the description from the top of the")
            print("              file -- regenerate by STRIPPING the repo copy's preamble:")
            print(
                '                python -c "import pathlib;p=pathlib.Path('
                "'docs/findings/verifying-memotron-SKILL.md').read_text();"
                "pathlib.Path.home().joinpath('.claude/skills/verifying-memotron/SKILL.md')"
                ".write_text(p[p.index('---\\n'):])\""
            )
            stale += 1

    # -- dead references ------------------------------------------------------
    # Repo-relative paths, plus ABSOLUTE ~/repos/... ones. The absolute case was added
    # 2026-08-30 after this check reported FRESH while the skill's own "Repo root:" line
    # pointed at `~/repos/jedai/worktrees/memotron-implementation`, a worktree that had
    # been deleted. Every path in the skill was "still there" because the pattern only ever
    # looked at four prefixes -- the one path a reader follows FIRST was the one not checked.
    refs = sorted({m.group(1) for m in re.finditer(r"`((?:src|scripts|tests|docs)/[\w/.-]+)`", text)})
    # `~/repos/...` ONLY, deliberately. A first pass matched every `~/` path and flagged
    # `~/.memotron/memory.sqlite` -- which is not a dead link, it is the skill correctly
    # documenting where `memotron init` WRITES a file that may not exist yet. Checking
    # runtime paths for existence manufactures a false positive, and a gate with those is
    # one people learn to bless past. `~/repos/...` means "go and look here", so it either
    # resolves or the instruction is broken.
    home_refs = sorted({m.group(1) for m in re.finditer(r"`(~/repos/[\w/.-]+)`", text)})

    dead = [r for r in refs if not pathlib.Path(r).exists()]
    dead += [r for r in home_refs if not pathlib.Path(r).expanduser().exists()]
    total = len(refs) + len(home_refs)
    print(f"  {total - len(dead)}/{total} referenced paths still exist ({len(home_refs)} absolute)")
    for r in dead:
        print(f"      DEAD  {r}")
        stale += 1

    # -- does it know the gates that exist? -----------------------------------
    # Additive staleness is the quieter failure: the skill stays true and stops being
    # complete, and a gate it has never heard of does not get run.
    #
    # Derived from check.sh's own lane list plus the milestone gates, NOT from a glob
    # of scripts/verify/. A glob would demand the skill name every benchmark and
    # helper in there, which is noise -- and a freshness check that manufactures
    # busywork is one people switch off.
    check_sh = pathlib.Path("scripts/check.sh").read_text()
    gates = set(re.findall(r"scripts/verify/(\w+\.py)", check_sh))
    gates |= {"sanity.sh", "coupling_report.py", "api_removals.sh", "ast_identity.py", "live_lane.sh"}
    unknown = sorted(g for g in gates if g not in text)
    print(f"  {len(gates) - len(unknown)}/{len(gates)} gates are mentioned in the skill")
    for g in unknown:
        print(f"      UNMENTIONED  scripts/verify/{g}")
        stale += 1

    print()
    print("SKILL FRESHNESS: FRESH" if not stale else f"SKILL FRESHNESS: STALE -- {stale} issue(s)")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
