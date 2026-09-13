"""Three-band gold-set evaluation harness (INGEST.md §7 Phase 2).

Runs ``ingest/goldset.yaml`` against the ingested graph and scores, per question:

* **found**      -- did any returned fact contain an expected answer string
* **slugs**      -- which ``slug#anchor`` the evidence actually came from
* **origin/hops**-- how the result arrived (``direct`` / ``entity_hop`` /
  ``theme_member`` / ``member_theme`` / ``seed`` / ``pinned``)
* **tokens**     -- injected-context cost of the graph answer
* **baseline**   -- injected-context cost of the naive alternative

Baseline
--------
INGEST.md's pass bar is "parity on Band 1, clear win on Bands 2-3 at lower token
cost" against a document-search stuffing baseline. The baseline implemented here
is deliberately *generous to the baseline*: score every page by query-term
overlap (an IDF-weighted bag-of-words stand-in for Pagefind), take the top-ranked
page, and charge the tokens of that whole page. For Band 2 the baseline is
charged the top-N pages needed to actually cover the answer, because a single
page cannot answer a cross-page join.

Nothing here mutates the graph.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kb_config as kb
import preprocess as pre

GOLDSET_PATH = Path(__file__).resolve().parent / "goldset.yaml"

CHARS_PER_TOKEN = 4.0
_WORD_RE = re.compile(r"[a-z0-9_]+")
_STOP = {
    "the",
    "a",
    "an",
    "of",
    "and",
    "or",
    "to",
    "for",
    "in",
    "on",
    "is",
    "are",
    "what",
    "which",
    "how",
    "does",
    "do",
    "it",
    "its",
    "that",
    "this",
    "with",
    "at",
    "by",
    "from",
    "into",
    "be",
    "was",
    "were",
    "as",
}


def tokens_of(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def terms(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOP and len(w) > 1]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def contains_any(haystack: str, needles: list[str]) -> str | None:
    hay = normalize(haystack)
    for needle in needles:
        if normalize(needle) in hay:
            return needle
    return None


# --------------------------------------------------------------------------
# Naive baseline: lexical page ranking + whole-page stuffing
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Corpus:
    """Preprocessed pilot pages, used only to price the baseline."""

    pages: dict[str, str] = field(default_factory=dict)
    idf: dict[str, float] = field(default_factory=dict)

    @classmethod
    def build(cls, sections: list[pre.Section], *, only_slugs: set[str] | None = None) -> Corpus:
        """Build the baseline corpus.

        ``only_slugs`` restricts pricing to the pages that were actually ingested
        (read from ``.manifest.json``). Without it the baseline would be ranked
        over all 77 pilot pages while the graph knows only the ingested subset --
        which would flatter the graph by letting the baseline rank a page the
        graph was never given. Same corpus on both sides or the comparison is
        meaningless.
        """
        pages: dict[str, list[str]] = defaultdict(list)
        for section in sections:
            if only_slugs is not None and section.slug not in only_slugs:
                continue
            pages[section.slug].append(section.section_text)
        joined = {slug: "\n\n".join(parts) for slug, parts in pages.items()}
        document_frequency: Counter = Counter()
        for text in joined.values():
            document_frequency.update(set(terms(text)))
        total = max(1, len(joined))
        idf = {term: math.log(1 + total / (1 + count)) for term, count in document_frequency.items()}
        return cls(pages=joined, idf=idf)

    def rank(self, query: str) -> list[tuple[str, float]]:
        query_terms = set(terms(query))
        scored: list[tuple[str, float]] = []
        for slug, text in self.pages.items():
            counts = Counter(terms(text))
            score = sum(self.idf.get(term, 0.0) * (1 + math.log(counts[term])) for term in query_terms if counts[term])
            if score > 0:
                scored.append((slug, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored

    def stuffing_tokens(self, query: str, *, pages: int = 1) -> tuple[int, list[str]]:
        ranked = self.rank(query)[:pages]
        return sum(tokens_of(self.pages[slug]) for slug, _ in ranked), [s for s, _ in ranked]

    def covering_tokens(self, query: str, expect_slugs: list[str]) -> tuple[int, int]:
        """Tokens the baseline must stuff to actually cover ``expect_slugs``.

        Walks the lexical ranking until every expected slug has been included --
        i.e. the honest cost of answering a cross-page join by stuffing pages.
        Returns ``(tokens, pages_needed)``.
        """
        wanted = {slug for slug in expect_slugs if slug in self.pages}
        if not wanted:
            return self.stuffing_tokens(query)[0], 1
        total = 0
        pages_used = 0
        for slug, _score in self.rank(query):
            total += tokens_of(self.pages[slug])
            pages_used += 1
            wanted.discard(slug)
            if not wanted:
                break
        for slug in wanted:  # never surfaced by lexical rank; still must be paid for
            total += tokens_of(self.pages[slug])
            pages_used += 1
        return total, pages_used


# --------------------------------------------------------------------------
# Querying the graph
# --------------------------------------------------------------------------


@dataclass(slots=True)
class QueryOutcome:
    id: str
    question: str
    found: bool
    matched: str | None
    slugs: list[str]
    origins: Counter
    max_hops: int
    result_count: int
    graph_tokens: int
    baseline_tokens: int
    baseline_pages: int
    evidence_precision: float
    notes: str = ""


async def answer(
    client,
    *,
    question: str,
    scope,
    limit: int,
    as_of=None,
) -> tuple[list[Any], list[str], Counter, int, int]:
    """Return ``(results, slugs, origins, max_hops, injected_tokens)``."""
    results = await client.search(query=question, scope=scope, limit=limit, as_of=as_of)
    origins: Counter = Counter()
    slugs: list[str] = []
    injected = 0
    max_hops = 0
    for result in results:
        origins[result.origin] += 1
        max_hops = max(max_hops, result.hops)
        injected += tokens_of(result.fact)
        # SearchResult carries no metadata; slug/anchor live on the evidence
        # episodes (and on relationship properties inherited from them).
        try:
            evidence = await client.memory_evidence(relationship_uuid=result.relationship_uuid)
        except ValueError:
            continue
        for source in (evidence.metadata, *(e.metadata for e in evidence.episodes)):
            slug = source.get("slug")
            if slug and slug not in slugs:
                slugs.append(slug)
    return results, slugs, origins, max_hops, injected


async def run_band(
    client,
    *,
    scope,
    corpus: Corpus,
    entries: list[dict[str, Any]],
    limit: int,
    band: str,
) -> list[QueryOutcome]:
    outcomes: list[QueryOutcome] = []
    for entry in entries:
        question = entry["question"]
        expect_any = entry.get("expect_any", [])
        expect_slugs = entry.get("expect_slugs", [])
        results, slugs, origins, max_hops, injected = await answer(client, question=question, scope=scope, limit=limit)
        matched = None
        for result in results:
            hit = contains_any(f"{result.fact} {result.object} {result.subject}", expect_any)
            if hit:
                matched = hit
                break

        if band == "band2":
            baseline_tokens, baseline_pages = corpus.covering_tokens(question, expect_slugs)
        else:
            baseline_tokens, ranked = corpus.stuffing_tokens(question)
            baseline_pages = len(ranked)

        hit_slugs = [s for s in slugs if s in expect_slugs]
        precision = len(hit_slugs) / len(slugs) if slugs else 0.0

        notes = ""
        min_slugs = entry.get("min_slugs")
        if min_slugs is not None and len({s for s in slugs if s in expect_slugs}) < min_slugs:
            notes += f"join incomplete ({len({s for s in slugs if s in expect_slugs})}/{min_slugs} slugs) "
        if entry.get("require_expansion") and not (
            set(origins) & {"entity_hop", "theme_member", "member_theme", "seed"}
        ):
            notes += "no expansion-attributed result "

        found = matched is not None
        if band == "band2" and min_slugs is not None:
            found = found and len({s for s in slugs if s in expect_slugs}) >= min_slugs

        outcomes.append(
            QueryOutcome(
                id=entry["id"],
                question=question,
                found=found,
                matched=matched,
                slugs=slugs,
                origins=origins,
                max_hops=max_hops,
                result_count=len(results),
                graph_tokens=injected,
                baseline_tokens=baseline_tokens,
                baseline_pages=baseline_pages,
                evidence_precision=precision,
                notes=notes.strip(),
            )
        )
    return outcomes


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def print_band(title: str, outcomes: list[QueryOutcome], *, gate: bool) -> dict[str, Any]:
    print()
    print("=" * 100)
    print(f"{title}   ({'GATE' if gate else 'MEASUREMENT ONLY'})")
    print("=" * 100)
    header = f"{'id':<7} {'found':<6} {'res':>4} {'slugs':>6} {'gtok':>6} {'btok':>7} {'ratio':>7}  origins"
    print(header)
    print("-" * 100)
    for outcome in outcomes:
        ratio = f"{outcome.graph_tokens / outcome.baseline_tokens:.3f}" if outcome.baseline_tokens else "n/a"
        origins = ",".join(f"{k}:{v}" for k, v in outcome.origins.most_common()) or "-"
        print(
            f"{outcome.id:<7} {'YES' if outcome.found else 'no':<6} {outcome.result_count:>4} "
            f"{len(outcome.slugs):>6} {outcome.graph_tokens:>6} {outcome.baseline_tokens:>7} "
            f"{ratio:>7}  {origins}"
        )
        if outcome.notes:
            print(f"{'':<7} note: {outcome.notes}")
    found = sum(1 for o in outcomes if o.found)
    graph_total = sum(o.graph_tokens for o in outcomes)
    baseline_total = sum(o.baseline_tokens for o in outcomes)
    precision = sum(o.evidence_precision for o in outcomes) / len(outcomes) if outcomes else 0.0
    pct = (found / len(outcomes) * 100) if outcomes else 0.0
    ratio = f"{graph_total / baseline_total:.3f}" if baseline_total else "n/a"
    print("-" * 100)
    print(
        f"  answered {found}/{len(outcomes)} ({pct:.0f}%)   "
        f"graph tokens {graph_total:,}   baseline tokens {baseline_total:,}   ratio {ratio}"
    )
    print(f"  mean evidence precision (fraction of evidence slugs that were expected): {precision:.2f}")
    return {
        "found": found,
        "total": len(outcomes),
        "graph_tokens": graph_total,
        "baseline_tokens": baseline_total,
        "precision": precision,
    }


async def run(args: argparse.Namespace) -> int:
    kb.assert_isolation()
    goldset = yaml.safe_load(GOLDSET_PATH.read_text(encoding="utf-8"))
    sections = pre.preprocess(content_root=args.content_root)

    # Price the baseline over exactly the pages the graph was given.
    ingested_slugs: set[str] | None = None
    if not args.full_corpus_baseline:
        if not kb.MANIFEST_PATH.exists():
            raise SystemExit(
                f"no manifest at {kb.MANIFEST_PATH}: run `uv run ingest/sync.py` first, or pass "
                "--full-corpus-baseline to price against every pilot page (which is NOT a fair "
                "comparison unless the whole slice was ingested)."
            )
        manifest = json.loads(kb.MANIFEST_PATH.read_text(encoding="utf-8"))["sections"]
        ingested_slugs = {entry["slug"] for entry in manifest.values() if entry.get("slug")}
    corpus = Corpus.build(sections, only_slugs=ingested_slugs)

    client = kb.build_client()
    scope = kb.SCOPES[args.scope]
    try:
        print("=" * 100)
        print("GOLD-SET EVALUATION -- JedAI portal KB pilot")
        print("=" * 100)
        print(f"  scope   {scope.key}")
        print(f"  goldset {GOLDSET_PATH}")
        print(f"  limit   {args.limit}")
        print(
            f"  corpus  {len(corpus.pages)} pages priced for the baseline"
            f"{' (all pilot pages)' if args.full_corpus_baseline else ' (= the ingested pages)'}"
        )

        band1 = await run_band(
            client,
            scope=scope,
            corpus=corpus,
            entries=goldset["band1_single_fact"],
            limit=args.limit,
            band="band1",
        )
        summary1 = print_band("BAND 1 -- single-fact lookup", band1, gate=True)

        band2 = await run_band(
            client,
            scope=scope,
            corpus=corpus,
            entries=goldset["band2_cross_page"],
            limit=args.limit,
            band="band2",
        )
        summary2 = print_band("BAND 2 -- cross-page join (multi-hop)", band2, gate=True)

        probe = await run_band(
            client,
            scope=scope,
            corpus=corpus,
            entries=goldset["band2_paraphrase_probe"],
            limit=args.limit,
            band="band2",
        )
        summary_probe = print_band(
            "BAND 2b -- paraphrase probe (INGEST.md §5a, 0.168 trigram cosine)",
            probe,
            gate=False,
        )

        print()
        print("=" * 100)
        print("SCORECARD")
        print("=" * 100)
        for name, summary, bar in (
            ("Band 1 single-fact", summary1, "parity with page stuffing"),
            ("Band 2 cross-page", summary2, "win at lower token cost"),
            ("Band 2b paraphrase", summary_probe, "measurement only"),
        ):
            ratio = summary["graph_tokens"] / summary["baseline_tokens"] if summary["baseline_tokens"] else 0.0
            print(
                f"  {name:<22} {summary['found']}/{summary['total']:<3} answered   "
                f"tokens {summary['graph_tokens']:>6,} vs {summary['baseline_tokens']:>7,} baseline "
                f"({ratio:.2%} of baseline)   [{bar}]"
            )
        print()
        print("  Band 3 (temporal supersession) runs in verify_goals.py G3-3, which performs the")
        print("  edit-and-resync in memory. The portal checkout is never modified.")
    finally:
        client.graph.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gold-set evaluation for the portal KB pilot")
    parser.add_argument("--content-root", type=Path, default=pre.DEFAULT_CONTENT_ROOT)
    parser.add_argument("--scope", choices=("external", "internal"), default="external")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--full-corpus-baseline",
        action="store_true",
        help="price the baseline over all pilot pages instead of only the ingested ones",
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
