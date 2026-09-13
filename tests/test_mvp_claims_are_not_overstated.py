"""User-facing surfaces must not claim erasure or content-level encryption.

#236, from `docs/design/16-encryption-and-erasure-for-mvp.md` (accepted 2026-09-04), which
states the constraint and assigns it to nobody. `docs/mvp-claims.md` is the owned version;
this file is what stops the repo contradicting it.

Three facts, each re-verified on `main` and against deployed `latest` on 2026-09-11 with
controls, not carried over from the design doc:

* **`memory_forget` does not delete.** It resolves to `mark_relationship(status=PRUNED)` -- an
  UPDATE. The row stays, the plaintext stays on disk, `memory_restore` reverses it.
  `agent_memory/_curation.py:4` says so itself: *"none of them DELETES"*.
* **The erasure machinery is unreachable.** `crypto_shred`, `issue_erasure_certificate` and
  `verify_erasure_certificate` have 0 call sites across both MCP servers and `admin_server/`.
  Control: `memory_forget`, which IS on the surface, returns 2 from the same query.
* **Nothing turns sealing on.** `erasure_behavior` defaults to `SOFT_RETIRE` and `.helm/` has 0
  references to it or to `CRYPTO_SHRED`. Measured live: a `search` on `latest` returned 0
  `dwcm1$` markers and readable plaintext.

WHAT THIS CAN AND CANNOT DO. It greps the repo. It cannot police a slide deck, a datasheet or a
security-questionnaire answer, which is where the real risk lives -- that is why
`docs/mvp-claims.md` exists and why #236 also asks that a human who owns launch messaging reads
Design 16. This file only guarantees the repo does not become the source of the overclaim.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Phrases that assert a capability we did not build, in a form no caveat can rescue.
#: Distinct from MENTIONING the machinery: `erasure.py` genuinely exists and the README is
#: right to document it. These are promises about what a deployment does.
FORBIDDEN = {
    "verifiable deletion": "no erasure certificate is reachable from any surface",
    "permanently delete": "memory_forget is an UPDATE; memory_restore reverses it",
    "permanently deleted": "same",
    "permanently removed": "same",
    "guaranteed deletion": "backups retain 4 (non-prod) / 8 (prod) with PITR on",
}

#: Capability words that are FINE to use, but only in a document that also points at the
#: claims doc. `erasure.py` exists; the trap is presenting it as something a deployment
#: exposes. A reader of the capability table would otherwise write a datasheet promising
#: erasure -- which is exactly what #236 was filed to prevent.
NEEDS_CAVEAT = ("crypto-shred", "crypto_shred", "erasure certificate", "machine-verifiable erasure")

CLAIMS_DOC = "docs/mvp-claims.md"


#: Surfaces a customer or their agent actually reads. NOT every file in `docs/`.
#:
#: `docs/data-model-and-tenancy.md` and `docs/environment-bring-up.md` mention crypto-shred and
#: are deliberately OUT of scope: they are internal engineering and operator documents, and
#: forcing a marketing caveat into them would be noise. The risk #236 names is *customer-facing
#: MVP material*; inside this repo that is the README, the consumer onboarding guide, the admin
#: UI, and the MCP tool descriptions every consuming agent reads.
def _user_facing() -> list[pathlib.Path]:
    paths = [
        ROOT / "README.md",
        ROOT / "docs" / "consumer-onboarding.md",
        ROOT / "src" / "memotron" / "mcp_server.py",
        ROOT / "src" / "memotron" / "agent_memory_mcp.py",
    ]
    paths += sorted((ROOT / "ui" / "admin" / "src").rglob("*.tsx"))
    return [p for p in paths if p.exists()]


#: The caveat requirement applies to PROSE documents only. A tool description cannot carry a
#: hyperlink usefully, and `memory_restore` saying "a crypto-shredded memory is refused" is
#: accurate and defensive -- it describes a refusal, not an available capability. Source files
#: are still covered by the forbidden-promises check and by the `memory_forget` assertion.
def _prose_documents() -> list[pathlib.Path]:
    return [p for p in _user_facing() if p.suffix == ".md"]


def test_there_are_surfaces_to_check() -> None:
    """The control, and it is not optional.

    My first search for these claims returned ZERO because a pathspec
    (`'ui/admin/src/**/*.tsx'`) matched no files -- and the control I ran alongside it used a
    DIFFERENT pathspec list, so it validated a different command and reported healthy. The
    README had 27 hits the whole time. A control must vary only the thing under test.
    """
    surfaces = _user_facing()
    # Name the required members rather than counting. A count is a magic number that gets
    # lowered until it passes; this fails loudly if a specific surface stops being checked.
    for required in (
        ROOT / "README.md",
        ROOT / "docs" / "consumer-onboarding.md",
        ROOT / "src" / "memotron" / "agent_memory_mcp.py",
    ):
        assert required in surfaces, f"{required.name} is no longer being checked"
    assert any(p.suffix == ".tsx" for p in surfaces), "the admin UI glob matched nothing"
    assert _prose_documents(), "no prose documents in scope; the caveat rule checks nothing"
    # Positive control: a word that MUST appear, proving these files are actually being read.
    assert any("memory" in p.read_text(errors="replace").lower() for p in surfaces)


@pytest.mark.parametrize("phrase", sorted(FORBIDDEN))
def test_no_user_facing_surface_promises_it(phrase: str) -> None:
    """THE GUARD. A promise no caveat can rescue.

    Read each file ONCE into a variable before asserting: `x not in ""` is always True, so a
    negative assertion against a value re-derived per check can pass because the value was
    empty.
    """
    pattern = re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE)
    offenders = []
    for path in _user_facing():
        text = path.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:90]}")

    assert not offenders, (
        f"user-facing surface promises {phrase!r} -- {FORBIDDEN[phrase]}.\n  "
        + "\n  ".join(offenders)
        + f"\n\nSee {CLAIMS_DOC} for the accurate wording."
    )


def test_documents_that_describe_the_erasure_machinery_carry_the_caveat() -> None:
    """Mentioning `erasure.py` is fine. Mentioning it with no pointer is how a datasheet
    ends up promising erasure.

    This is deliberately NOT a word ban: the README is right that the module exists, and
    deleting accurate documentation to satisfy a grep would be worse than the problem. The
    requirement is only that such a document also says where the limits are written down.
    """
    offenders = []
    for path in _prose_documents():
        text = path.read_text(encoding="utf-8", errors="replace")
        mentions = [w for w in NEEDS_CAVEAT if w.lower() in text.lower()]
        if mentions and CLAIMS_DOC not in text:
            offenders.append(f"{path.relative_to(ROOT)} mentions {mentions} but never links {CLAIMS_DOC}")

    assert not offenders, (
        "\n  ".join(["document(s) describe erasure/sealing with no pointer to the limits:", *offenders])
        + f"\n\nAdd a link to {CLAIMS_DOC} near the claim. Do NOT delete the description if it is "
        "true about the code -- the point is that a reader learns it is unreachable, not that the "
        "module goes unmentioned."
    )


def test_memory_forget_still_describes_itself_as_soft_retire() -> None:
    """The positive half. Forbidding words does not make the description accurate.

    `memory_forget` is the tool most likely to be reworded into an overclaim, because
    "forget" already sounds like deletion. Its MCP description is what every consuming agent
    reads, so it is customer-facing in the most literal sense.
    """
    source = (ROOT / "src" / "memotron" / "agent_memory_mcp.py").read_text()
    match = re.search(r'async def memory_forget\(.*?"""(.*?)"""', source, re.DOTALL)
    assert match, "memory_forget or its docstring moved; this guard is no longer reading it"

    description = match.group(1)
    assert "soft-retire" in description.lower(), (
        f"memory_forget's description no longer says soft-retire: {description.strip()[:120]!r}. "
        "It resolves to an UPDATE and memory_restore reverses it -- describing it as deletion "
        "tells every consuming agent something false."
    )


def test_the_claims_document_exists_and_is_reachable() -> None:
    """A rule nobody can find is a rule nobody follows."""
    claims = ROOT / "docs" / "mvp-claims.md"
    assert claims.exists(), "docs/mvp-claims.md is missing; this guard enforces a file that is gone"

    text = claims.read_text()
    for anchor in ("archive with exclusion", "CMEK", "point_in_time_recovery"):
        assert anchor in text, f"docs/mvp-claims.md no longer states {anchor!r}"
